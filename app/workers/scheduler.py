import os
import threading
import logging
import sys
import time
from datetime import datetime, timedelta
from app.timeutils import now_utc

from apscheduler.schedulers.blocking import BlockingScheduler

from app.database import SessionLocal
from app.backup import last_backup, make_backup
from app.retention import apply_retention
from app.report import CRITICAL, collect_findings, summary_line
from app.workers.verify_stock import verify_all
from app.models import Platform, PlatformAccount, WorkerHeartbeat
from app.routers.health import SCHEDULER_START_MARKER
from app.workers.credentials import CredentialsMissing
from app.workers.client_factory import build_client
from app.workers.circuit_breaker import record_success, record_failure
from app.workers.order_poller import poll_new_orders, poll_cancellations, poll_confirmations
from app.workers.dispatch import run_dispatch_cycle
from app.workers.catalog_sync import load_platform_catalog
from app.workers.catalog_poller import poll_catalog
from app.workers.ftp_channel import (
    LocalExchange, build_task_batch, apply_result_batch, collect_stock_delta,
    detect_timed_out_tasks, repost_stuck_movements, finalize_stock_delta,
    fetch_stock_export_files, fetch_stock_export_snapshot, fetch_barcode_dict_files,
    apply_stock_on_date_files, detect_timed_out_stock_date_requests,
    prune_stock_date_snapshots,
)
from app.workers.reconciliation import run_reconciliation, import_product_master, import_barcode_dict

# Время в строке лога — МЕСТНОЕ (так его ставит logging, так же 1С называет свои
# файлы обмена), а в базе и в интерфейсе — UTC. На боевом сервере это разница в три
# часа, и на ней уже один раз построили ложный вывод «1С обработала задание дважды»:
# заявка была закрыта «в 13:00», а файл ответа лежал «от 15:59» — одно и то же
# событие в двух шкалах. Поэтому к времени приписывается явное смещение:
# `2026-09-17 16:00:20,561+0300`.
LOG_FORMAT = "%(asctime)s{offset} %(levelname)s %(name)s: %(message)s"


def _log_time_offset() -> str:
    """Смещение часового пояса машины в виде `+0300`."""
    return time.strftime("%z")


def use_utf8(stream) -> bool:
    """Заставляет поток лога писать в UTF-8 вне зависимости от локали системы.

    NSSM перенаправляет stderr процесса в `worker.err.log`, а Python кодирует
    его в кодировку локали — на русской Windows это cp1251. Кириллица в логе
    оказывалась в однобайтовой кодировке, и любой инструмент, читающий файл как
    UTF-8, показывал вместо неё мусор: строка
    `reconciliation: нет свежего файла выгрузки остатков — пропуск`
    читалась как `��� ������� ����� ... � �������`. Это не косметика: именно по
    этой строке видно, что сверка не сверяет, а найти её поиском по слову было
    нельзя. Наша же инструкция (`deploy/README_WINDOWS.md`) велела читать лог
    как UTF-8 — то есть гарантированно показывала мусор.

    `errors="replace"` — чтобы неожиданный символ (например, имя файла, которое
    файловая система отдала суррогатом) портил одну букву в строке, а не ронял
    саму запись в лог.

    Возвращает False, если поток переключить нельзя (его подменили на объект без
    `reconfigure` — так делает перехват вывода в тестах): лог тогда остаётся в
    прежней кодировке, но воркер из-за этого не падает.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return False
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        return False
    return True


def configure_logging(level: int = logging.INFO):
    use_utf8(sys.stderr)
    logging.basicConfig(level=level, format=LOG_FORMAT.format(offset=_log_time_offset()))
    # APScheduler на КАЖДЫЙ цикл пишет «Running job» и «executed successfully».
    # При задании раз в 45 секунд плюс по заданию на кабинет это сотни строк в час
    # и мегабайты в неделю, в которых тонут наши собственные строки: на боевом
    # сервере лог воркера дорос до 13 МБ, и разбирать по нему сбой уже нечем.
    # WARNING оставляет ровно то, ради чего этот лог читают: пропущенные запуски
    # (`Run time of job ... was missed`) и исключения внутри заданий.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


configure_logging()
logger = logging.getLogger("sync_worker")

# РЕАЛЬНЫЕ имена складов в базе «Магазин одежды и обуви» (справочник Склады).
# Сверено по конфигурации 2026-09-13. .epf ищет их по точному Наименованию.
# Источник (физический остаток) — центральный склад.
SOURCE_WAREHOUSE_NAME = "ЦС Склад"      # магазин ЦС_МСК

# Вариант A (см. memory warehouse-posting-rule): конфигурация «Магазин одежды и
# обуви» проводит межмагазинное перемещение ТОЛЬКО между ОСНОВНЫМИ складами
# магазинов. «Впути»-склады не основные, поэтому ЦС->Впути не проводится.
# Значит резерв под заказ сразу уходит в ОСНОВНОЙ склад площадки — он же склад
# продаж. Промежуточного «Впути»-шага в этой базе быть не может.
PENDING_WAREHOUSE_NAME = {
    Platform.wb: "Wildberries_Склад_FBO",
    Platform.ozon: "OZON_Склад",
    Platform.kit: "Сайт AWER",            # магазин «Сайт AWER» (Kit/Яндекс)
}

# Склад продаж = основной склад площадки. Совпадает с PENDING (резерв = продажа
# в один шаг), поэтому отдельного движения при подтверждении не создаётся
# (см. process_confirmation: пропуск, когда pending == sold).
SOLD_WAREHOUSE_NAME = {
    Platform.wb: "Wildberries_Склад_FBO",
    Platform.ozon: "OZON_Склад",
    Platform.kit: "Сайт AWER",            # магазин «Сайт AWER» (Kit/Яндекс)
}


def _heartbeat(db, worker_name: str, success: bool, error: str = ""):
    """Отметка «задание отработало». ОТКАТ ПЕРВЫМ ДЕЛОМ — это не перестраховка.

    Отметку почти всегда зовут из `except`-ветки, а самый важный класс сбоя —
    упавший `commit` внутри задания (`database is locked` после `busy_timeout`,
    ошибка целостности). После него сессия сломана, и `_heartbeat` падал САМ, с
    `PendingRollbackError`: запись об ошибке не появлялась вовсе, а в базе
    оставались время и `last_success=True` от ПРОШЛОГО удачного прогона. То есть
    ровно в тот момент, когда задание перестало работать, `/health` оставался
    зелёным, «Диагностика» пустой, а текст ошибки терялся. Для заданий с длинным
    окном (сверка — три часа, бэкап и чистка — трое суток) зелёный держался
    соответственно долго.

    Откат стоит ЗДЕСЬ, а не в каждой ветке: иначе новое задание заведёт этот
    дефект заново, а заметить его можно будет только по тишине.

    Откатываем ТОЛЬКО сломанную сессию (`is_active` ложно ровно в этом случае —
    проверено). Безусловный откат был бы лекарством хуже болезни: отметку зовут и
    из успешных веток, а часть заданий коммитит свою работу тем же коммитом, что
    и отметку.
    """
    if not db.is_active:
        db.rollback()
    hb = db.query(WorkerHeartbeat).filter(WorkerHeartbeat.worker_name == worker_name).first()
    if hb is None:
        hb = WorkerHeartbeat(worker_name=worker_name, last_run_at=now_utc())
        db.add(hb)
    hb.last_run_at = now_utc()
    hb.last_success = success
    hb.last_error = error[:2000] if error else None
    db.commit()


def _active_accounts(db) -> list[PlatformAccount]:
    return list(db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all())


def _build_ftp_exchange() -> LocalExchange:
    # Обмен с 1С — через локальную папку (1С и приложение на одной машине).
    # Пути переопределяются в .env через SYNC_DIR_*; по умолчанию C:\sync\...
    return LocalExchange(
        dir_tasks=os.environ.get("SYNC_DIR_TASKS", r"C:\sync\tasks"),
        dir_results=os.environ.get("SYNC_DIR_RESULTS", r"C:\sync\results"),
        dir_archive=os.environ.get("SYNC_DIR_ARCHIVE", r"C:\sync\archive"),
    )


def job_poll_orders(account_id: int):
    db = SessionLocal()
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    worker_name = f"poll_orders_account_{account_id}"
    try:
        if account is None or not account.is_active:
            return
        client = build_client(db, account_id)
        new_stats = poll_new_orders(db, client, account, PENDING_WAREHOUSE_NAME[account.platform])
        confirm_stats = poll_confirmations(
            db, client, account,
            PENDING_WAREHOUSE_NAME[account.platform], SOLD_WAREHOUSE_NAME[account.platform],
        )
        cancel_stats = poll_cancellations(db, client, account)
        logger.info("%s (%s): new=%s confirm=%s cancel=%s",
                    worker_name, account.name, new_stats, confirm_stats, cancel_stats)
        # Сбойные ОТДЕЛЬНЫЕ заказы цикл переживает (см. poll_new_orders), но
        # молчать о них нельзя: пропущенный заказ — это несписанный остаток.
        for label, st in (("заказов", new_stats), ("подтверждений", confirm_stats),
                          ("отмен", cancel_stats)):
            if st.get("failed"):
                logger.warning("%s (%s): %s не проведено из-за ошибок: %d — %s",
                               worker_name, account.name, label, st["failed"],
                               "; ".join(st.get("problems") or [])[:500])
        record_success(db, account)
        db.commit()
        _heartbeat(db, worker_name, True)
    except CredentialsMissing as e:
        logger.warning("%s: %s", worker_name, e)
        _heartbeat(db, worker_name, False, str(e))
    except Exception as e:
        logger.exception("%s failed", worker_name)
        # Откат ПЕРЕД записью сбоя. Сессия после неудачного commit внутри цикла
        # остаётся сломанной, и `record_failure` + `commit` по ней падали бы
        # сами — вместе с ними терялись и счётчик предохранителя, и heartbeat,
        # то есть сбой оставался невидимым и для «Диагностики», и для /health.
        db.rollback()
        if account is not None:
            disabled = record_failure(db, account, str(e))
            db.commit()
            if disabled:
                logger.error("%s: кабинет «%s» автоматически отключён после %d сбоев подряд",
                             worker_name, account.name, account.consecutive_failures)
        _heartbeat(db, worker_name, False, str(e))
    finally:
        db.close()


def job_dispatch():
    db = SessionLocal()
    try:
        accounts = _active_accounts(db)
        clients = {}
        for account in accounts:
            try:
                clients[account.id] = build_client(db, account.id)
            except CredentialsMissing:
                continue
        stats = run_dispatch_cycle(db, clients, accounts)
        logger.info("dispatch: %s", stats)
        _heartbeat(db, "dispatch", True)
    except Exception as e:
        logger.exception("dispatch failed")
        _heartbeat(db, "dispatch", False, str(e))
    finally:
        db.close()


# Замок на все три расписания `job_ftp_send`: см. его докстроку. Ставится на
# уровне модуля, потому что расписания живут в одном процессе воркера.
_FTP_SEND_LOCK = threading.Lock()


def job_ftp_send(request_stock_export: bool = False, request_barcode_export: bool = False,
                 heartbeat_name: str = "ftp_send"):
    """Отправка накопленных заданий в 1С. Три расписания зовут эту же функцию:
    минутное (только задания), часовое (плюс запрос выгрузки остатков) и суточное
    (плюс запрос справочника баркодов). Каждое пишет СВОЙ heartbeat: под общим
    именем минутный прогон затирал бы остальные, и остановка часового запроса
    выгрузки — то есть фактическая остановка сверки — была бы не видна в /health.

    Время heartbeat часового запроса дополнительно служит отметкой «когда мы в
    последний раз попросили выгрузку»: сверка применяет только снимок новее её.

    ОДИН ЗАМОК НА ВСЕ ТРИ РАСПИСАНИЯ. `max_instances=1` действует на каждый `id`
    по отдельности, а расписаний здесь три — то есть два прогона могли идти
    одновременно в разных потоках пула. `build_task_batch` читает `pending`-задания
    и помечает их отправленными в одной транзакции, но коммитит только в конце,
    так что оба успевали прочитать ОДНИ И ТЕ ЖЕ строки и разложить их по ДВУМ
    файлам для 1С. Для `CREATE_MOVEMENT` это безвредно — 1С идемпотентна по номеру
    заказа. Для `CANCEL_MOVEMENT` нет: отмена идемпотентной не сделана и сделана
    быть не может, второй файл создаёт ВТОРОЙ обратный документ, и на ЦС
    возвращается вдвое больше, чем оттуда уезжало. `existing_cancel_task` тут не
    помогает — она не даёт завести второе ЗАДАНИЕ, а здесь одно задание уезжает
    дважды.

    Расписания сами по себе разведены на 20 и 40 секунд и на секундной сетке не
    сходятся. Но `misfire_grace_time` плюс `coalesce` означают, что после любой
    паузы воркера все просроченные задания уходят в пул ОДНОВРЕМЕННО.
    """
    with _FTP_SEND_LOCK:
        _job_ftp_send_locked(request_stock_export, request_barcode_export, heartbeat_name)


def _job_ftp_send_locked(request_stock_export: bool, request_barcode_export: bool,
                         heartbeat_name: str):
    db = SessionLocal()
    try:
        exchange = _build_ftp_exchange()
        # Зависшие перемещения возвращаем в очередь ПЕРЕД сборкой файла, чтобы они
        # уехали этим же циклом. Механизм сам решает, включён ли он, и сам держит
        # предел повторов — здесь никаких условий не дублируем.
        repost_stats = repost_stuck_movements(db)
        if repost_stats["reposted"] or repost_stats["exhausted"]:
            logger.info("ftp_send: перепроведение зависших %s", repost_stats)
        batch = build_task_batch(db, request_stock_export=request_stock_export,
                                 request_barcode_export=request_barcode_export,
                                 exchange=exchange)
        if batch:
            filename, content = batch
            exchange.upload_task_file(filename, content)
            logger.info("ftp_send: %s (%d строк)", filename, content.count("\n") + 1)
        _heartbeat(db, heartbeat_name, True)
    except Exception as e:
        logger.exception("ftp_send failed")
        _heartbeat(db, heartbeat_name, False, str(e))
    finally:
        db.close()


def job_ftp_receive():
    db = SessionLocal()
    try:
        exchange = _build_ftp_exchange()
        for filename in exchange.list_result_files():
            # Читаем → разбираем → коммитим → и ТОЛЬКО ПОТОМ убираем в архив.
            # Раньше файл уезжал в архив первым действием, и любой сбой разбора
            # (на бою это `database is locked`) уносил ответы 1С безвозвратно:
            # из архива их никто не перечитывает, задания оставались «в пути»
            # навсегда, остаток — заниженным по созданиям и завышенным по
            # отменам. Повторный разбор того же файла безопасен: задание
            # закрывается один раз, повторный ответ по закрытому даёт
            # `unmatched`. Потеря — нет.
            content = exchange.read_result(filename)
            try:
                stats = apply_result_batch(db, content)
            except Exception:
                db.rollback()
                logger.exception("ftp_receive: %s не разобран — файл оставлен в results "
                                 "на следующий цикл", filename)
                continue
            exchange.archive_result(filename)
            logger.info("ftp_receive: %s -> %s", filename, stats)

        # Оперативные изменения остатка ЦС: 1С кладёт их сама, не дожидаясь
        # часовой выгрузки. Применяются ТОЛЬКО как частичные —
        # `missing_means_zero=False`: товар, которого в файле нет, не трогаем.
        # Полный снимок обнуляет отсутствующих намеренно, и дельта, применённая
        # как снимок, обнулила бы весь каталог с первого же сообщения.
        delta, delta_stats = collect_stock_delta(db, exchange)
        if delta:
            # Момент снимка — время ФАЙЛА, а не время приёма: дельта описывает
            # склад на ту минуту, когда 1С её записала. Со временем приёма
            # задания, закрытые в промежутке, переставали считаться «в пути»,
            # хотя в файле их ещё нет, и уже отгруженные единицы возвращались на
            # склад. `finalize_stock_delta` помечает документы и убирает файлы в
            # архив ТОЛЬКО после успешного применения — иначе правка 1С терялась
            # насовсем.
            recon = run_reconciliation(
                db, delta, missing_means_zero=False,
                snapshot_at=delta_stats.get("snapshot_at") or now_utc())
            finalize_stock_delta(db, exchange, delta_stats)
            logger.info("ftp_receive: изменение остатка ЦС %s -> сверка %s",
                        {k: v for k, v in delta_stats.items()
                         if k not in ("files_pending", "documents_pending")}, recon)
        else:
            # Применять нечего (все строки отсеяны), но файлы прочитаны — их
            # надо убрать, иначе они будут перечитываться каждую минуту.
            finalize_stock_delta(db, exchange, delta_stats)

        # Выгрузки остатков на дату — отдельный префикс файлов и отдельная
        # таблица: в остаток товара и в сверку они не попадают никогда.
        on_date = apply_stock_on_date_files(db, exchange)
        if on_date["files"] or on_date["unmatched"]:
            logger.info("ftp_receive: остатки на дату -> %s", on_date)
        prune_stock_date_snapshots(db)

        timed_out = detect_timed_out_tasks(db)
        if timed_out:
            logger.warning("ftp_receive: %d заданий просрочены без ответа", len(timed_out))

        stale_dates = detect_timed_out_stock_date_requests(db)
        if stale_dates:
            logger.warning("ftp_receive: %d заявок на остатки на дату без ответа", len(stale_dates))

        _heartbeat(db, "ftp_receive", True)
    except Exception as e:
        logger.exception("ftp_receive failed")
        _heartbeat(db, "ftp_receive", False, str(e))
    finally:
        db.close()


# Метка фактически применённой сверки. Само задание `reconciliation` отчитывается
# об успехе и когда сверять было нечем: свежего файла выгрузки 1С нет — задание
# честно отработало, ошибки не случилось. Но для системы про остатки «сверка
# запускалась» и «остатки сверены» — разные вещи: пока 1С не отдаёт выгрузку,
# остаток в приложении живёт сам по себе, расходится со складом и уезжает на
# площадки как есть, то есть это прямая дорога к оверселлу. Такое молчание уже
# ловили дважды (находки 14 и 15), здесь тот же случай: heartbeat зелёный,
# мониторинг доволен, а сверки нет. По этой метке /health видит именно её
# отсутствие.
RECONCILIATION_APPLIED = "reconciliation_applied"


def _heartbeat_at(db, worker_name: str):
    hb = db.query(WorkerHeartbeat).filter(WorkerHeartbeat.worker_name == worker_name).first()
    return hb.last_run_at if hb else None


def _export_request_answered(db) -> bool:
    """Ответ 1С на последний запрос выгрузки уже применён?

    Сравниваются две отметки: когда в последний раз ПОПРОСИЛИ выгрузку и когда в
    последний раз её ПРИМЕНИЛИ. Применили позже, чем попросили — цикл закрыт,
    ждать нечего. Нет хотя бы одной отметки (первый запуск на чистой базе) —
    считаем, что ответа ещё нет: сказать про ожидание лишний раз безопаснее, чем
    промолчать о том, что сверка стоит.
    """
    requested = _heartbeat_at(db, "ftp_send_export_request")
    applied = _heartbeat_at(db, RECONCILIATION_APPLIED)
    if requested is None or applied is None:
        return False
    return applied >= requested


def job_recalc():
    """Массовая актуализация остатков — порциями, чтобы не занимать базу надолго.

    Раз в 20 секунд берёт незавершённое задание и обрабатывает несколько товаров.
    Нет задания — мгновенно выходит. Работу делает воркер, а не веб: здесь и ключи
    площадок, и канал 1С, и запрос от браузера столько не живёт.
    """
    from app.recalc import run_tick

    db = SessionLocal()
    try:
        result = run_tick(db, build_client, lambda platform: PENDING_WAREHOUSE_NAME.get(
            platform, "Ожидает"))
        if result.get("job") and not result.get("finished"):
            logger.info("recalc: задание #%s — обработано %s из %s",
                        result["job"], result.get("processed"), result.get("total"))
        _heartbeat(db, "recalc", True)
    except Exception as e:
        logger.exception("recalc failed")
        _heartbeat(db, "recalc", False, str(e))
    finally:
        db.close()


def job_reconciliation():
    db = SessionLocal()
    try:
        exchange = _build_ftp_exchange()
        # Снимок старше последнего запроса выгрузки — ответ на ПРОШЛЫЙ цикл: он
        # отражает склад часовой давности и вернул бы проданное за час обратно.
        requested_at = _heartbeat_at(db, "ftp_send_export_request")
        rows, snapshot_at = fetch_stock_export_snapshot(exchange, not_older_than=requested_at)
        if rows:
            # 1С — хозяин ассортимента: сначала заводим/обновляем товары
            # (новые SKU, размер/цвет), затем сверяем остатки.
            imp = import_product_master(db, rows)
            logger.info("import_products: %s", imp)
            stock = {}
            for r in rows:
                for bc in r["barcodes"]:
                    stock[bc] = r["quantity"]
            # Выгрузка 1С — полный снимок склада ЦС (публикуется атомарно), поэтому
            # отсутствующий в ней товар распродан в ноль.
            stats = run_reconciliation(db, stock, missing_means_zero=True,
                                       snapshot_at=snapshot_at)
            logger.info("reconciliation: %s", stats)
            # Предохранитель «снимок подозрительный» сработал: сверка перестала
            # обнулять распроданное. Это правильно (обрезанный файл не должен
            # стереть каталог), но МОЛЧА так быть не может — состояние
            # самоподдерживающееся, снимок сам не вырастет, и предохранитель
            # будет срабатывать каждый час бесконечно. Всё это время на площадки
            # уходит последнее ненулевое число по товарам, которых на складе нет.
            suspicious = stats.get("snapshot_suspicious")
            if suspicious:
                logger.warning(
                    "reconciliation: снимок 1С покрыл только %s позиций — это меньше "
                    "половины прежних ненулевых, обнуление распроданного ОТКЛЮЧЕНО. "
                    "Наружу продолжает уходить остаток по товарам, которых на складе "
                    "нет. Проверьте выгрузку 1С: файл обрезан или сформирован не "
                    "полностью.", suspicious)
            # Разные остатки по баркодам ОДНОГО товара: несколько штрихкодов
            # одного SKU держат один физический остаток, и 1С отдаёт по ним одно
            # число. Разные значат, что один баркод привязан к чужому товару, —
            # а максимум тогда берёт ЧУЖОЙ остаток, и наружу уходит больше, чем
            # лежит на складе. Чинится мэппингом, но сначала об этом надо узнать.
            conflicts = stats.get("barcode_conflicts")
            if conflicts:
                logger.warning(
                    "reconciliation: у %s товаров 1С отдала РАЗНЫЕ остатки по разным "
                    "баркодам одного товара. Один из баркодов привязан к чужому "
                    "товару: сверка берёт наибольшее, то есть чужое число, и на "
                    "площадки уходит больше, чем есть. Разберите привязку на "
                    "странице «Мэппинг».", conflicts)
            # Отдельная метка: сверка не просто отработала, а ФАКТИЧЕСКИ применила
            # снимок 1С. Ниже, в ветке пропуска, её намеренно нет — см. RECONCILIATION_APPLIED.
            _heartbeat(db, RECONCILIATION_APPLIED, True,
                       error=(f"снимок покрыл {suspicious} позиций — обнуление "
                              f"распроданного отключено" if suspicious else ""))
        elif not _export_request_answered(db):
            # Проверок теперь двенадцать в час, а ответ 1С — один. Писать строку на
            # каждый холостой заход значит вернуть в лог тот самый шум, ради которого
            # глушили apscheduler. Пишем только пока ОЖИДАЕМ ответа на последний
            # запрос: это 1-2 строки в час и ровно та информация, ради которой лог
            # читают — «попросили, ответа пока нет». После применения снимка ждать
            # нечего, и до следующего запроса сверка молчит.
            logger.info("reconciliation: ответа 1С на запрос выгрузки ещё нет — ждём")
        _heartbeat(db, "reconciliation", True)
    except Exception as e:
        logger.exception("reconciliation failed")
        _heartbeat(db, "reconciliation", False, str(e))
    finally:
        db.close()


FULL_BARCODE_IMPORT_EVERY = timedelta(days=7)


def _should_run_full_barcode_import(db) -> bool:
    """Полный прогон справочника — раз в неделю. Метка последнего полного прогона
    хранится в WorkerHeartbeat('import_barcodes_full'). Первый прогон после
    деплоя (метки ещё нет) — сразу полный, чтобы добрать весь справочник."""
    hb = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "import_barcodes_full").first()
    if hb is None or hb.last_run_at is None:
        return True
    return (now_utc() - hb.last_run_at) >= FULL_BARCODE_IMPORT_EVERY


def job_import_barcodes():
    """Приём справочника баркодов из 1С (barcodes_*.txt): заводит баркоды с
    размером/цветом и закрывает конфликты сопоставления. Файл 1С делает раз в
    сутки по запросу (job_ftp_send request_barcode_export).

    Раз в 15 мин; но раз в неделю (по метке 'import_barcodes_full') прогон идёт
    ПОЛНЫЙ — заводит весь справочник 1С, а не только баркоды, уже присутствующие
    в каталогах площадок, — чтобы сопоставление шло по полному справочнику 1С.
    Самоопределение по метке устойчиво к тому, что файл забирается (архивируется)
    первым же прогоном: отдельный недельный джоб на том же источнике не нашёл бы
    файла."""
    db = SessionLocal()
    try:
        full = _should_run_full_barcode_import(db)
        exchange = _build_ftp_exchange()
        rows = fetch_barcode_dict_files(exchange)
        if rows:
            stats = import_barcode_dict(db, rows, full=full)
            logger.info("import_barcodes: %s", stats)
            # Метку полного прогона ставим ТОЛЬКО когда реально были строки и
            # прогон был полным — иначе таймер недели не должен сбрасываться.
            if full:
                _heartbeat(db, "import_barcodes_full", True)
        _heartbeat(db, "import_barcodes", True)
    except Exception as e:
        logger.exception("import_barcodes failed")
        _heartbeat(db, "import_barcodes", False, str(e))
    finally:
        db.close()


def job_catalog_poll(account_id: int):
    db = SessionLocal()
    worker_name = f"catalog_poll_account_{account_id}"
    try:
        account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
        if account is None or not account.is_active:
            return
        hb = db.query(WorkerHeartbeat).filter(
            WorkerHeartbeat.worker_name == worker_name).first()
        if hb is not None and hb.last_success \
                and now_utc() - hb.last_run_at < CATALOG_POLL_MIN_GAP:
            return
        client = build_client(db, account_id)
        load_stats = load_platform_catalog(db, client, account)
        proposal_stats = poll_catalog(db, account)
        logger.info("%s (%s): загрузка=%s предложения=%s", worker_name, account.name, load_stats, proposal_stats)
        # Выдачу оборвал защитный предел страниц. Снимок каталога при этом
        # выглядит свежим, но неполон: часть карточек осталась со старыми
        # данными или не завелась вовсе. По снимку считаются ключи отправки —
        # chrtId у WB, variant_id у Kit, — и по не попавшим в него позициям
        # остаток либо уйдёт баркодом, либо не уйдёт совсем. Heartbeat, а не
        # только лог: «Диагностика» показывает именно его, а лог на бою читают,
        # когда уже что-то случилось.
        if load_stats.get("truncated"):
            logger.warning(
                "%s (%s): выгрузка каталога ОБОРВАНА защитным пределом страниц — "
                "снимок неполон, по не попавшим в него карточкам остаток может "
                "уйти не тем ключом или не уйти вовсе", worker_name, account.name)
            _heartbeat(db, worker_name, True,
                       error="выгрузка каталога оборвана пределом страниц — "
                             "снимок неполон")
        else:
            _heartbeat(db, worker_name, True)
    except CredentialsMissing as e:
        _heartbeat(db, worker_name, False, str(e))
    except Exception as e:
        logger.exception("%s failed", worker_name)
        _heartbeat(db, worker_name, False, str(e))
    finally:
        db.close()


POLL_ORDERS_JOB_PREFIX = "poll_orders_account_"
CATALOG_POLL_JOB_PREFIX = "catalog_poll_account_"

# Выгрузка каталога кабинета — раз в сутки. Но триггер `interval` у APScheduler
# отсчитывает ПЕРВЫЙ запуск от момента добавления задания, а задания живут в
# памяти процесса и навешиваются заново при каждом старте воркера. Значит суточное
# задание срабатывает, только если процесс проработал сутки без перезапуска — а на
# бою он перезапускается чаще. 19.09 это и вскрылось: у `catalog_poll_account_5`
# не было ни одной отметки о прогоне ВООБЩЕ, снимок каталога Kit лежал от 14.09
# (его сделали руками со страницы «Мэппинг»). Поэтому явно просим первый запуск
# вскоре после старта, а не через сутки.
CATALOG_POLL_INTERVAL_HOURS = 24
# Сколько задание имеет право опоздать и всё-таки выполниться. Умолчание
# APScheduler — ОДНА СЕКУНДА, и это означает «занят в момент запуска — значит
# не выполнять». Пять минут покрывают любую разумную занятость воркера,
# включая длинную транзакцию сверки и накат.
MISFIRE_GRACE_SECONDS = 300

CATALOG_POLL_FIRST_RUN_DELAY = timedelta(minutes=2)
# Раз задание теперь запускается после каждого старта, а стартов за сутки бывает
# много, саму выгрузку пропускаем, если она недавно уже отработала успешно.
# Считаем по своей же отметке: пропуск её НЕ трогает, иначе он сдвигал бы срок
# вперёд на каждом рестарте и выгрузка снова не случилась бы никогда.
CATALOG_POLL_MIN_GAP = timedelta(hours=20)


def reconcile_account_jobs(sched, db) -> dict:
    """Приводит per-account задания планировщика в соответствие с БД:
    добавляет задания для новых активных кабинетов и снимает для тех,
    что отключили или удалили. Благодаря периодическому вызову
    (см. build_scheduler) новый кабинет из админки подхватывается сам,
    без перезапуска процесса планировщика."""
    active_ids = {a.id for a in _active_accounts(db)}

    existing_poll = {j.id for j in sched.get_jobs() if j.id.startswith(POLL_ORDERS_JOB_PREFIX)}
    existing_catalog = {j.id for j in sched.get_jobs() if j.id.startswith(CATALOG_POLL_JOB_PREFIX)}
    desired_poll = {f"{POLL_ORDERS_JOB_PREFIX}{i}" for i in active_ids}
    desired_catalog = {f"{CATALOG_POLL_JOB_PREFIX}{i}" for i in active_ids}

    added = removed = 0
    for account_id in active_ids:
        poll_id = f"{POLL_ORDERS_JOB_PREFIX}{account_id}"
        if poll_id not in existing_poll:
            sched.add_job(job_poll_orders, "interval", minutes=2, args=[account_id],
                          id=poll_id, max_instances=1)
            added += 1
        catalog_id = f"{CATALOG_POLL_JOB_PREFIX}{account_id}"
        if catalog_id not in existing_catalog:
            # `next_run_time` обязателен: без него первый прогон — через сутки
            # после старта, до которых процесс не доживает (см. константы выше).
            # Планировщик поднят с timezone="UTC", naive-время он трактует в ней
            # же, так что `now_utc()` тут ровно то, что нужно.
            sched.add_job(job_catalog_poll, "interval",
                          hours=CATALOG_POLL_INTERVAL_HOURS, args=[account_id],
                          id=catalog_id, max_instances=1,
                          next_run_time=now_utc() + CATALOG_POLL_FIRST_RUN_DELAY)
            added += 1

    dropped_heartbeats = 0
    for stale_id in (existing_poll - desired_poll) | (existing_catalog - desired_catalog):
        sched.remove_job(stale_id)
        removed += 1
        # Вместе с заданием убираем и его heartbeat. Иначе строка остаётся
        # навсегда, протухает — и /health бессрочно отдаёт 503 из-за кабинета,
        # который отключён штатно (руками или предохранителем). Мониторинг,
        # который всегда красный, никто не читает.
        # id задания и имя heartbeat — одна и та же строка
        # (`poll_orders_account_<id>` / `catalog_poll_account_<id>`); это
        # закреплено тестом, чтобы переименование не сломало уборку молча.
        dropped_heartbeats += db.query(WorkerHeartbeat).filter(
            WorkerHeartbeat.worker_name == stale_id,
        ).delete(synchronize_session=False)
    if dropped_heartbeats:
        db.commit()

    return {"added": added, "removed": removed, "heartbeats_dropped": dropped_heartbeats}


def job_reconcile_accounts(sched):
    db = SessionLocal()
    try:
        stats = reconcile_account_jobs(sched, db)
        if stats["added"] or stats["removed"]:
            logger.info("reconcile_accounts: %s", stats)
        _heartbeat(db, "reconcile_accounts", True)
    except Exception as e:
        logger.exception("reconcile_accounts failed")
        _heartbeat(db, "reconcile_accounts", False, str(e))
    finally:
        db.close()


# Сверка отправленного с тем, что площадка держит на самом деле. Раз в полчаса:
# чаще незачем (проверяем не сиюминутное состояние, а «осталось ли наше число»),
# реже — теряем связь с событием. Ходит В API ПЛОЩАДОК, поэтому вынесена в
# отдельное задание, а не в отчёт: отчёт обязан оставаться чистым чтением базы.
VERIFY_STOCK_INTERVAL_MINUTES = 30


def job_verify_stock():
    db = SessionLocal()
    try:
        accounts = _active_accounts(db)
        stats = verify_all(db, build_client, accounts)
        if stats["diverged"]:
            # WARNING: площадка держит не то, что мы отправили. Либо кто-то
            # пишет поверх нас, либо отправка поняла ответ неверно.
            logger.warning("сверка остатков: %s", stats)
        else:
            logger.info("сверка остатков: %s", stats)
        _heartbeat(db, "verify_stock", True)
    except Exception as e:
        logger.exception("verify_stock failed")
        _heartbeat(db, "verify_stock", False, str(e))
    finally:
        db.close()


# Отчёт о расхождениях — раз в час. Ничего не чинит и не отправляет наружу,
# только читает и пишет ОДНУ строку в лог. Смысл именно в строке: каждый
# серьёзный дефект сентября был виден в данных за часы до того, как его нашли, —
# не хватало не данных, а того, кто посмотрит. Теперь смотреть можно по логу, не
# открывая браузер.
DISCREPANCY_REPORT_INTERVAL_MINUTES = 60


def job_discrepancy_report():
    db = SessionLocal()
    try:
        findings = collect_findings(db)
        line = summary_line(findings)
        if any(f.level == CRITICAL for f in findings):
            # WARNING, а не INFO: критичная находка — это деньги, и она обязана
            # отличаться от обычного «посмотрел, всё в порядке» при поиске по логу.
            logger.warning("расхождения: %s", line)
        else:
            # Пишем и когда всё чисто. Отсутствие строки должно означать «отчёт не
            # собирался», а не «расхождений не было» — иначе молчание неотличимо
            # от поломки самого отчёта.
            logger.info("расхождения: %s", line)
        _heartbeat(db, "discrepancy_report", True)
    except Exception as e:
        logger.exception("discrepancy_report failed")
        _heartbeat(db, "discrepancy_report", False, str(e))
    finally:
        db.close()


# Бэкап базы. Раз в сутки, первый прогон — вскоре после старта: суточное
# задание на `interval` до своего первого запуска не доживает (процесс
# перезапускается чаще раза в сутки, см. историю с выгрузкой каталога).
BACKUP_INTERVAL_HOURS = 24
BACKUP_FIRST_RUN_DELAY = timedelta(minutes=5)
# Не снимать копию, если свежая уже есть. Иначе цепочка перезапусков воркера
# наделала бы копий на каждый старт и вытеснила бы ими всю историю.
BACKUP_MIN_GAP = timedelta(hours=20)


def job_backup():
    """Резервная копия базы — единственное, что отделяет от невосстановимой
    потери мэппинга и истории проведения.

    Копия снимается на живой базе штатным механизмом SQLite и сразу
    проверяется (см. `app/backup.py`). Пропуск при свежей копии — тоже успех:
    инвариант «свежая копия есть» выполнен, и heartbeat об этом и говорит.
    Молчать в журнале нельзя ни в одном из случаев: бэкап, о котором ничего не
    написано, неотличим от бэкапа, которого не было.

    «Свежая копия есть» считается по `last_backup()`, то есть ПО ИМЕНИ ФАЙЛА, и
    это безопасно ровно потому, что неудавшаяся попытка под именем копии не
    остаётся: `make_backup` откладывает её в `.bad`. Пока этого не было, одна
    сорвавшаяся попытка делала следующий запуск «пропуском при свежей копии» —
    зелёный heartbeat, никакой новой попытки двадцать часов и молчащая находка
    отчёта двое суток, при том что копии нет.
    """
    db = SessionLocal()
    try:
        moment, total = last_backup()
        if moment is not None and now_utc() - moment < BACKUP_MIN_GAP:
            logger.info("бэкап: свежая копия уже есть (%s), пропуск; всего копий %d",
                        moment.strftime("%d.%m.%Y %H:%M"), total)
            _heartbeat(db, "backup", True)
            return

        result = make_backup()
        if result.ok:
            logger.info("бэкап: %s, %.1f МБ, удалено старых %d",
                        result.path, result.size_bytes / 1024 / 1024, result.removed)
            _heartbeat(db, "backup", True)
        else:
            # WARNING, а не INFO: неснятая копия — это риск потерять всё, и в
            # журнале она обязана отличаться от обычного прогона.
            logger.warning("бэкап НЕ СНЯТ: %s", result.error)
            _heartbeat(db, "backup", False, result.error)
    except Exception as e:
        logger.exception("backup failed")
        _heartbeat(db, "backup", False, str(e))
    finally:
        db.close()


# Чистка истории по срокам хранения. Раз в сутки и с тем же «первым прогоном
# вскоре после старта», что и бэкап: суточное задание на `interval` до своего
# первого запуска не доживает.
RETENTION_INTERVAL_HOURS = 24
RETENTION_FIRST_RUN_DELAY = timedelta(minutes=9)


def job_retention():
    """Удалить историю, вышедшую за срок хранения (`app/retention.py`).

    Девять минут после старта, а не пять: бэкап идёт первым намеренно. Если
    чистка когда-нибудь удалит лишнее, копия, снятая ДО неё, окажется тем
    единственным, что это исправит.
    """
    db = SessionLocal()
    try:
        stats = apply_retention(db)
        total = sum(stats.values())
        if total:
            logger.info("хранение: удалено %d строк (%s)", total,
                        ", ".join(f"{k}={v}" for k, v in stats.items() if v))
        else:
            # Пишем и когда чистить нечего: молчание обязано означать «задание не
            # отработало», а не «всё в пределах сроков».
            logger.info("хранение: удалять нечего")
        _heartbeat(db, "retention", True)
    except Exception as e:
        logger.exception("retention failed")
        _heartbeat(db, "retention", False, str(e))
    finally:
        db.close()


def build_scheduler() -> BlockingScheduler:
    """Статические задания навешиваются один раз, per-account задания —
    через reconcile_account_jobs() (первый прогон при старте плюс
    периодический каждые 5 минут). Новый активный кабинет из админки
    подхватывается автоматически в пределах этого интервала — перезапуск
    процесса больше не требуется."""
    # `job_defaults` обязателен, и вот почему. По умолчанию APScheduler даёт
    # заданию `misfire_grace_time = 1` СЕКУНДУ: если момент запуска прошёл
    # больше секунды назад (воркер был занят, машина притормозила, база держала
    # блокировку), задание не выполняется вовсе — оно помечается пропущенным.
    # Для рассылки раз в 45 секунд это безобидно, следующий цикл всё доделает.
    # Но тем же правилом живут ЧАСОВОЙ запрос выгрузки остатков у 1С и СУТОЧНЫЙ
    # запрос справочника баркодов: там пропуск означает час и сутки без данных
    # соответственно — молча, и узнаёшь об этом по последствиям.
    # `coalesce` не даёт накопившимся пропускам выполниться пачкой: нам нужен
    # один прогон, а не пять подряд.
    sched = BlockingScheduler(
        timezone="UTC",
        job_defaults={"misfire_grace_time": MISFIRE_GRACE_SECONDS,
                      "coalesce": True, "max_instances": 1},
    )

    # Отметка старта: по ней /health понимает, сколько планировщик работает, и не
    # объявляет пропавшим воркер, который просто ещё не отработал первый раз.
    db = SessionLocal()
    try:
        _heartbeat(db, SCHEDULER_START_MARKER, True)
    finally:
        db.close()

    sched.add_job(job_dispatch, "interval", seconds=45, id="dispatch", max_instances=1)
    sched.add_job(job_ftp_send, "interval", minutes=1, id="ftp_send", max_instances=1)
    sched.add_job(job_ftp_receive, "interval", minutes=1, id="ftp_receive", max_instances=1)
    # Часовые задания: первый прогон сразу после старта, а не через час. Иначе каждый
    # рестарт воркера (деплой) сдвигает запрос выгрузки и сверку на час, и остаток ЦС
    # в приложении отстаёт от 1С до часа дольше, чем должен.
    start = now_utc()
    sched.add_job(lambda: job_ftp_send(request_stock_export=True,
                                       heartbeat_name="ftp_send_export_request"), "interval",
                  hours=1, id="ftp_send_export_request", max_instances=1,
                  next_run_time=start + timedelta(seconds=20))
    # Сверка смотрит папку ответов КАЖДЫЕ 5 МИНУТ, хотя выгрузку просим раз в час.
    # Раньше она была часовой и шла через 5 минут после запроса — «чтобы 1С успела
    # ответить». Это молчаливо предполагало, что 1С отвечает быстрее пяти минут, а на
    # боевом обработка запускается по СВОЕМУ расписанию, раз в 10 минут. Ответ приходил
    # в среднем через 7 минут, то есть всегда ПОСЛЕ того, как сверка уже посмотрела и
    # ушла, а на следующем цикле тот же файл отбраковывался как более старый, чем новый
    # запрос. И это не разовое невезение: оба расписания периодические, поэтому фаза,
    # выпавшая при старте воркера, держится до его перезапуска — сверка, промахнувшись один раз,
    # не срабатывала уже никогда. Именно так она простояла 17.09.2026.
    # Разделяем два разных вопроса: «как часто просить у 1С выгрузку» (дорого — раз в час,
    # это полный снимок 152 тыс. товаров) и «как часто проверять, не пришёл ли ответ»
    # (дёшево — это чтение каталога). Ни на какое расписание 1С мы больше не закладываемся.
    # Правило свежести при этом НЕ ослаблено: применяется по-прежнему только снимок новее
    # последнего запроса — учащение проверок не даёт применить ни одного файла, который
    # старая схема сочла бы устаревшим.
    sched.add_job(job_reconciliation, "interval", minutes=5, id="reconciliation",
                  max_instances=1, next_run_time=start + timedelta(minutes=1))
    sched.add_job(lambda: job_ftp_send(request_barcode_export=True,
                                       heartbeat_name="ftp_send_barcode_request"), "interval",
                  hours=24, id="ftp_send_barcode_request", max_instances=1,
                  next_run_time=start + timedelta(seconds=40))
    sched.add_job(job_import_barcodes, "interval", minutes=15, id="import_barcodes", max_instances=1)
    # Массовая актуализация: частый опрос дешёвый (без задания — один SELECT),
    # зато прогресс на странице двигается заметно для человека.
    sched.add_job(job_recalc, "interval", seconds=20, id="recalc", max_instances=1)
    # Первый прогон вскоре после старта, а не через час: суточные и часовые задания
    # на `interval` отсчитывают первый запуск от момента добавления, а воркер
    # перезапускается чаще (см. историю `job_catalog_poll` выше).
    sched.add_job(job_discrepancy_report, "interval",
                  minutes=DISCREPANCY_REPORT_INTERVAL_MINUTES, id="discrepancy_report",
                  max_instances=1, next_run_time=start + timedelta(minutes=2))
    # Первый прогон через три минуты после старта — по той же причине, что и у
    # отчёта: `interval` отсчитывает первый запуск от момента добавления задания.
    sched.add_job(job_verify_stock, "interval",
                  minutes=VERIFY_STOCK_INTERVAL_MINUTES, id="verify_stock",
                  max_instances=1, next_run_time=start + timedelta(minutes=3))

    # Per-account задания: первичная простановка + периодическая сверка.
    db = SessionLocal()
    try:
        reconcile_account_jobs(sched, db)
    finally:
        db.close()
    sched.add_job(job_backup, "interval", hours=BACKUP_INTERVAL_HOURS, id="backup",
                  max_instances=1, next_run_time=start + BACKUP_FIRST_RUN_DELAY)
    sched.add_job(job_retention, "interval", hours=RETENTION_INTERVAL_HOURS,
                  id="retention", max_instances=1,
                  next_run_time=start + RETENTION_FIRST_RUN_DELAY)
    sched.add_job(job_reconcile_accounts, "interval", minutes=5, args=[sched],
                  id="reconcile_accounts", max_instances=1)

    return sched


def main():
    """Точка входа службы sync_admin_worker."""
    logger.info("Запуск планировщика воркеров синхронизации")
    try:
        build_scheduler().start()
    except KeyboardInterrupt:
        # NSSM останавливает службу, посылая процессу Ctrl+C. Без этой ветки
        # Python валил в лог трассировку KeyboardInterrupt, и штатный рестарт —
        # то есть каждый деплой — выглядел в журнале как авария. При разборе
        # настоящего сбоя это лишний ложный след.
        # Ловим ТОЛЬКО остановку: любое другое исключение обязано долететь до
        # лога со стеком, иначе упавший планировщик будет молчать.
        logger.info("Планировщик остановлен")


if __name__ == "__main__":
    main()

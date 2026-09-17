import os
import logging
import sys
import time
from datetime import datetime, timedelta
from app.timeutils import now_utc

from apscheduler.schedulers.blocking import BlockingScheduler

from app.database import SessionLocal
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
    LocalExchange, build_task_batch, apply_result_batch, detect_timed_out_tasks,
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
        record_success(db, account)
        db.commit()
        _heartbeat(db, worker_name, True)
    except CredentialsMissing as e:
        logger.warning("%s: %s", worker_name, e)
        _heartbeat(db, worker_name, False, str(e))
    except Exception as e:
        logger.exception("%s failed", worker_name)
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


def job_ftp_send(request_stock_export: bool = False, request_barcode_export: bool = False,
                 heartbeat_name: str = "ftp_send"):
    """Отправка накопленных заданий в 1С. Три расписания зовут эту же функцию:
    минутное (только задания), часовое (плюс запрос выгрузки остатков) и суточное
    (плюс запрос справочника баркодов). Каждое пишет СВОЙ heartbeat: под общим
    именем минутный прогон затирал бы остальные, и остановка часового запроса
    выгрузки — то есть фактическая остановка сверки — была бы не видна в /health.

    Время heartbeat часового запроса дополнительно служит отметкой «когда мы в
    последний раз попросили выгрузку»: сверка применяет только снимок новее её."""
    db = SessionLocal()
    try:
        exchange = _build_ftp_exchange()
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
            content = exchange.download_and_archive_result(filename)
            stats = apply_result_batch(db, content)
            logger.info("ftp_receive: %s -> %s", filename, stats)

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


def job_reconciliation():
    db = SessionLocal()
    try:
        exchange = _build_ftp_exchange()
        # Снимок старше последнего запроса выгрузки — ответ на ПРОШЛЫЙ цикл: он
        # отражает склад часовой давности и вернул бы проданное за час обратно.
        marker = db.query(WorkerHeartbeat).filter(
            WorkerHeartbeat.worker_name == "ftp_send_export_request").first()
        rows, snapshot_at = fetch_stock_export_snapshot(
            exchange, not_older_than=marker.last_run_at if marker else None)
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
            # Отдельная метка: сверка не просто отработала, а ФАКТИЧЕСКИ применила
            # снимок 1С. Ниже, в ветке пропуска, её намеренно нет — см. RECONCILIATION_APPLIED.
            _heartbeat(db, RECONCILIATION_APPLIED, True)
        else:
            logger.info("reconciliation: нет свежего файла выгрузки остатков — пропуск")
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
        client = build_client(db, account_id)
        load_stats = load_platform_catalog(db, client, account)
        proposal_stats = poll_catalog(db, account)
        logger.info("%s (%s): загрузка=%s предложения=%s", worker_name, account.name, load_stats, proposal_stats)
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
            sched.add_job(job_catalog_poll, "interval", hours=24, args=[account_id],
                          id=catalog_id, max_instances=1)
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


def build_scheduler() -> BlockingScheduler:
    """Статические задания навешиваются один раз, per-account задания —
    через reconcile_account_jobs() (первый прогон при старте плюс
    периодический каждые 5 минут). Новый активный кабинет из админки
    подхватывается автоматически в пределах этого интервала — перезапуск
    процесса больше не требуется."""
    sched = BlockingScheduler(timezone="UTC")

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
    # Сверка — через 5 минут после запроса выгрузки, чтобы 1С успела ответить:
    # обработка запускается по своему расписанию, и читать результат через 10 секунд
    # означало читать файл прошлого цикла.
    sched.add_job(job_reconciliation, "interval", hours=1, id="reconciliation", max_instances=1,
                  next_run_time=start + timedelta(minutes=5))
    sched.add_job(lambda: job_ftp_send(request_barcode_export=True,
                                       heartbeat_name="ftp_send_barcode_request"), "interval",
                  hours=24, id="ftp_send_barcode_request", max_instances=1,
                  next_run_time=start + timedelta(seconds=40))
    sched.add_job(job_import_barcodes, "interval", minutes=15, id="import_barcodes", max_instances=1)

    # Per-account задания: первичная простановка + периодическая сверка.
    db = SessionLocal()
    try:
        reconcile_account_jobs(sched, db)
    finally:
        db.close()
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

"""Массовая актуализация остатков: «Расчёт» на странице «Товары и остатки».

Порядок работы оператора, ради которого всё это существует:

1. отобрать фильтрами товары, которые собираются включить на трансляцию;
2. «Записать остаток ЦС на дату» — закрепить порог на выбранное число;
3. поправить факт и бронь, где надо;
4. **«Расчёт»** — поднять с площадок реальные заказы с той же даты и провести
   их: списать остаток и создать перемещения в 1С их настоящими датами;
5. и только потом включить трансляцию.

Смысл шага 4 в том, что между базовой датой и сегодняшним днём товар продавался
на маркетплейсах, а в 1С эти отгрузки не отражены. Пока их не провести, остаток
ЦС завышен, и включённая трансляция отправит на площадки числа больше реальных —
то есть оверселл. Движения самого склада отдельно поднимать не нужно: они
приходят часовой выгрузкой 1С уже внутри остатка.

**На площадки в ходе актуализации не уходит ничего** — ни остаток, ни ноль.
Обеспечивает это не расчёт, а общее правило: нетранслируемый товар в очередь
рассылки не ставится вовсе (`transmit.enqueue_full_resend`,
`order_poller._enqueue_dispatch_to_others`). С площадок только читаем заказы.

Выполняет работу ВОРКЕР, а не веб: по каждому товару надо опросить каждый его
кабинет по историческим заказам, и на полусотне позиций это сотни обращений к
API площадок. Браузер столько не ждёт, а перезагрузка страницы посреди прогона
оставила бы часть товаров необработанными без следа.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from app.models import (Barcode, PlatformAccount, ProcessedOrder, Product, RecalcItem,
                        RecalcJob, RecalcStatus, SyncSetting)
from app.timeutils import now_utc
from app.broadcast_gate import apply_pending_broadcast
from app.transmit import covered_accounts, enqueue_full_resend
from app.workers.credentials import CredentialsMissing

logger = logging.getLogger("sync_worker")

# Сколько товаров обрабатывать за один заход воркера. Задание идёт порциями, а не
# одним куском: база — SQLite, в которую в это же время пишут опрос заказов и
# рассылка, и держать её занятой минутами нельзя. Плюс прогресс на странице
# двигается, а не стоит до самого конца.
ITEMS_PER_TICK = 5


def create_job(db: Session, products: list[Product], username: str) -> RecalcJob:
    """Создаёт задание по списку товаров.

    Список фиксируется здесь и сейчас. Отбор по фильтру мог бы измениться, пока
    задание стоит в очереди (пришла сверка, оператор поправил строку), и тогда
    обработалось бы не то, что человек видел на экране, когда нажимал кнопку.
    """
    job = RecalcJob(created_by=username, total=len(products))
    db.add(job)
    db.flush()                       # нужен job.id; autoflush в приложении выключен
    for product in products:
        db.add(RecalcItem(job_id=job.id, uid_1c=product.uid_1c))
    return job


def active_job(db: Session) -> RecalcJob | None:
    """Незавершённое задание. Второе параллельно не заводим: они шли бы по одним
    и тем же товарам и дублировали обращения к площадкам."""
    return db.query(RecalcJob).filter(
        RecalcJob.status.in_([RecalcStatus.pending, RecalcStatus.running]),
    ).order_by(RecalcJob.id.asc()).first()


def last_job(db: Session) -> RecalcJob | None:
    return db.query(RecalcJob).order_by(RecalcJob.id.desc()).first()


def _enabled_accounts(db: Session, uid_1c: str) -> list[PlatformAccount]:
    """Активные кабинеты, отмеченные для товара. Заказы поднимаем только оттуда:
    именно в этих кабинетах он и продавался."""
    ids = [s.account_id for s in db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.enabled.is_(True)).all()]
    if not ids:
        return []
    return db.query(PlatformAccount).filter(
        PlatformAccount.id.in_(ids), PlatformAccount.is_active.is_(True),
    ).order_by(PlatformAccount.id).all()


def collect_orders(db: Session, product: Product, since: date,
                   build_client) -> tuple[list, list, list]:
    """Читает с площадок заказы товара с даты. Ничего не меняет.

    Фильтр строго по баркодам этого товара, а не через общее сопоставление: иначе
    чужие баркоды кабинета осели бы конфликтами сопоставления, которых оператор
    не просил.
    """
    barcodes = {b.barcode for b in db.query(Barcode).filter(Barcode.uid_1c == product.uid_1c).all()}
    if not barcodes:
        return [], ["нет баркодов — товар не сопоставлен"], []

    accounts = _enabled_accounts(db, product.uid_1c)
    if not accounts:
        # Спрашивать заказы негде. Молча вернуть «ничего не нашлось» нельзя: тогда
        # товар получил бы отметку «актуализирован», хотя мы никуда не заглядывали,
        # и оператор включил бы трансляцию, считая остаток проверенным.
        return [], ["не отмечен ни один кабинет — заказы спрашивать негде"], []

    rows, problems = [], []
    covered: list[int] = []
    for account in accounts:
        try:
            client = build_client(db, account.id)
        except CredentialsMissing as e:
            problems.append(f"{account.name}: нет ключей ({e})")
            continue
        try:
            orders = client.get_orders_since(since)
        except Exception as e:
            problems.append(f"{account.name}: {type(e).__name__}: {e}")
            continue

        # Площадка могла ответить, но не по всем строкам: у Kit баркод узнаётся
        # отдельным запросом на вариант, и 429 после всех повторов раньше просто
        # выбрасывал заказ. Тишина и пустой ответ выглядели одинаково, а значат
        # разное — «продаж не было» против «мы не увидели часть продаж». Пробел
        # обязан дойти сюда: иначе товар получит «актуализирован» по заказам,
        # которых расчёт не видел, и оператор включит трансляцию завышенного
        # остатка.
        lost = getattr(client, "last_unresolved", 0) or 0
        if lost:
            problems.append(
                f"{account.name}: не удалось получить баркод по {lost} строкам заказов "
                f"(лимит запросов площадки) — часть продаж могла остаться неучтённой")
            continue

        # Лента оборвалась не потому, что данные кончились, а потому что упёрлась
        # в защитный предел страниц. 18.09 на бою похожая тишина стоила 253
        # непроведённых заказа по одному кабинету: расчёт отчитался «проведено 0,
        # проблем нет» и поставил товару «актуализирован».
        if getattr(client, "last_truncated", False):
            problems.append(
                f"{account.name}: лента заказов оборвалась на защитном пределе — "
                f"картина неполная, часть продаж не увидена")
            continue

        covered.append(account.id)
        seen = set()
        for o in orders:
            if o.order_id in seen or o.barcode not in barcodes:
                continue
            seen.add(o.order_id)
            rows.append((account, o))
    rows.sort(key=lambda r: (r[1].order_date or date(1970, 1, 1), r[0].id))
    return rows, problems, covered


def catch_up_product(db: Session, product: Product, build_client, pending_warehouse) -> dict:
    """Актуализирует один товар: поднимает заказы с базовой даты и проводит их.

    Проводит тем же `process_new_order`, что и живой опрос, — с `is_test=False`
    (отгрузки настоящие, в 1С их ещё нет) и `respect_enabled=False` (галочка
    «синхронизировать» гейтит ЖИВОЙ опрос, а здесь мы сознательно догоняем
    прошлое). Идемпотентность по `ProcessedOrder` не даёт провести заказ дважды,
    поэтому повторный запуск безопасен.

    Сдвинули дату назад — поднимутся более старые заказы, и перемещения в 1С
    создадутся на них. Это осознанное поведение: именно так и догоняют историю.
    """
    from app.workers.order_poller import process_new_order   # локально: цикл импортов
    from app.workers.platform_clients.base import PlatformOrder

    stats = {"applied": 0, "skipped": 0, "failed": 0, "problems": [],
             "covered_accounts": [], "broadcast_on": 0}

    if product.offset_base_date is None:
        stats["problems"].append("не задана дата расчёта")
        return stats

    rows, problems, covered = collect_orders(db, product, product.offset_base_date, build_client)
    stats["problems"] += problems
    stats["covered_accounts"] = covered

    for account, o in rows:
        already = db.query(ProcessedOrder).filter(
            ProcessedOrder.account_id == account.id,
            ProcessedOrder.order_id == o.order_id,
        ).first() is not None
        if already:
            stats["skipped"] += 1
            continue
        order = PlatformOrder(order_id=o.order_id, barcode=o.barcode, quantity=o.quantity,
                              raw_status=o.raw_status, order_date=o.order_date)
        try:
            result = process_new_order(
                db, order, account, pending_warehouse(account.platform),
                is_test=False, order_date=o.order_date, respect_enabled=False)
            if result["status"] == "processed":
                stats["applied"] += 1
            elif result["status"] == "already_processed":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1
            db.commit()
        except Exception as e:
            # Без отката сессия остаётся сломанной после неудачного commit внутри
            # process_new_order, и следующий же заказ вылетел бы наружу. Часть
            # заказов при этом уже проведена — бросать задание нельзя.
            db.rollback()
            stats["failed"] += 1
            stats["problems"].append(f"заказ {o.order_id}: {type(e).__name__}: {e}")

    # Отметку ставим, даже если новых заказов не нашлось: это значит, что за
    # период их и не было, а остаток уже актуален. Не ставим только когда что-то
    # помешало посмотреть — иначе «актуализирован» было бы неправдой.
    if not stats["problems"]:
        was_covered = covered_accounts(product)
        product.recalc_done_at = now_utc()
        # Запоминаем ИМЕННО те кабинеты, чьи заказы удалось прочитать целиком.
        # Кабинет, отмеченный позже, в этот список не попадёт — и трансляция в
        # него не начнётся, пока расчёт не пройдёт заново уже с ним.
        product.recalc_account_ids = ",".join(str(i) for i in sorted(covered)) or None

        # Кабинет, покрытый ВПЕРВЫЕ, надо ещё и толкнуть. Ворота ему открыл
        # именно этот расчёт, а событие, которое поставило бы доотправку в
        # очередь, уже прошло: галочку отмечали ДО расчёта, и тогда ступень 2
        # справедливо отказала. Без этого получается состояние «трансляция
        # включена, кабинет покрыт, строка показывает число» — и при этом
        # наружу не ушло ничего до ближайшего изменения остатка из 1С.
        # Так и вышло 18.09 с Kit: карточка осталась стоять в нуле, который мы
        # же туда и отправили.
        #
        # Отложенное включение трансляции идёт ДО постановки в очередь, и это не
        # косметика: нетранслируемый товар автоматические пути в очередь не
        # ставят вовсе, поэтому включи мы его после — ворота открылись бы, а
        # наружу до ближайшего изменения остатка не ушло бы ничего.
        if apply_pending_broadcast(product):
            stats["broadcast_on"] += 1
            logger.info("расчёт: трансляция включена по просьбе из файла — %s",
                        product.uid_1c)
            # Кабинеты, покрытые не впервые, цикл ниже не затронет, а число по
            # ним до сих пор не уезжало — трансляция была выключена.
            for account_id in sorted(was_covered & set(covered)):
                enqueue_full_resend(db, product.uid_1c, account_id,
                                    reason="recalc_broadcast_on")
        for account_id in sorted(set(covered) - was_covered):
            enqueue_full_resend(db, product.uid_1c, account_id, reason="recalc_covered")
    return stats


def run_tick(db: Session, build_client, pending_warehouse, limit: int = ITEMS_PER_TICK) -> dict:
    """Один заход воркера: берёт задание и обрабатывает до `limit` товаров."""
    job = active_job(db)
    if job is None:
        return {"job": None}

    if job.status == RecalcStatus.pending:
        job.status = RecalcStatus.running
        job.started_at = now_utc()
        db.commit()

    items = db.query(RecalcItem).filter(
        RecalcItem.job_id == job.id, RecalcItem.done.is_(False),
    ).order_by(RecalcItem.id.asc()).limit(limit).all()

    if not items:
        job.status = RecalcStatus.done
        job.finished_at = now_utc()
        db.commit()
        logger.info("recalc: задание #%d завершено — товаров %d, заказов проведено %d, "
                    "пропущено %d, с ошибкой %d",
                    job.id, job.processed, job.orders_applied, job.orders_skipped,
                    job.failed_items)
        return {"job": job.id, "finished": True}

    for item in items:
        product = db.query(Product).filter(Product.uid_1c == item.uid_1c).first()
        if product is None:
            item.done = True
            item.error = "товар не найден"
            job.failed_items += 1
            job.processed += 1
            db.commit()
            continue

        stats = catch_up_product(db, product, build_client, pending_warehouse)
        item.done = True
        item.orders_applied = stats["applied"]
        item.orders_skipped = stats["skipped"]
        item.error = "; ".join(stats["problems"])[:1000] or None
        job.processed += 1
        job.orders_applied += stats["applied"]
        job.orders_skipped += stats["skipped"]
        if stats["failed"] or stats["problems"]:
            job.failed_items += 1
        db.commit()

    return {"job": job.id, "processed": job.processed, "total": job.total}

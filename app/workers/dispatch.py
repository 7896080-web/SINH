from datetime import datetime, timedelta
from app.timeutils import now_utc

from sqlalchemy.orm import Session

from app.transmit import quantity_for_account
from app.models import (
    DispatchQueueItem, DispatchStatus, SyncSetting, PlatformAccount, Barcode, PlatformCatalogItem, Product,
)
from app.workers.order_poller import _representative_barcode
from app.workers.platform_clients.base import PlatformClient, StockPushItem


def _resolve_push_target(db: Session, uid_1c: str, account_id: int) -> tuple[str, str, str] | None:
    """Идентификаторы товара для отправки остатка на КОНКРЕТНЫЙ кабинет.

    У каждой площадки свой ключ остатка (WB — баркод, Ozon — offer_id/артикул,
    Kit — variant_id). Ищем строку каталога этого кабинета по любому баркоду
    из пула товара 1С (у размер-цвета может быть несколько баркодов) и берём
    её идентификаторы. Возвращает (barcode, external_id, article) или None,
    если у товара вообще нет баркодов.

    Если каталог кабинета не загружен (строки нет) — отдаём только баркод
    (external_id/article пустые); клиент WB отработает верно, Ozon/Kit
    упадут обратно на баркод (заведомо загрузите каталог кабинета)."""
    pool = [b.barcode for b in db.query(Barcode).filter(Barcode.uid_1c == uid_1c).all()]
    if not pool:
        return None
    row = (
        db.query(PlatformCatalogItem)
        .filter(PlatformCatalogItem.account_id == account_id, PlatformCatalogItem.barcode.in_(pool))
        .first()
    )
    if row is not None:
        return row.barcode, row.external_id or "", row.article or ""
    return pool[0], "", ""


def _quantity_to_send(db: Session, uid_1c: str, account_id: int, quantity: int) -> int:
    """Сколько уйдёт на площадку. Лестница приоритетов — в app/transmit.py, один
    модуль на рассылку и на интерфейс (раньше копии разошлись, и страница показывала
    не то, что реально уходило). Считается в момент отправки, а не при постановке в
    очередь, чтобы взять самые свежие значения."""
    return quantity_for_account(db, uid_1c, account_id, quantity)


# Сколько раз пробуем отправить одну запись, прежде чем признать сбой
# окончательным, и пауза перед каждой следующей попыткой.
MAX_ATTEMPTS = 5

# Сколько позиций уходит на площадку в ОДНОМ запросе. См. комментарий в
# run_dispatch_cycle: предел у каждой площадки свой, сто проходит везде.
PUSH_BATCH_SIZE = 100
RETRY_BACKOFF_MINUTES = (1, 2, 5, 15)   # после 1-й, 2-й, 3-й и 4-й неудачи


def _retry_delay(attempts: int) -> timedelta:
    """Пауза перед следующей попыткой. `attempts` — сколько их уже было."""
    idx = min(max(attempts, 1), len(RETRY_BACKOFF_MINUTES)) - 1
    return timedelta(minutes=RETRY_BACKOFF_MINUTES[idx])


def run_dispatch_cycle(db: Session, clients: dict, active_accounts: list[PlatformAccount] | None = None) -> dict:
    """Раз в 30-60 секунд (см. планировщик): забирает накопленную очередь по
    каждому активному кабинету, схлопывает по товару (если за цикл пришло
    несколько изменений — берём только последнее), применяет минимальный
    порог, отправляет батчем.

    Сбой отправки НЕ терминален: запись остаётся `pending` и повторяется с
    нарастающей паузой, пока попытки не исчерпаны (`MAX_ATTEMPTS`). Раньше
    первая же ошибка площадки ставила `error`, и запись не возвращалась в работу
    никогда: остаток был списан у нас, а площадка о нём не узнавала до
    следующего события по товару — то есть продолжала продавать то, чего нет."""

    if active_accounts is None:
        active_accounts = list(db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all())

    stats = {}

    for account in active_accounts:
        client: PlatformClient | None = clients.get(account.id)
        # dispatch_enabled=False — ручная пауза трансляции на этот кабинет:
        # остатки на площадке не трогаем (очередь копится, уйдёт при включении).
        if client is None or not account.warehouse_id or not account.dispatch_enabled:
            continue

        pending = db.query(DispatchQueueItem).filter(
            DispatchQueueItem.account_id == account.id,
            DispatchQueueItem.status == DispatchStatus.pending,
            DispatchQueueItem.is_test.is_(False),  # тестовые записи со страницы тестирования — никогда не уходят на площадку
        ).order_by(DispatchQueueItem.created_at.asc()).all()

        if not pending:
            continue

        # Дедупликация: несколько записей на один товар за окно накопления —
        # реально нужно отправить только последнее значение. Берём его среди ВСЕХ
        # ожидающих записей, включая те, что сейчас на паузе после сбоя: иначе
        # отложенная запись пережила бы более новую и позже отправила бы на
        # площадку устаревшее число.
        latest_by_uid = {}
        for item in pending:
            latest_by_uid[item.uid_1c] = item

        now = now_utc()
        push_items = []
        uid_to_items = {}
        for uid_1c, item in latest_by_uid.items():
            if item.next_attempt_at is not None and item.next_attempt_at > now:
                continue          # пауза после сбоя ещё не вышла
            target = _resolve_push_target(db, uid_1c, account.id)
            if target is None:
                item.status = DispatchStatus.error
                item.last_error = "нет баркода для отправки"
                continue
            barcode, external_id, article = target
            quantity = _quantity_to_send(db, uid_1c, account.id, item.quantity)
            # Фиксируем ИМЕННО ТО число, которое уходит на площадку. `item.quantity`
            # для этого не годится: там исходный остаток, а не итог лестницы.
            item.sent_quantity = quantity
            # И идентификатор, под которым оно уходит. У товара бывает несколько
            # баркодов, выбор делает `_resolve_push_target` прямо здесь — без
            # записи восстановить ключ по базе потом невозможно. 19.09 разбор
            # «почему на WB ноль» из-за этого занял час: число знали, sku нет.
            item.sent_sku = barcode
            push_items.append(StockPushItem(
                barcode=barcode, quantity=quantity, external_id=external_id, article=article,
            ))
            uid_to_items[barcode] = item

        # Пачками, а не всё разом. Каждая площадка ограничивает размер одного
        # запроса остатков, и предел у всех разный (у Ozon он самый тесный).
        # Сто — осторожное значение, которое проходит везде: ошибка в меньшую
        # сторону стоит лишнего запроса, в большую — отказа ВСЕЙ пачки.
        #
        # При обычной работе очередь за цикл короткая и пачка выходит одна. Но
        # массовая переотправка (`enqueue_resend_all`) кладёт в очередь сразу все
        # транслируемые товары, и без деления это был бы один запрос на столько
        # позиций, сколько их есть. Делим здесь, а не в клиентах: запрос собирает
        # рассылка, и предел должен соблюдаться в одном месте.
        result = {"ok": [], "errors": []}
        for start in range(0, len(push_items), PUSH_BATCH_SIZE):
            part = client.push_stock(account.warehouse_id,
                                     push_items[start:start + PUSH_BATCH_SIZE])
            # Складываем, а не заменяем: отказ одной пачки не должен отменять
            # успех остальных — иначе одна сбойная позиция вернула бы в очередь
            # весь каталог и площадка получила бы его заново следующим циклом.
            result["ok"] += list(part.get("ok", []))
            result["errors"] += list(part.get("errors", []))

        ok_set = set(result.get("ok", []))
        errors_text = str(result.get("errors"))[:400]
        retried = 0
        for barcode, item in uid_to_items.items():
            item.attempts += 1
            if barcode in ok_set:
                item.status = DispatchStatus.sent
                item.sent_at = now_utc()
                item.next_attempt_at = None
            elif item.attempts < MAX_ATTEMPTS:
                # Сбой не окончательный: пробуем ещё, с паузой. Остаток уже списан
                # у нас — если не дослать его на площадку, она продаст то, чего нет.
                item.status = DispatchStatus.pending
                item.next_attempt_at = now_utc() + _retry_delay(item.attempts)
                item.last_error = f"попытка {item.attempts} из {MAX_ATTEMPTS}: {errors_text}"
                retried += 1
            else:
                item.status = DispatchStatus.error
                item.last_error = f"не отправлено за {item.attempts} попыток: {errors_text}"

        # Все элементы очереди по товару, кроме самого свежего — считаем
        # поглощёнными (не отправляем устаревшие промежуточные значения)
        for item in pending:
            if item.uid_1c not in latest_by_uid or latest_by_uid[item.uid_1c].id != item.id:
                item.status = DispatchStatus.sent
                item.last_error = "поглощено более новым изменением в этом цикле"

        db.commit()

        stats[account.name] = {
            "sent": len(ok_set), "errors": len(result.get("errors", [])),
            "queued": len(pending), "retry": retried,
        }

    return stats

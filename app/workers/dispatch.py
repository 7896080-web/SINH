from datetime import datetime
from app.timeutils import now_utc

from sqlalchemy.orm import Session

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
    """Итоговое количество для отправки на площадку из сырого остатка.

    Приоритет (всё применяется в момент отправки, не при постановке в очередь —
    чтобы учесть самые свежие значения):

    0. Трансляция SKU выключена (`broadcast_enabled = False`) → 0 (товар
       снимается с продажи на площадках).
    1. Ручной «передаваемый остаток» (`transmit_override` задан) → ровно это
       значение (не ниже 0). Резерв и порог не применяются — оператор задал
       число явно. Заказы вычитаются из override, отмена возвращает (order_poller);
       фоновое обновление остатка из 1С его не трогает.
    2. Иначе — расчёт: резерв (на товар) + минимальный порог (на кабинет):
       на площадку доступно max(0, остаток − резерв); если доступное не
       превышает порог — уходит 0 (не продать последние штуки во всех каналах)."""
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()

    if product is not None and not product.broadcast_enabled:
        return 0
    # Порог трансляции (broadcast_offset): фиксированная зависимость «текущий ЦС −
    # порог» (порог может быть ±). На площадку уходит max(0, остаток ЦС − порог).
    # Не накапливается и не дрейфует — на любое движение ЦС меняется stock_on_hand,
    # а порог фиксирован. Приоритетнее устаревшего transmit_override.
    if product is not None and product.broadcast_offset is not None:
        return max(0, (product.stock_on_hand or 0) - product.broadcast_offset)
    if product is not None and product.transmit_override is not None:
        return max(0, product.transmit_override)

    reserve = product.reserve if product else 0
    # max(0, ...) заодно корректно отрабатывает ОТРИЦАТЕЛЬНЫЙ остаток из 1С
    # (пересортица, ещё не исправленная — это не баг): на площадку уходит 0,
    # а не отрицательное значение и не оверселл.
    available = max(0, quantity - reserve)

    setting = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account_id,
    ).first()
    if setting and setting.min_threshold and available <= setting.min_threshold:
        return 0
    return available


def run_dispatch_cycle(db: Session, clients: dict, active_accounts: list[PlatformAccount] | None = None) -> dict:
    """Раз в 30-60 секунд (см. планировщик): забирает накопленную очередь по
    каждому активному кабинету, схлопывает по товару (если за цикл пришло
    несколько изменений — берём только последнее), применяет минимальный
    порог, отправляет батчем."""

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
        # реально нужно отправить только последнее значение
        latest_by_uid = {}
        for item in pending:
            latest_by_uid[item.uid_1c] = item

        push_items = []
        uid_to_items = {}
        for uid_1c, item in latest_by_uid.items():
            target = _resolve_push_target(db, uid_1c, account.id)
            if target is None:
                item.status = DispatchStatus.error
                item.last_error = "нет баркода для отправки"
                continue
            barcode, external_id, article = target
            quantity = _quantity_to_send(db, uid_1c, account.id, item.quantity)
            push_items.append(StockPushItem(
                barcode=barcode, quantity=quantity, external_id=external_id, article=article,
            ))
            uid_to_items[barcode] = item

        result = {"ok": [], "errors": []}
        if push_items:
            result = client.push_stock(account.warehouse_id, push_items)

        ok_set = set(result.get("ok", []))
        for barcode, item in uid_to_items.items():
            item.attempts += 1
            if barcode in ok_set:
                item.status = DispatchStatus.sent
                item.sent_at = now_utc()
            else:
                item.status = DispatchStatus.error
                item.last_error = str(result.get("errors"))[:500]

        # Все элементы очереди по товару, кроме самого свежего — считаем
        # поглощёнными (не отправляем устаревшие промежуточные значения)
        for item in pending:
            if item.uid_1c not in latest_by_uid or latest_by_uid[item.uid_1c].id != item.id:
                item.status = DispatchStatus.sent
                item.last_error = "поглощено более новым изменением в этом цикле"

        db.commit()

        stats[account.name] = {
            "sent": len(ok_set), "errors": len(result.get("errors", [])),
            "queued": len(pending),
        }

    return stats

from app.models import (
    Product, Barcode, SyncSetting, SyncAnomaly, ProcessedOrder, DispatchQueueItem,
    FtpTask, Platform, OrderProcessStatus,
)
from app.workers.platform_clients.base import PlatformOrder
from app.workers.order_poller import poll_new_orders, poll_cancellations
from tests.factories import make_account


class FakeClient:
    """Минимальная реализация PlatformClient для тестов — без сети."""

    def __init__(self, new_orders=None, cancelled_orders=None, confirmed_orders=None):
        self._new_orders = new_orders or []
        self._cancelled_orders = cancelled_orders or []
        self._confirmed_orders = confirmed_orders or []

    def get_orders_awaiting_confirmation(self):
        return self._new_orders

    def get_cancelled_orders(self, order_ids):
        return [o for o in self._cancelled_orders if o.order_id in order_ids]

    def get_confirmed_orders(self, order_ids):
        return [o for o in self._confirmed_orders if o.order_id in order_ids]

    def push_stock(self, warehouse_id, items):
        return {"ok": [i.barcode for i in items], "errors": []}

    def get_catalog_items(self):
        return []


def _seed_product(db, uid="u1", stock=10, barcode="111", enabled_accounts=()):
    db.add(Product(uid_1c=uid, article="A1", name="Товар", stock_on_hand=stock))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    for account in enabled_accounts:
        db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))
    db.commit()


def test_new_order_decrements_stock_and_dispatches_to_other_accounts(db):
    source = make_account(db, name="Источник")
    other1 = make_account(db, name="Другой 1")
    other2 = make_account(db, name="Другой 2")
    _seed_product(db, stock=10, enabled_accounts=[source, other1, other2])

    client = FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="new"),
    ])

    stats = poll_new_orders(db, client, source, warehouse_pending="WB.Ожидает")

    assert stats["processed"] == 1

    product = db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 7  # 10 - 3

    po = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "o1").first()
    assert po is not None
    assert po.status == OrderProcessStatus.processed
    assert po.quantity == 3
    assert po.account_id == source.id

    # Рассылка — на other1 и other2, но НЕ на source (источник события)
    queued_accounts = {q.account_id for q in db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u1").all()}
    assert queued_accounts == {other1.id, other2.id}
    for q in db.query(DispatchQueueItem).all():
        assert q.quantity == 7

    task = db.query(FtpTask).filter(FtpTask.order_id == "o1").first()
    assert task is not None
    assert task.command == "CREATE_MOVEMENT"
    assert task.warehouse_to == "WB.Ожидает"
    assert task.quantity == 3
    assert task.account_id == source.id


def test_dispatch_reaches_other_wb_cabinet_not_just_other_platforms(db):
    """Три кабинета WB продают из одного и того же физического остатка —
    заказ в одном кабинете должен обновить ДРУГИЕ кабинеты той же площадки,
    не только Ozon/Kit."""
    wb1 = make_account(db, platform=Platform.wb, name="ИП Яворская")
    wb2 = make_account(db, platform=Platform.wb, name="ИП Ребрик")
    wb3 = make_account(db, platform=Platform.wb, name="ИП Караман")
    _seed_product(db, stock=10, enabled_accounts=[wb1, wb2, wb3])

    client = FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=1, raw_status="new"),
    ])
    poll_new_orders(db, client, wb1, "WB.Ожидает")

    queued_accounts = {q.account_id for q in db.query(DispatchQueueItem).all()}
    assert queued_accounts == {wb2.id, wb3.id}


def test_idempotent_second_poll_does_not_reprocess(db):
    account = make_account(db)
    _seed_product(db, enabled_accounts=[account])
    client = FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="new"),
    ])

    poll_new_orders(db, client, account, "WB.Ожидает")
    stats2 = poll_new_orders(db, client, account, "WB.Ожидает")

    assert stats2["already_processed"] == 1
    assert stats2["processed"] == 0

    product = db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 7


def test_order_on_disabled_account_skipped_silently(db):
    """Отбор по включённым товарам (гейт respect_enabled): заказ по товару с
    выключенной синхронизацией живой поллер пропускает ПОЛНОСТЬЮ и тихо — без
    движения в 1С, списания, рассылки, аномалии и отметки об обработке."""
    from app.models import ProcessedOrder
    account = make_account(db)
    _seed_product(db, stock=10, enabled_accounts=())  # синхронизация нигде не включена

    client = FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=2, raw_status="new"),
    ])
    stats = poll_new_orders(db, client, account, "WB.Ожидает")

    assert stats["skipped_disabled"] == 1
    assert stats["anomalies"] == 0
    assert db.query(SyncAnomaly).filter(SyncAnomaly.order_id == "o1").first() is None
    assert db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "o1").first() is None

    product = db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 10  # остаток не тронут


def test_unknown_barcode_creates_conflict_and_skips(db):
    account = make_account(db)
    client = FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="unknown-barcode", quantity=1, raw_status="new"),
    ])
    stats = poll_new_orders(db, client, account, "WB.Ожидает")

    assert stats["unmatched"] == 1
    assert db.query(ProcessedOrder).count() == 0

    from app.models import MappingConflict
    assert db.query(MappingConflict).filter(MappingConflict.barcode == "unknown-barcode").count() == 1


def test_cancellation_reverses_stock(db):
    a1 = make_account(db, name="Кабинет 1")
    a2 = make_account(db, name="Кабинет 2")
    _seed_product(db, stock=10, enabled_accounts=[a1, a2])

    client = FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=4, raw_status="new"),
    ])
    poll_new_orders(db, client, a1, "WB.Ожидает")

    product = db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 6

    client._cancelled_orders = [
        PlatformOrder(order_id="o1", barcode="", quantity=0, raw_status="cancel", is_cancellation=True),
    ]
    stats = poll_cancellations(db, client, a1)

    assert stats["reversed"] == 1
    db.refresh(product)
    assert product.stock_on_hand == 10

    po = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "o1").first()
    assert po.status == OrderProcessStatus.cancelled

    cancel_task = db.query(FtpTask).filter(FtpTask.command == "CANCEL_MOVEMENT", FtpTask.order_id == "o1").first()
    assert cancel_task is not None


def test_partial_refund_returns_only_refused_quantity(db):
    account = make_account(db, platform=Platform.kit, name="Kit")
    _seed_product(db, stock=10, enabled_accounts=[account])

    client = FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=5, raw_status="new"),
    ])
    poll_new_orders(db, client, account, "Kit.Ожидает")

    product = db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 5  # 10 - 5

    client._cancelled_orders = [
        PlatformOrder(order_id="o1", barcode="111", quantity=0, raw_status="PARTIAL_REFUND",
                      is_cancellation=True, is_partial_refund=True, refused_quantity=2),
    ]
    stats = poll_cancellations(db, client, account)

    assert stats["partial"] == 1
    db.refresh(product)
    assert product.stock_on_hand == 7  # вернулось только 2, не все 5

    po = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "o1").first()
    assert po.status == OrderProcessStatus.processed


def test_confirmation_moves_pending_to_platform_warehouse(db):
    """Подтверждение: статус -> confirmed, движение Ожидает -> Склад площадки,
    остаток НЕ меняется."""
    from app.workers.order_poller import poll_confirmations
    source = make_account(db, name="Караман")
    _seed_product(db, stock=10, enabled_accounts=[source])
    # приняли заказ
    poll_new_orders(db, FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="new"),
    ]), source, warehouse_pending="WB.Ожидает")
    stock_before = db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand

    client = FakeClient(confirmed_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="confirmed"),
    ])
    stats = poll_confirmations(db, client, source, "WB.Ожидает", "Склад WB")

    assert stats["confirmed"] == 1
    po = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "o1").first()
    assert po.status == OrderProcessStatus.confirmed
    # остаток не изменился подтверждением
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == stock_before
    # движение Ожидает -> Склад WB
    task = db.query(FtpTask).filter(FtpTask.command == "CONFIRM_MOVEMENT").first()
    assert task is not None
    assert task.warehouse_from == "WB.Ожидает"
    assert task.warehouse_to == "Склад WB"
    assert task.quantity == 3


def test_confirmation_idempotent_skips_already_confirmed(db):
    from app.workers.order_poller import poll_confirmations
    source = make_account(db, name="Караман")
    _seed_product(db, stock=10, enabled_accounts=[source])
    poll_new_orders(db, FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="new"),
    ]), source, warehouse_pending="WB.Ожидает")

    conf = FakeClient(confirmed_orders=[PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="confirmed")])
    poll_confirmations(db, conf, source, "WB.Ожидает", "Склад WB")
    # повторный прогон — уже confirmed, второго движения нет
    stats2 = poll_confirmations(db, conf, source, "WB.Ожидает", "Склад WB")
    assert stats2["confirmed"] == 0
    assert db.query(FtpTask).filter(FtpTask.command == "CONFIRM_MOVEMENT").count() == 1


def test_cancellation_ignores_already_confirmed_order(db):
    """Поток по требованию: подтверждённый заказ терминален. Возврат ПОСЛЕ
    подтверждения (после продажи) НЕ обрабатываем — отмена его не трогает."""
    source = make_account(db, name="Караман")
    _seed_product(db, stock=10, enabled_accounts=[source])
    poll_new_orders(db, FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="new"),
    ]), source, warehouse_pending="WB.Ожидает")
    from app.workers.order_poller import poll_confirmations
    poll_confirmations(db, FakeClient(confirmed_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="confirmed"),
    ]), source, "WB.Ожидает", "Склад WB")
    stock_after_confirm = db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand

    # площадка сообщает отмену уже подтверждённого — должно быть проигнорировано
    stats = poll_cancellations(db, FakeClient(cancelled_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="cancelled", is_cancellation=True),
    ]), source)

    assert stats["reversed"] == 0  # не реверсим
    po = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "o1").first()
    assert po.status == OrderProcessStatus.confirmed  # статус не изменился
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == stock_after_confirm  # остаток не вернулся


def test_confirm_movement_serialized_to_ftp_batch(db):
    from app.workers.ftp_channel import build_task_batch
    source = make_account(db, name="Караман")
    db.add(FtpTask(command="CONFIRM_MOVEMENT", barcode="111", warehouse_from="WB.Ожидает",
                   warehouse_to="Склад WB", quantity=3, order_id="o1", account_id=source.id))
    db.commit()
    filename, content = build_task_batch(db)
    assert "CONFIRM_MOVEMENT|111|WB.Ожидает|Склад WB|3|o1|wb" in content


def test_new_order_can_drive_stock_negative(db):
    """Отрицательный остаток легитимен (пересортица) — приём заказа не клампит
    в 0, а уводит в минус, как и сверка из 1С."""
    account = make_account(db)
    _seed_product(db, stock=1, enabled_accounts=[account])
    poll_new_orders(db, FakeClient(new_orders=[
        PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="new"),
    ]), account, "WB.Ожидает")
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == -2  # 1 - 3

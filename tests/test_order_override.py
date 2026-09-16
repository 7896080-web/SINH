"""Ручной «передаваемый остаток» (transmit_override) при заказах:
заказ вычитается ИЗ override, отмена возвращает; автоматический сценарий
(override не задан) — без изменений."""
from app.models import Product, Barcode, ProcessedOrder, SyncSetting
from app.workers.order_poller import process_new_order, process_cancellation
from app.workers.platform_clients.base import PlatformOrder
from tests.factories import make_account


def _seed(db, stock=5, override=None):
    acc = make_account(db, warehouse_id="wh-1")
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=stock,
                   transmit_override=override, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    # Синхронизация включена: иначе гейт отбора (respect_enabled в process_new_order)
    # тихо пропустит заказ и ничего не спишет.
    db.add(SyncSetting(uid_1c="u1", account_id=acc.id, enabled=True))
    db.commit()
    return acc


def test_order_decrements_manual_override(db):
    acc = _seed(db, stock=5, override=3)
    order = PlatformOrder(order_id="o1", barcode="111", quantity=1, raw_status="new")

    process_new_order(db, order, acc, "WB.Ожидает")

    p = db.query(Product).first()
    assert p.stock_on_hand == 4       # физический остаток списался
    assert p.transmit_override == 2   # заказ вычтен из ручной цифры: 3 − 1


def test_order_leaves_automatic_scenario_untouched(db):
    """override не задан — поведение как раньше: заказ учитывается через остаток,
    сам override остаётся None (автоматический расчёт)."""
    acc = _seed(db, stock=5, override=None)
    order = PlatformOrder(order_id="o1", barcode="111", quantity=2, raw_status="new")

    process_new_order(db, order, acc, "WB.Ожидает")

    p = db.query(Product).first()
    assert p.stock_on_hand == 3
    assert p.transmit_override is None


def test_order_override_clamped_at_zero(db):
    acc = _seed(db, stock=5, override=2)
    order = PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="new")

    process_new_order(db, order, acc, "WB.Ожидает")

    assert db.query(Product).first().transmit_override == 0  # не уходит в минус


def test_cancellation_restores_override(db):
    acc = _seed(db, stock=5, override=3)
    order = PlatformOrder(order_id="o1", barcode="111", quantity=1, raw_status="new")
    process_new_order(db, order, acc, "WB.Ожидает")
    assert db.query(Product).first().transmit_override == 2

    record = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "o1").first()
    cancelled = PlatformOrder(order_id="o1", barcode="111", quantity=1, raw_status="cancelled")

    process_cancellation(db, cancelled, record, acc)

    p = db.query(Product).first()
    assert p.stock_on_hand == 5        # остаток вернулся
    assert p.transmit_override == 3    # ручная цифра вернулась

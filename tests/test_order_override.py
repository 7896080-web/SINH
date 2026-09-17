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


def test_order_override_goes_below_zero_instead_of_losing_the_difference(db):
    """Находка 22: раньше здесь стоял кламп в ноль, и приём с отменой переставали
    быть обратными — ручная цифра дрейфовала вверх. Теперь «долг» хранится как
    минус; на площадку он всё равно превращается в 0."""
    acc = _seed(db, stock=5, override=2)
    order = PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="new")

    process_new_order(db, order, acc, "WB.Ожидает")

    assert db.query(Product).first().transmit_override == -1


def test_negative_override_still_sends_zero_to_the_platform(db):
    """Граница: минус живёт только в учёте. Наружу уходит ноль, а не минус."""
    from app.transmit import quantity_for_account, sku_quantity

    acc = _seed(db, stock=5, override=2)
    process_new_order(db, PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="new"),
                      acc, "WB.Ожидает")

    product = db.query(Product).first()
    assert sku_quantity(product) == 0
    assert quantity_for_account(db, "u1", acc.id, product.stock_on_hand) == 0


def test_order_and_cancellation_are_exactly_inverse(db):
    """Сценарий из аудита дословно: было 2, заказ на 3, затем отмена — ручная
    цифра обязана вернуться к 2, а не стать 3."""
    acc = _seed(db, stock=5, override=2)
    order = PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="new")
    process_new_order(db, order, acc, "WB.Ожидает")

    record = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "o1").first()
    cancelled = PlatformOrder(order_id="o1", barcode="111", quantity=3, raw_status="cancelled")
    process_cancellation(db, cancelled, record, acc)

    p = db.query(Product).first()
    assert p.transmit_override == 2     # ровно столько, сколько было
    assert p.stock_on_hand == 5


def test_reconciliation_moves_override_without_clamping(db):
    """Сверка двигает ручную цифру на складскую дельту — тоже без клампа, иначе
    дрейф вернулся бы с другой стороны."""
    from app.workers.reconciliation import run_reconciliation

    _seed(db, stock=5, override=1)

    run_reconciliation(db, {"111": 2})          # дельта склада −3

    p = db.query(Product).first()
    assert (p.stock_on_hand, p.transmit_override) == (2, -2)


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

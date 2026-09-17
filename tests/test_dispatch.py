import time

from app.models import Product, Barcode, SyncSetting, DispatchQueueItem, DispatchStatus
from app.workers.dispatch import run_dispatch_cycle
from tests.factories import make_account


class FakePlatformClient:
    def __init__(self):
        self.push_calls = []

    def push_stock(self, warehouse_id, items):
        self.push_calls.append((warehouse_id, list(items)))
        return {"ok": [i.barcode for i in items], "errors": []}


def test_dispatch_sends_only_latest_value_per_product(db):
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Товар", stock_on_hand=1))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    # Отметка кабинета обязательна: рассылка проверяет её так же, как интерфейс
    # (находка 7 — без этого запись уходила на кабинет со снятой галочкой).
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    for qty in (8, 6, 5):
        db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=qty, reason="order"))
        db.commit()
        time.sleep(0.001)

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    assert len(client.push_calls) == 1
    warehouse_id, items = client.push_calls[0]
    assert warehouse_id == "wh-1"
    assert len(items) == 1
    assert items[0].quantity == 5  # только самое свежее значение

    all_items = db.query(DispatchQueueItem).all()
    assert all(i.status == DispatchStatus.sent for i in all_items)


def test_dispatch_skips_account_without_client_or_warehouse(db):
    account = make_account(db, warehouse_id=None)  # склад не настроен
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Товар", stock_on_hand=1))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5, reason="order"))
    db.commit()

    stats = run_dispatch_cycle(db, {}, [account])  # нет клиента вообще

    item = db.query(DispatchQueueItem).first()
    assert item.status == DispatchStatus.pending
    assert stats == {}


def test_dispatch_marks_error_when_no_barcode(db):
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Товар без баркода", stock_on_hand=1))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5, reason="order"))
    db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    item = db.query(DispatchQueueItem).first()
    assert item.status == DispatchStatus.error
    assert "баркод" in item.last_error
    assert client.push_calls == []


def test_dispatch_applies_min_threshold_sends_zero_below_it(db):
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Товар", stock_on_hand=3))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True, min_threshold=5))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=3, reason="order"))
    db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert items[0].quantity == 0  # 3 <= порог 5 -> отправляем 0, не 3


def test_dispatch_sends_actual_quantity_above_threshold(db):
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Товар", stock_on_hand=8))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True, min_threshold=5))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=8, reason="order"))
    db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert items[0].quantity == 8  # выше порога — уходит как есть


def test_dispatch_zero_threshold_means_disabled(db):
    """min_threshold=0 (значение по умолчанию) — порог считается выключенным,
    даже нулевой физический остаток уходит как 0 честно, не блокируется логикой порога."""
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Товар", stock_on_hand=1))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True, min_threshold=0))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=1, reason="order"))
    db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert items[0].quantity == 1  # порог выключен — уходит фактическое значение


def test_dispatch_uses_active_accounts_from_db_when_not_provided(db):
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5, reason="order"))
    db.commit()

    client = FakePlatformClient()
    # active_accounts не передан явно — должен сам подтянуть активные из БД
    run_dispatch_cycle(db, {account.id: client})

    assert len(client.push_calls) == 1


def test_dispatch_enriches_platform_identifiers_from_catalog(db):
    """Рассылка достаёт из каталога кабинета offer_id (article) и variant_id
    (external_id) по баркоду — чтобы Ozon/Kit получили верный идентификатор."""
    from app.models import Platform, PlatformCatalogItem
    account = make_account(db, platform=Platform.ozon, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(PlatformCatalogItem(account_id=account.id, external_id="pid-9", barcode="111", article="OFR-9", name="Товар"))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5, reason="order"))
    db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert items[0].barcode == "111"
    assert items[0].external_id == "pid-9"
    assert items[0].article == "OFR-9"


def test_dispatch_without_catalog_leaves_identifiers_empty(db):
    """Каталог кабinета не загружен — идентификаторы пустые, но баркод есть
    (fallback: WB отработает, Ozon/Kit требуют загруженного каталога)."""
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5, reason="order"))
    db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert items[0].barcode == "111"
    assert items[0].external_id == ""
    assert items[0].article == ""


def test_dispatch_reserve_subtracts_from_quantity(db):
    """Резерв 2 при остатке 5 -> на площадку уходит 3."""
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Т", stock_on_hand=5, reserve=2))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5, reason="order"))
    db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert items[0].quantity == 3


def test_dispatch_reserve_ge_stock_sends_zero(db):
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Т", stock_on_hand=5, reserve=5))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5, reason="order"))
    db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert items[0].quantity == 0


def test_dispatch_reserve_applied_before_threshold(db):
    """Сначала резерв (5-2=3), затем порог: 3 <= порог 3 -> уходит 0."""
    from app.models import SyncSetting
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Т", stock_on_hand=5, reserve=2))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True, min_threshold=3))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5, reason="order"))
    db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert items[0].quantity == 0


# --- Фаза 1: трансляция, override, пауза кабинета ---

def test_quantity_broadcast_disabled_sends_zero(db):
    from app.workers.dispatch import _quantity_to_send
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A", name="N", stock_on_hand=5, reserve=0,
                   broadcast_enabled=False))
    db.commit()
    assert _quantity_to_send(db, "u1", account.id, 5) == 0


def test_quantity_override_wins_over_reserve_and_threshold(db):
    from app.workers.dispatch import _quantity_to_send
    account = make_account(db)
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A", name="N", stock_on_hand=5, reserve=2,
                   transmit_override=9))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True, min_threshold=100))
    db.commit()
    # 9 — ручной override; не 3 (=5−2) и не 0 (порог 100 не применяется к override)
    assert _quantity_to_send(db, "u1", account.id, 5) == 9


def test_quantity_override_zero_and_negative_clamped_to_zero(db):
    from app.workers.dispatch import _quantity_to_send
    account = make_account(db)
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A", name="N", stock_on_hand=5, transmit_override=0))
    db.commit()
    assert _quantity_to_send(db, "u1", account.id, 5) == 0

    p = db.query(Product).first()
    p.transmit_override = -3
    db.commit()
    assert _quantity_to_send(db, "u1", account.id, 5) == 0


def test_quantity_reserve_applies_when_no_override(db):
    from app.workers.dispatch import _quantity_to_send
    account = make_account(db)
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A", name="N", stock_on_hand=5, reserve=2))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    assert _quantity_to_send(db, "u1", account.id, 5) == 3  # override не задан → расчёт


def test_dispatch_skips_account_with_dispatch_disabled(db):
    account = make_account(db, warehouse_id="wh-1")
    account.dispatch_enabled = False  # пауза трансляции на кабинет
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Т", stock_on_hand=5))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5, reason="order"))
    db.commit()

    client = FakePlatformClient()
    stats = run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.push_calls == []            # ничего не отправлено
    item = db.query(DispatchQueueItem).first()
    assert item.status == DispatchStatus.pending  # очередь копится до включения
    assert stats == {}


def test_dispatch_negative_stock_from_1c_sends_zero(db):
    """1С может отдать отрицательный остаток (пересортица, ещё не исправлена) —
    это не баг. На площадку должно уйти 0, не отрицательное значение."""
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A1", name="Т", stock_on_hand=-3))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=-3, reason="reconciliation"))
    db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert items[0].quantity == 0


def test_quantity_broadcast_off_by_default_for_new_product(db):
    """Дефолт: новый товар (флаг трансляции не задан) НЕ транслируется —
    на площадку уходит 0, пока оператор явно не включит трансляцию."""
    from app.workers.dispatch import _quantity_to_send
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A", name="N", stock_on_hand=5))  # broadcast_enabled по умолчанию False
    db.commit()
    assert db.query(Product).first().broadcast_enabled is False
    assert _quantity_to_send(db, "u1", account.id, 5) == 0

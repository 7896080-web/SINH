from app.models import (
    Product, Barcode, SyncSetting, DispatchQueueItem, DispatchStatus, FtpTask, FtpTaskStatus,
)
from app.workers.order_poller import process_new_order, process_cancellation
from app.workers.dispatch import run_dispatch_cycle
from app.workers.ftp_channel import build_task_batch
from app.workers.platform_clients.base import PlatformOrder
from tests.factories import make_account


class FakeClientThatWouldSendAnything:
    """Клиент, который «отправляет» вообще всё, что ему передали — нужен,
    чтобы доказать: тестовые записи до него в принципе не доходят, а не
    что он сам их случайно отбраковывает."""

    def push_stock(self, warehouse_id, items):
        return {"ok": [i.barcode for i in items], "errors": []}


def _seed_product_with_two_accounts(db):
    source = make_account(db, name="Источник (тестируем его)")
    other = make_account(db, name="Другой реальный кабинет", warehouse_id="wh-real")

    # Транслируемый товар: тесты ниже про то, что СИМУЛЯЦИЯ не уходит на боевую
    # площадку. У нетранслируемого товара в очередь не попадает ничего вообще, и
    # проверять на нём изоляцию бессмысленно — она получилась бы сама собой.
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10,
                   broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=source.id, enabled=True))
    db.add(SyncSetting(uid_1c="u1", account_id=other.id, enabled=True))
    db.commit()
    return source, other


def test_simulated_order_dispatch_item_is_tagged_test(db):
    source, other = _seed_product_with_two_accounts(db)
    order = PlatformOrder(order_id="TEST-abc123", barcode="111", quantity=3, raw_status="test")

    process_new_order(db, order, source, "WB.Ожидает", is_test=True)

    item = db.query(DispatchQueueItem).filter(DispatchQueueItem.account_id == other.id).first()
    assert item is not None
    assert item.is_test is True


def test_dispatch_cycle_never_sends_test_items_even_with_eager_client(db):
    """Ключевой тест: реальный воркер рассылки, запущенный ПАРАЛЛЕЛЬНО с
    тестом (как и будет в проде), не должен ничего отправить на площадку
    по тестовому заказу — даже если клиент готов «отправить всё»."""
    source, other = _seed_product_with_two_accounts(db)
    order = PlatformOrder(order_id="TEST-abc123", barcode="111", quantity=3, raw_status="test")
    process_new_order(db, order, source, "WB.Ожидает", is_test=True)

    client = FakeClientThatWouldSendAnything()
    stats = run_dispatch_cycle(db, {other.id: client}, [other])

    # Ничего не отправлено — тестовая запись даже не попала в выборку
    assert stats == {}

    item = db.query(DispatchQueueItem).filter(DispatchQueueItem.account_id == other.id).first()
    assert item.status == DispatchStatus.pending  # так и осталась нетронутой


def test_ftp_task_from_simulation_never_enters_real_batch_file(db):
    """Ключевой тест: реальная выгрузка задания в 1С, запущенная параллельно
    с тестом, не должна включить тестовое перемещение в файл для 1С."""
    source, other = _seed_product_with_two_accounts(db)
    order = PlatformOrder(order_id="TEST-abc123", barcode="111", quantity=3, raw_status="test")
    result = process_new_order(db, order, source, "WB.Ожидает", is_test=True)

    task = db.query(FtpTask).filter(FtpTask.id == result["ftp_task_id"]).first()
    assert task.is_test is True
    assert task.status == FtpTaskStatus.pending

    batch = build_task_batch(db)
    assert batch is None  # нечего отправлять — единственное задание тестовое

    db.refresh(task)
    assert task.status == FtpTaskStatus.pending  # не тронуто, не помечено как "sent"


def test_ftp_task_real_and_test_coexist_only_real_goes_out(db):
    """Реальное задание рядом с тестовым — в файл должно попасть только
    реальное, тестовое остаётся нетронутым."""
    source, other = _seed_product_with_two_accounts(db)
    real_order = PlatformOrder(order_id="REAL-order-1", barcode="111", quantity=1, raw_status="new")
    process_new_order(db, real_order, source, "WB.Ожидает", is_test=False)

    test_order = PlatformOrder(order_id="TEST-xyz789", barcode="111", quantity=99, raw_status="test")
    process_new_order(db, test_order, other, "Ozon.Ожидает", is_test=True)

    filename, content = build_task_batch(db)
    assert "REAL-order-1" in content
    assert "TEST-xyz789" not in content
    assert "99" not in content  # тестовое количество нигде не всплыло


def test_reconciliation_in_flight_ignores_test_tasks(db):
    from app.workers.reconciliation import _in_flight_adjustment

    source, other = _seed_product_with_two_accounts(db)
    test_order = PlatformOrder(order_id="TEST-inflight", barcode="111", quantity=50, raw_status="test")
    process_new_order(db, test_order, source, "WB.Ожидает", is_test=True)

    # Тестовое задание реально стоит в очереди (status=pending, quantity=50),
    # но раз оно никогда не уйдёт в 1С по-настоящему — сверка не должна
    # считать его "в пути". Если бы фильтр не работал, тут было бы 50.
    adjustment = _in_flight_adjustment(db, "u1")
    assert adjustment == 0


def test_reconciliation_in_flight_counts_real_task_not_test_one(db):
    from app.workers.reconciliation import _in_flight_adjustment

    source, other = _seed_product_with_two_accounts(db)

    real_order = PlatformOrder(order_id="REAL-inflight", barcode="111", quantity=4, raw_status="new")
    process_new_order(db, real_order, source, "WB.Ожидает", is_test=False)

    test_order = PlatformOrder(order_id="TEST-inflight2", barcode="111", quantity=999, raw_status="test")
    process_new_order(db, test_order, other, "Ozon.Ожидает", is_test=True)

    # Должно учесть только реальное задание (4), тестовое (999) — игнорировать
    adjustment = _in_flight_adjustment(db, "u1")
    assert adjustment == 4


def test_cleanup_removes_test_dispatch_items_across_fanout_accounts(db):
    """Симуляция рассылает тестовую запись в ДРУГИЕ кабинеты — очистка
    должна убрать их все, не только у кабинета, с которым тестировали."""
    source, other = _seed_product_with_two_accounts(db)
    order = PlatformOrder(order_id="TEST-fanout", barcode="111", quantity=2, raw_status="test")
    process_new_order(db, order, source, "WB.Ожидает", is_test=True)

    assert db.query(DispatchQueueItem).filter(DispatchQueueItem.account_id == other.id).count() == 1

    # Симулируем то, что делает testing.py при очистке (без HTTP-слоя)
    db.query(DispatchQueueItem).filter(
        DispatchQueueItem.uid_1c == "u1", DispatchQueueItem.is_test.is_(True),
    ).delete(synchronize_session=False)
    db.commit()

    assert db.query(DispatchQueueItem).filter(DispatchQueueItem.account_id == other.id).count() == 0


def test_cancellation_simulation_also_tagged_test(db):
    from app.models import ProcessedOrder

    source, other = _seed_product_with_two_accounts(db)
    order = PlatformOrder(order_id="TEST-cancel1", barcode="111", quantity=4, raw_status="test")
    process_new_order(db, order, source, "WB.Ожидает", is_test=True)

    record = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "TEST-cancel1").first()
    cancelled = PlatformOrder(order_id="TEST-cancel1", barcode="", quantity=0,
                               raw_status="test_cancel", is_cancellation=True)
    result = process_cancellation(db, cancelled, record, source, is_test=True)

    task = db.query(FtpTask).filter(FtpTask.id == result["ftp_task_id"]).first()
    assert task.is_test is True

    dispatch_items = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == other.id, DispatchQueueItem.reason == "cancel",
    ).all()
    assert all(i.is_test for i in dispatch_items)


def test_test_anomaly_excluded_from_anomalies_page_counts(db):
    """Тестовая аномалия (заказ на кабинет без синхронизации) не должна
    попадать ни в счётчик на странице «Аномалии», ни в сводку «Диагностики»."""
    source, other = _seed_product_with_two_accounts(db)
    # source и other оба enabled в фикстуре — сделаем ещё один без sync
    no_sync_account = make_account(db, name="Без синхронизации")
    order = PlatformOrder(order_id="TEST-anomaly1", barcode="111", quantity=1, raw_status="test")

    process_new_order(db, order, no_sync_account, "WB.Ожидает", is_test=True)

    from app.models import SyncAnomaly, AnomalyStatus
    anomaly = db.query(SyncAnomaly).filter(SyncAnomaly.order_id == "TEST-anomaly1").first()
    assert anomaly is not None
    assert anomaly.is_test is True

    from app.routers.anomalies import _load_grouped_anomalies
    rows = _load_grouped_anomalies(db, AnomalyStatus.new.value, "", "", "")
    assert not any(r.uid_1c == "u1" and r.account_id == no_sync_account.id for r in rows)

def _seed(web_db):
    from app.models import Product, Barcode, PlatformAccount, SyncSetting

    a1 = PlatformAccount(platform="wb", name="Кабинет 1", warehouse_id="wh-1")
    a2 = PlatformAccount(platform="ozon", name="Кабинет 2", warehouse_id="wh-2")
    web_db.add_all([a1, a2])
    web_db.commit()
    web_db.refresh(a1)
    web_db.refresh(a2)

    web_db.add(Product(broadcast_enabled=True, uid_1c="u1", article="ART-1", name="Тестовый товар", stock_on_hand=10))
    web_db.add(Barcode(barcode="111", uid_1c="u1"))
    web_db.add(SyncSetting(uid_1c="u1", account_id=a1.id, enabled=True))
    web_db.add(SyncSetting(uid_1c="u1", account_id=a2.id, enabled=True))
    web_db.commit()
    return a1, a2


def test_testing_page_empty_selection(logged_in_client, web_db):
    r = logged_in_client.get("/testing")
    assert r.status_code == 200
    assert "выберите" in r.text


def test_testing_page_shows_product_state(logged_in_client, web_db):
    a1, a2 = _seed(web_db)
    r = logged_in_client.get(f"/testing?uid_1c=u1&account_id={a1.id}")
    assert "Тестовый товар" in r.text
    assert "111" in r.text
    assert "включена" in r.text


def test_simulate_order_decrements_stock_and_creates_test_order(logged_in_client, web_db):
    a1, a2 = _seed(web_db)

    r = logged_in_client.post(
        "/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 3},
        follow_redirects=False,
    )
    assert r.status_code == 303

    from app.models import Product, ProcessedOrder
    product = web_db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 7

    order = web_db.query(ProcessedOrder).filter(ProcessedOrder.uid_1c == "u1").first()
    assert order is not None
    assert order.order_id.startswith("TEST-")
    assert order.quantity == 3


def test_simulate_order_dispatches_to_other_account(logged_in_client, web_db):
    a1, a2 = _seed(web_db)

    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 2})

    from app.models import DispatchQueueItem
    queued = web_db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u1").all()
    assert any(q.account_id == a2.id for q in queued)
    assert not any(q.account_id == a1.id for q in queued)  # не на источник


def test_simulate_order_dispatch_item_tagged_test_and_not_sendable(logged_in_client, web_db):
    """Тот самый критичный момент: запись должна быть помечена как тестовая,
    чтобы реальный dispatch.py её не подхватил."""
    a1, a2 = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 2})

    from app.models import DispatchQueueItem
    item = web_db.query(DispatchQueueItem).filter(DispatchQueueItem.account_id == a2.id).first()
    assert item.is_test is True


def test_simulate_order_creates_ftp_task(logged_in_client, web_db):
    a1, a2 = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 1})

    from app.models import FtpTask
    task = db_task = web_db.query(FtpTask).filter(FtpTask.account_id == a1.id).first()
    assert task is not None
    assert task.command == "CREATE_MOVEMENT"
    assert task.order_id.startswith("TEST-")
    assert task.is_test is True  # не должно попасть в реальный файл для 1С


def test_testing_page_lists_test_orders_and_allows_cancel(logged_in_client, web_db):
    a1, a2 = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 2})

    r = logged_in_client.get(f"/testing?uid_1c=u1&account_id={a1.id}")
    assert "TEST-" in r.text
    assert "Симулировать отмену" in r.text


def test_simulate_cancel_reverses_stock(logged_in_client, web_db):
    a1, a2 = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 4})

    from app.models import Product, ProcessedOrder
    product = web_db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 6

    order = web_db.query(ProcessedOrder).filter(ProcessedOrder.uid_1c == "u1").first()

    r = logged_in_client.post(
        "/testing/simulate-cancel",
        data={"uid_1c": "u1", "account_id": a1.id, "order_id": order.order_id},
        follow_redirects=False,
    )
    assert r.status_code == 303

    web_db.refresh(product)
    assert product.stock_on_hand == 10  # вернулось к исходному

    web_db.refresh(order)
    assert order.status.value == "cancelled"


def test_cleanup_restores_stock_for_open_test_order(logged_in_client, web_db):
    a1, a2 = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 5})

    from app.models import Product
    product = web_db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 5

    r = logged_in_client.post("/testing/cleanup", data={"uid_1c": "u1", "account_id": a1.id}, follow_redirects=False)
    assert r.status_code == 303

    web_db.refresh(product)
    assert product.stock_on_hand == 10  # восстановлено


def test_cleanup_removes_test_records(logged_in_client, web_db):
    a1, a2 = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 1})

    logged_in_client.post("/testing/cleanup", data={"uid_1c": "u1", "account_id": a1.id})

    from app.models import ProcessedOrder, FtpTask
    assert web_db.query(ProcessedOrder).filter(ProcessedOrder.order_id.like("TEST-%")).count() == 0
    assert web_db.query(FtpTask).filter(FtpTask.order_id.like("TEST-%")).count() == 0


def test_cleanup_does_not_touch_real_order_dispatch_items(logged_in_client, web_db):
    """Ключевая проверка безопасности: реальный (не тестовый) элемент в
    очереди рассылки не должен пострадать при очистке тестовых данных,
    а тестовый — должен быть удалён (теперь это можно делать точно,
    благодаря флагу is_test)."""
    a1, a2 = _seed(web_db)

    from app.models import DispatchQueueItem
    real_item = DispatchQueueItem(uid_1c="u1", account_id=a2.id, quantity=999, reason="order", is_test=False)
    web_db.add(real_item)
    web_db.commit()
    real_item_id = real_item.id

    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 2})
    test_item = web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == a2.id, DispatchQueueItem.is_test.is_(True),
    ).first()
    assert test_item is not None

    logged_in_client.post("/testing/cleanup", data={"uid_1c": "u1", "account_id": a1.id})

    # Реальная запись осталась нетронутой (find by content, not raw id —
    # SQLite может переиспользовать освободившийся id для новой строки,
    # в отличие от Postgres, поэтому сравнивать по id после delete+insert
    # в одной транзакции ненадёжно)
    assert web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.id == real_item_id, DispatchQueueItem.quantity == 999,
    ).count() == 1
    # Тестовых записей для этого товара больше нет вообще
    assert web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.uid_1c == "u1", DispatchQueueItem.is_test.is_(True),
    ).count() == 0


def test_cleanup_enqueues_corrective_dispatch_with_restored_value(logged_in_client, web_db):
    a1, a2 = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 3})
    logged_in_client.post("/testing/cleanup", data={"uid_1c": "u1", "account_id": a1.id})

    from app.models import DispatchQueueItem
    corrective = db_q = web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.uid_1c == "u1", DispatchQueueItem.reason == "test_cleanup",
    ).all()
    assert len(corrective) > 0
    assert all(item.quantity == 10 for item in corrective)  # восстановленное значение


def test_push_stock_without_barcode_shows_warning(logged_in_client, web_db):
    from app.models import Product, PlatformAccount

    account = PlatformAccount(platform="wb", name="Кабинет", warehouse_id="wh-1")
    web_db.add(account)
    web_db.add(Product(broadcast_enabled=True, uid_1c="u2", article="A2", name="Без баркода", stock_on_hand=1))
    web_db.commit()
    web_db.refresh(account)

    r = logged_in_client.post(
        "/testing/push-stock", data={"uid_1c": "u2", "account_id": account.id}, follow_redirects=False,
    )
    assert r.status_code == 303

    r2 = logged_in_client.get(f"/testing?uid_1c=u2&account_id={account.id}")
    assert "нет баркода" in r2.text or "не сопоставлен" in r2.text


def test_push_stock_success_via_fake_client(logged_in_client, web_db, monkeypatch):
    import app.routers.testing as testing_router

    a1, a2 = _seed(web_db)

    class FakeClient:
        def push_stock(self, warehouse_id, items):
            return {"ok": [i.barcode for i in items], "errors": []}

    monkeypatch.setattr(testing_router, "build_client", lambda db, account_id: FakeClient())

    r = logged_in_client.post(
        "/testing/push-stock", data={"uid_1c": "u1", "account_id": a1.id}, follow_redirects=False,
    )
    assert r.status_code == 303

    r2 = logged_in_client.get(f"/testing?uid_1c=u1&account_id={a1.id}")
    assert "успешно отправлен" in r2.text


def test_live_log_records_each_action(logged_in_client, web_db):
    a1, a2 = _seed(web_db)

    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 2})

    from app.models import TestLogEntry
    entries = web_db.query(TestLogEntry).filter(
        TestLogEntry.uid_1c == "u1", TestLogEntry.account_id == a1.id,
    ).all()
    assert len(entries) >= 2  # начало действия + результат
    assert any(e.action == "simulate_order" for e in entries)


def test_live_log_visible_on_page(logged_in_client, web_db):
    a1, a2 = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 1})

    r = logged_in_client.get(f"/testing?uid_1c=u1&account_id={a1.id}")
    assert "Живой журнал" in r.text
    assert "simulate_order" in r.text


def test_live_log_rows_endpoint_updates_live(logged_in_client, web_db):
    a1, a2 = _seed(web_db)

    r0 = logged_in_client.get(f"/testing/log-rows?uid_1c=u1&account_id={a1.id}")
    assert "Журнал пуст" in r0.text

    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 1})

    r1 = logged_in_client.get(f"/testing/log-rows?uid_1c=u1&account_id={a1.id}")
    assert "simulate_order" in r1.text
    assert "Журнал пуст" not in r1.text


def test_live_log_records_cleanup_and_cancel(logged_in_client, web_db):
    a1, a2 = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 2})

    from app.models import ProcessedOrder, TestLogEntry
    order = web_db.query(ProcessedOrder).filter(ProcessedOrder.uid_1c == "u1").first()

    logged_in_client.post(
        "/testing/simulate-cancel", data={"uid_1c": "u1", "account_id": a1.id, "order_id": order.order_id},
    )
    logged_in_client.post("/testing/cleanup", data={"uid_1c": "u1", "account_id": a1.id})

    actions = {e.action for e in web_db.query(TestLogEntry).filter(TestLogEntry.uid_1c == "u1").all()}
    assert "simulate_cancel" in actions
    assert "cleanup" in actions


def test_live_log_scoped_per_account_not_leaked(logged_in_client, web_db):
    """Журнал одного кабинета не должен показываться при выборе другого."""
    a1, a2 = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 1})

    r = logged_in_client.get(f"/testing/log-rows?uid_1c=u1&account_id={a2.id}")
    assert "simulate_order" not in r.text


def test_push_stock_applies_reserve_and_resolves_identifiers(logged_in_client, web_db, monkeypatch):
    """/testing идёт тем же путём, что рассылка: вычитает резерв и берёт
    offer_id (article) из каталога, а не баркод."""
    import app.routers.testing as testing_router
    from app.models import Product, Barcode, PlatformAccount, PlatformCatalogItem

    oz = PlatformAccount(platform="ozon", name="ОЗОН", warehouse_id="wh")
    web_db.add(oz); web_db.commit(); web_db.refresh(oz)
    web_db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A", name="Т", stock_on_hand=5, reserve=2))
    web_db.add(Barcode(barcode="111", uid_1c="u1"))
    web_db.add(PlatformCatalogItem(account_id=oz.id, external_id="pid-9", barcode="111", article="OFR-9", name="Т"))
    web_db.commit()

    captured = {}

    class FakeClient:
        def push_stock(self, warehouse_id, items):
            captured["items"] = list(items)
            return {"ok": [i.barcode for i in items], "errors": []}

    monkeypatch.setattr(testing_router, "build_client", lambda db, account_id: FakeClient())
    logged_in_client.post("/testing/push-stock", data={"uid_1c": "u1", "account_id": oz.id})

    item = captured["items"][0]
    assert item.quantity == 3          # 5 − резерв 2
    assert item.article == "OFR-9"     # offer_id из каталога, не баркод
    assert item.external_id == "pid-9"


def test_push_stock_negative_stock_clamped_to_zero(logged_in_client, web_db, monkeypatch):
    import app.routers.testing as testing_router
    from app.models import Product, Barcode, PlatformAccount

    wb = PlatformAccount(platform="wb", name="WB", warehouse_id="wh")
    web_db.add(wb); web_db.commit(); web_db.refresh(wb)
    web_db.add(Product(broadcast_enabled=True, uid_1c="u1", article="A", name="Т", stock_on_hand=-3))  # пересортица
    web_db.add(Barcode(barcode="111", uid_1c="u1"))
    web_db.commit()

    captured = {}

    class FakeClient:
        def push_stock(self, warehouse_id, items):
            captured["items"] = list(items)
            return {"ok": [i.barcode for i in items], "errors": []}

    monkeypatch.setattr(testing_router, "build_client", lambda db, account_id: FakeClient())
    logged_in_client.post("/testing/push-stock", data={"uid_1c": "u1", "account_id": wb.id})

    assert captured["items"][0].quantity == 0  # отрицательный -> 0


def test_simulate_confirm_moves_to_platform_warehouse(logged_in_client, web_db):
    from app.models import Product, ProcessedOrder, OrderProcessStatus, FtpTask
    a1, a2 = _seed(web_db)

    # приняли тестовый заказ
    logged_in_client.post("/testing/simulate-order", data={"uid_1c": "u1", "account_id": a1.id, "quantity": 2})
    order = web_db.query(ProcessedOrder).filter(ProcessedOrder.account_id == a1.id).first()
    stock_before = web_db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand

    logged_in_client.post("/testing/simulate-confirm",
                          data={"uid_1c": "u1", "account_id": a1.id, "order_id": order.order_id})

    web_db.expire_all()
    po = web_db.query(ProcessedOrder).filter(ProcessedOrder.order_id == order.order_id).first()
    assert po.status == OrderProcessStatus.confirmed
    # остаток подтверждением не меняется
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == stock_before
    # Вариант А: резерв уже в основном складе площадки (pending == sold), поэтому
    # при подтверждении отдельное перемещение «Ожидает → Склад» НЕ создаётся —
    # только фиксируется статус confirmed (см. process_confirmation).
    task = web_db.query(FtpTask).filter(FtpTask.command == "CONFIRM_MOVEMENT").first()
    assert task is None

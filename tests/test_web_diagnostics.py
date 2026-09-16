from app.timeutils import now_utc
def _seed_account(web_db, platform="wb", name="ИП Яворская", warehouse_id="wh-1"):
    from app.models import PlatformAccount

    account = PlatformAccount(platform=platform, name=name, warehouse_id=warehouse_id)
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    return account


def test_diagnostics_page_empty_state(logged_in_client):
    r = logged_in_client.get("/diagnostics")
    assert r.status_code == 200
    assert "Кабинетов ещё нет" in r.text


def test_diagnostics_page_shows_account_and_queue_counts(logged_in_client, web_db):
    from app.models import DispatchQueueItem, Product, Barcode

    account = _seed_account(web_db)
    web_db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    web_db.add(Barcode(barcode="111", uid_1c="u1"))
    web_db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5, reason="order"))
    web_db.commit()

    r = logged_in_client.get("/diagnostics")
    assert "ИП Яворская" in r.text
    assert "WB" in r.text


def test_test_connection_without_credentials_shows_error(logged_in_client, web_db):
    account = _seed_account(web_db)

    r = logged_in_client.post(f"/diagnostics/accounts/{account.id}/test-connection", follow_redirects=False)
    assert r.status_code == 303

    r2 = logged_in_client.get("/diagnostics")
    assert "не заполнены ключи" in r2.text

    web_db.refresh(account)
    assert account.last_connection_ok is False
    assert account.last_connection_check_at is not None


def test_test_connection_success_via_fake_client(logged_in_client, web_db, monkeypatch):
    import app.routers.diagnostics as diag_router

    account = _seed_account(web_db)

    class FakeClient:
        def test_connection(self):
            return True, "Всё хорошо"

    monkeypatch.setattr(diag_router, "build_client", lambda db, account_id: FakeClient())

    r = logged_in_client.post(f"/diagnostics/accounts/{account.id}/test-connection", follow_redirects=False)
    assert r.status_code == 303

    r2 = logged_in_client.get("/diagnostics")
    assert "Всё хорошо" in r2.text

    web_db.refresh(account)
    assert account.last_connection_ok is True


def test_poll_now_without_credentials_shows_friendly_message(logged_in_client, web_db):
    account = _seed_account(web_db)

    r = logged_in_client.post(f"/diagnostics/accounts/{account.id}/poll-now", follow_redirects=False)
    assert r.status_code == 303

    r2 = logged_in_client.get("/diagnostics")
    assert "не заполнены ключи" in r2.text


def test_poll_now_success_via_fake_client(logged_in_client, web_db, monkeypatch):
    import app.routers.diagnostics as diag_router
    from app.workers.platform_clients.base import PlatformOrder

    account = _seed_account(web_db)

    class FakeClient:
        def get_orders_awaiting_confirmation(self):
            return []

        def get_cancelled_orders(self, order_ids):
            return []

    monkeypatch.setattr(diag_router, "build_client", lambda db, account_id: FakeClient())

    r = logged_in_client.post(f"/diagnostics/accounts/{account.id}/poll-now", follow_redirects=False)
    assert r.status_code == 303

    r2 = logged_in_client.get("/diagnostics")
    assert "Новые:" in r2.text


def test_reset_failures_button(logged_in_client, web_db):
    account = _seed_account(web_db)
    account.consecutive_failures = 3
    account.last_error = "что-то сломалось"
    web_db.commit()

    r = logged_in_client.get("/diagnostics")
    assert "сбоев подряд: 3" in r.text

    logged_in_client.post(f"/diagnostics/accounts/{account.id}/reset-failures")

    web_db.refresh(account)
    assert account.consecutive_failures == 0
    assert account.last_error is None

    r2 = logged_in_client.get("/diagnostics")
    assert "сбоев подряд" not in r2.text


def test_disabled_account_shown_with_badge(logged_in_client, web_db):
    account = _seed_account(web_db)
    account.is_active = False
    web_db.commit()

    r = logged_in_client.get("/diagnostics")
    assert "отключён" in r.text


def test_shared_workers_heartbeat_shown(logged_in_client, web_db):
    from app.models import WorkerHeartbeat

    web_db.add(WorkerHeartbeat(worker_name="dispatch", last_run_at=now_utc(), last_success=True))
    web_db.commit()

    r = logged_in_client.get("/diagnostics")
    assert "dispatch" in r.text

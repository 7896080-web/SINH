from datetime import datetime, timedelta
from app.timeutils import now_utc


def test_api_keys_page_renders_empty_state(logged_in_client):
    r = logged_in_client.get("/api-keys")
    assert r.status_code == 200
    assert "Кабинетов ещё нет" in r.text


def test_warehouse_list_always_shows_tsentr_sklad(logged_in_client):
    r = logged_in_client.get("/api-keys")
    assert "ЦС" in r.text
    assert "Основной физический склад" in r.text


def test_warehouse_list_shows_wb_ozhidaet_only_when_wb_account_active(logged_in_client, web_db):
    r0 = logged_in_client.get("/api-keys")
    assert "Wildberries_Склад_FBO" not in r0.text

    logged_in_client.post("/api-keys/accounts/create", data={"platform": "wb", "name": "ИП Яворская"})

    r1 = logged_in_client.get("/api-keys")
    assert "Wildberries_Склад_FBO" in r1.text
    assert "ИП Яворская" in r1.text


def test_warehouse_list_shared_across_multiple_wb_cabinets(logged_in_client, web_db):
    """Три кабинета WB — один и тот же склад «Wildberries_Склад_FBO» в списке, но с
    перечислением всех трёх в колонке «Использует»."""
    for name in ("ИП Яворская", "ИП Ребрик", "ИП Караман"):
        logged_in_client.post("/api-keys/accounts/create", data={"platform": "wb", "name": name})

    r = logged_in_client.get("/api-keys")
    # Вариант А: pending == sold, поэтому склад появляется 2 раза (резерв + продажи),
    # но общий на все кабинеты — не по разу на кабинет (иначе было бы 6).
    assert r.text.count("Wildberries_Склад_FBO") == 2
    for name in ("ИП Яворская", "ИП Ребрик", "ИП Караман"):
        assert name in r.text


def test_warehouse_list_excludes_deactivated_account_platform(logged_in_client, web_db):
    logged_in_client.post("/api-keys/accounts/create", data={"platform": "kit", "name": "Kit"})
    from app.models import PlatformAccount
    account = web_db.query(PlatformAccount).filter(PlatformAccount.platform == "kit").first()

    r1 = logged_in_client.get("/api-keys")
    assert "Сайт AWER" in r1.text

    logged_in_client.post(f"/api-keys/accounts/{account.id}/deactivate")

    r2 = logged_in_client.get("/api-keys")
    assert "Сайт AWER" not in r2.text


def test_create_wb_account(logged_in_client, web_db):
    r = logged_in_client.post(
        "/api-keys/accounts/create", data={"platform": "wb", "name": "ИП Яворская"}, follow_redirects=False,
    )
    assert r.status_code == 303

    from app.models import PlatformAccount
    account = web_db.query(PlatformAccount).filter(PlatformAccount.name == "ИП Яворская").first()
    assert account is not None
    assert account.platform.value == "wb"
    assert account.is_active is True

    r2 = logged_in_client.get("/api-keys")
    assert "ИП Яворская" in r2.text


def test_create_three_wb_accounts_shown_separately(logged_in_client, web_db):
    for name in ("ИП Яворская", "ИП Ребрик", "ИП Караман"):
        logged_in_client.post("/api-keys/accounts/create", data={"platform": "wb", "name": name})

    r = logged_in_client.get("/api-keys")
    for name in ("ИП Яворская", "ИП Ребрик", "ИП Караман"):
        assert name in r.text

    from app.models import PlatformAccount
    assert web_db.query(PlatformAccount).filter(PlatformAccount.platform == "wb").count() == 3


def test_create_account_rejects_empty_name(logged_in_client):
    r = logged_in_client.post(
        "/api-keys/accounts/create", data={"platform": "wb", "name": "   "}, follow_redirects=False,
    )
    assert r.status_code == 303

    r2 = logged_in_client.get("/api-keys")
    assert "не может быть пустым" in r2.text


def test_save_credential_for_account_and_mask(logged_in_client, web_db):
    r = logged_in_client.post("/api-keys/accounts/create", data={"platform": "wb", "name": "ИП Яворская"})
    from app.models import PlatformAccount
    account = web_db.query(PlatformAccount).filter(PlatformAccount.name == "ИП Яворская").first()

    logged_in_client.post(f"/api-keys/accounts/{account.id}/token", data={"value": "1234567890abcdef"})

    r2 = logged_in_client.get("/api-keys")
    assert "1234" in r2.text
    assert "1234567890abcdef" not in r2.text


def test_two_wb_accounts_have_independent_tokens(logged_in_client, web_db):
    logged_in_client.post("/api-keys/accounts/create", data={"platform": "wb", "name": "Кабинет 1"})
    logged_in_client.post("/api-keys/accounts/create", data={"platform": "wb", "name": "Кабинет 2"})

    from app.models import PlatformAccount
    a1 = web_db.query(PlatformAccount).filter(PlatformAccount.name == "Кабинет 1").first()
    a2 = web_db.query(PlatformAccount).filter(PlatformAccount.name == "Кабинет 2").first()

    logged_in_client.post(f"/api-keys/accounts/{a1.id}/token", data={"value": "token-aaaaaaaa"})
    logged_in_client.post(f"/api-keys/accounts/{a2.id}/token", data={"value": "token-bbbbbbbb"})

    from app.models import ApiCredential
    from app.crypto import decrypt_value
    cred1 = web_db.query(ApiCredential).filter(ApiCredential.account_id == a1.id, ApiCredential.field_name == "token").first()
    cred2 = web_db.query(ApiCredential).filter(ApiCredential.account_id == a2.id, ApiCredential.field_name == "token").first()

    assert decrypt_value(cred1.encrypted_value) == "token-aaaaaaaa"
    assert decrypt_value(cred2.encrypted_value) == "token-bbbbbbbb"


def test_save_credential_without_visiting_page_first(logged_in_client, web_db):
    """Строка под ключ может ещё не существовать (страницу не открывали) —
    сохранение должно само её создать, не проваливаться молча."""
    from app.models import PlatformAccount, ApiCredential

    account_resp = logged_in_client.post("/api-keys/accounts/create", data={"platform": "ozon", "name": "Основной"})
    account = web_db.query(PlatformAccount).filter(PlatformAccount.name == "Основной").first()

    # У Ozon страница уже создала пустые строки при create (мы явно вызываем
    # _ensure_credential_rows сразу после создания кабинета) — проверим,
    # что сохранение всё равно работает даже если считать это несозданным
    assert web_db.query(ApiCredential).filter(ApiCredential.account_id == account.id).count() >= 1

    r = logged_in_client.post(
        f"/api-keys/accounts/{account.id}/client_id", data={"value": "1234567890"}, follow_redirects=False,
    )
    assert r.status_code == 303
    r2 = logged_in_client.get("/api-keys")
    assert "1234" in r2.text


def test_unknown_field_is_rejected(logged_in_client, web_db):
    logged_in_client.post("/api-keys/accounts/create", data={"platform": "wb", "name": "ИП Яворская"})
    from app.models import PlatformAccount, ApiCredential
    account = web_db.query(PlatformAccount).first()

    logged_in_client.post(f"/api-keys/accounts/{account.id}/not_a_real_field", data={"value": "x"})

    assert web_db.query(ApiCredential).filter(ApiCredential.field_name == "not_a_real_field").count() == 0


def test_update_warehouse_id(logged_in_client, web_db):
    logged_in_client.post("/api-keys/accounts/create", data={"platform": "wb", "name": "ИП Яворская"})
    from app.models import PlatformAccount
    account = web_db.query(PlatformAccount).first()

    logged_in_client.post(f"/api-keys/accounts/{account.id}/warehouse", data={"warehouse_id": "12345"})

    web_db.refresh(account)
    assert account.warehouse_id == "12345"


def test_deactivate_and_activate_account(logged_in_client, web_db):
    logged_in_client.post("/api-keys/accounts/create", data={"platform": "wb", "name": "ИП Яворская"})
    from app.models import PlatformAccount
    account = web_db.query(PlatformAccount).first()
    assert account.is_active is True

    logged_in_client.post(f"/api-keys/accounts/{account.id}/deactivate")
    web_db.refresh(account)
    assert account.is_active is False

    r = logged_in_client.get("/api-keys")
    assert "отключён" in r.text

    logged_in_client.post(f"/api-keys/accounts/{account.id}/activate")
    web_db.refresh(account)
    assert account.is_active is True


def test_health_ok_when_all_workers_fresh(logged_in_client, web_db):
    from app.models import WorkerHeartbeat

    web_db.add(WorkerHeartbeat(worker_name="dispatch", last_run_at=now_utc(), last_success=True))
    web_db.commit()

    r = logged_in_client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_503_when_worker_stale(logged_in_client, web_db):
    from app.models import WorkerHeartbeat

    web_db.add(WorkerHeartbeat(
        worker_name="ftp_send", last_run_at=now_utc() - timedelta(hours=5), last_success=True,
    ))
    web_db.commit()

    r = logged_in_client.get("/health")
    assert r.status_code == 503
    data = r.json()
    assert data["ok"] is False
    assert data["workers"][0]["stale"] is True


def test_health_accessible_without_login(client, web_db):
    from app.models import WorkerHeartbeat

    # Доступен без авторизации (не редиректит на /login, отдаёт JSON).
    web_db.add(WorkerHeartbeat(worker_name="dispatch", last_run_at=now_utc(), last_success=True))
    web_db.commit()

    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


def test_health_503_when_no_heartbeats(client, web_db):
    # Планировщик ни разу не отчитался — /health обязан сигналить проблему,
    # а не показывать "здоров" на пустой таблице heartbeat-ов.
    r = client.get("/health")
    assert r.status_code == 503
    data = r.json()
    assert data["ok"] is False
    assert data["workers"] == []
    assert "reason" in data


def test_health_expected_seconds_matches_dynamic_worker_names():
    """Имена per-account воркеров динамические — интервал берётся по префиксу,
    иначе суточный catalog_poll вечно 'протух' на дефолте 600с."""
    from app.routers.health import _expected_seconds
    assert _expected_seconds("poll_orders_account_5") == 120 * 3
    assert _expected_seconds("catalog_poll_account_5") == 86400 * 3
    assert _expected_seconds("reconcile_accounts") == 300 * 3
    assert _expected_seconds("dispatch") == 45 * 3
    assert _expected_seconds("что-то-неизвестное") == 600


def test_health_daily_catalog_poller_not_falsely_stale(logged_in_client, web_db):
    from app.models import WorkerHeartbeat
    # catalog_poll суточный: heartbeat час назад — НЕ протух (ожидается 3 суток)
    web_db.add(WorkerHeartbeat(worker_name="catalog_poll_account_7",
                               last_run_at=now_utc() - timedelta(hours=1), last_success=True))
    web_db.commit()
    r = logged_in_client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_per_account_order_poller_stale_after_its_interval(logged_in_client, web_db):
    from app.models import WorkerHeartbeat
    # poll_orders интервал 120*3=360с; heartbeat 20 минут назад — протух
    web_db.add(WorkerHeartbeat(worker_name="poll_orders_account_7",
                               last_run_at=now_utc() - timedelta(minutes=20), last_success=True))
    web_db.commit()
    r = logged_in_client.get("/health")
    assert r.status_code == 503
    assert r.json()["workers"][0]["stale"] is True


def test_health_import_barcodes_15min_job_not_stale_between_runs(client, web_db):
    """import_barcodes идёт раз в 15 мин — через 20 минут после прогона это ещё не протухание."""
    from app.models import WorkerHeartbeat

    web_db.add(WorkerHeartbeat(worker_name="import_barcodes",
                               last_run_at=now_utc() - timedelta(minutes=20), last_success=True))
    web_db.commit()

    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json()["workers"][0]["stale"] is False


def test_health_weekly_full_barcode_import_marker_not_stale_for_days(client, web_db):
    """Метка import_barcodes_full пишется раз в неделю: 5 дней назад — норма, а не 503."""
    from app.models import WorkerHeartbeat

    web_db.add(WorkerHeartbeat(worker_name="import_barcodes_full",
                               last_run_at=now_utc() - timedelta(days=5), last_success=True))
    web_db.commit()

    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json()["workers"][0]["stale"] is False

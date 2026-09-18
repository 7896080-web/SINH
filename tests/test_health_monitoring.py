"""Находки 14, 15, 16 аудита — мониторинг через /health.

14. Heartbeat отключённого кабинета оставался навсегда, протухал и держал
    /health в 503 бессрочно. Предохранитель отключает кабинет ШТАТНО, то есть
    штатное срабатывание защиты навсегда красило мониторинг.
15. /health проверял только те heartbeat-ы, что уже есть в базе: если задание
    не навесилось вовсе, его строки просто не было — и мониторинг оставался
    зелёным, хотя часовой запрос выгрузки не делался и сверка стояла.
16. Анонимный запрос видел `last_error` воркера — вместе с адресом эндпоинта
    площадки, хотя докстринг обещал, что чувствительного там нет.
"""
from datetime import timedelta

from app.models import PlatformAccount, Platform, WorkerHeartbeat
from app.routers.health import (ERROR_PLACEHOLDER, REQUIRED_WORKERS, SCHEDULER_START_MARKER,
                                account_id_from_worker)
from app.timeutils import now_utc


def _account(web_db, active: bool = True) -> PlatformAccount:
    account = PlatformAccount(platform=Platform.wb, name="ИП ЯВОРСКАЯ", warehouse_id="wh",
                              is_active=active)
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    return account


def _hb(web_db, name: str, *, age_seconds: int = 0, success: bool = True, error: str = ""):
    web_db.add(WorkerHeartbeat(worker_name=name, last_success=success, last_error=error or None,
                               last_run_at=now_utc() - timedelta(seconds=age_seconds)))
    web_db.commit()


def _all_required(web_db, uptime_seconds: int = 3600):
    """Все обязательные воркеры свежие + отметка старта планировщика."""
    _hb(web_db, SCHEDULER_START_MARKER, age_seconds=uptime_seconds)
    for name in REQUIRED_WORKERS:
        _hb(web_db, name)


# ---------------------------------- 14. снятый кабинет не держит 503

def test_heartbeat_of_disabled_account_is_ignored(client, web_db):
    """Кабинет отключён (руками или предохранителем) — его опрос снят, heartbeat
    больше не обновляется. Это не повод держать мониторинг красным вечно."""
    account = _account(web_db, active=False)
    _hb(web_db, "dispatch")
    _hb(web_db, f"poll_orders_account_{account.id}", age_seconds=86400)

    r = client.get("/health")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["ignored_workers"] == 1
    assert [w["worker"] for w in data["workers"]] == ["dispatch"]


def test_heartbeat_of_deleted_account_is_ignored(client, web_db):
    """Кабинета уже нет в базе вовсе — осиротевшая строка тем более не должна
    красить мониторинг."""
    _hb(web_db, "dispatch")
    _hb(web_db, "catalog_poll_account_999", age_seconds=86400 * 30)

    r = client.get("/health")

    assert r.status_code == 200
    assert r.json()["ignored_workers"] == 1


def test_active_account_poller_is_still_checked(client, web_db):
    """Обратная граница: у ЖИВОГО кабинета опрос обязан проверяться как раньше —
    иначе находку 14 «починили» бы, отключив мониторинг совсем."""
    account = _account(web_db, active=True)
    _hb(web_db, "dispatch")
    _hb(web_db, f"poll_orders_account_{account.id}", age_seconds=86400)

    r = client.get("/health")

    assert r.status_code == 503
    assert r.json()["ignored_workers"] == 0


def test_disabled_accounts_are_counted_for_the_operator(client, web_db):
    """Отключённый кабинет мониторинг не красит, но и прятать его нельзя:
    отдаём число (без имён — /health анонимный)."""
    _account(web_db, active=False)
    _account(web_db, active=False)
    _hb(web_db, "dispatch")

    data = client.get("/health").json()

    assert data["disabled_accounts"] == 2
    assert "ЯВОРСКАЯ" not in client.get("/health").text


def test_scheduler_drops_heartbeat_with_the_job(db):
    """Вторая половина той же находки: планировщик убирает строку вместе с
    заданием, а не оставляет её протухать."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.workers.scheduler import reconcile_account_jobs
    from tests.factories import make_account

    account = make_account(db)
    sched = BackgroundScheduler(timezone="UTC")
    reconcile_account_jobs(sched, db)
    db.add(WorkerHeartbeat(worker_name=f"poll_orders_account_{account.id}",
                           last_run_at=now_utc(), last_success=True))
    db.add(WorkerHeartbeat(worker_name=f"catalog_poll_account_{account.id}",
                           last_run_at=now_utc(), last_success=True))
    db.commit()

    account.is_active = False
    db.commit()
    stats = reconcile_account_jobs(sched, db)

    assert stats["removed"] == 2
    assert stats["heartbeats_dropped"] == 2
    assert db.query(WorkerHeartbeat).count() == 0


def test_job_id_and_worker_name_stay_the_same_string(db):
    """Уборка heartbeat-ов опирается на то, что id задания и имя heartbeat —
    одна строка. Если кто-то переименует одно из них, тест упадёт здесь, а не
    молча оставит мониторинг красным."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.workers.scheduler import reconcile_account_jobs
    from tests.factories import make_account

    account = make_account(db)
    sched = BackgroundScheduler(timezone="UTC")
    reconcile_account_jobs(sched, db)

    job_ids = {j.id for j in sched.get_jobs()}
    assert f"poll_orders_account_{account.id}" in job_ids
    assert f"catalog_poll_account_{account.id}" in job_ids
    assert account_id_from_worker(f"poll_orders_account_{account.id}") == account.id


# ------------------------------- 15. пропавший воркер виден

def test_missing_required_worker_turns_health_red(client, web_db):
    """Сценарий находки: часовой запрос выгрузки перестал навешиваться. Строки
    нет — раньше проверять было нечего, и мониторинг оставался зелёным, хотя
    сверка встала."""
    _all_required(web_db)
    web_db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "ftp_send_export_request").delete()
    web_db.commit()

    r = client.get("/health")

    assert r.status_code == 503
    assert r.json()["missing_workers"] == ["ftp_send_export_request"]


def test_all_required_workers_present_is_green(client, web_db):
    _all_required(web_db)

    r = client.get("/health")

    assert r.status_code == 200
    assert r.json()["missing_workers"] == []


def test_missing_worker_is_forgiven_right_after_restart(client, web_db):
    """Сразу после рестарта воркер ещё не отработал первый раз — объявлять его
    пропавшим нельзя, иначе каждый деплой даёт ложную тревогу."""
    _hb(web_db, SCHEDULER_START_MARKER, age_seconds=10)
    _hb(web_db, "dispatch")

    r = client.get("/health")

    assert r.status_code == 200
    assert r.json()["missing_workers"] == []


def test_missing_workers_are_not_judged_without_the_start_marker(client, web_db):
    """Старая база: отметки старта ещё нет (появится при первом запуске нового
    планировщика). До этого пропажу не судим — иначе деплой сразу даёт 503."""
    _hb(web_db, "dispatch")

    r = client.get("/health")

    assert r.status_code == 200
    assert r.json()["missing_workers"] == []


def test_start_marker_itself_is_not_a_worker(client, web_db):
    """Отметка старта не обновляется, и проверять её на протухание нельзя —
    иначе она сама навсегда покрасит мониторинг."""
    _all_required(web_db, uptime_seconds=86400 * 30)

    r = client.get("/health")

    assert r.status_code == 200
    assert SCHEDULER_START_MARKER not in [w["worker"] for w in r.json()["workers"]]


# --------------------------- 16. текст ошибки наружу не отдаём

def test_worker_error_text_is_not_exposed_anonymously(client, web_db):
    """Воспроизведено в аудите: анонимный запрос видел last_error с адресом
    эндпоинта площадки."""
    _hb(web_db, "dispatch", success=False,
        error="401 от https://suppliers-api.wildberries.ru/api/v3/stocks/wh-77")

    r = client.get("/health")

    assert r.status_code == 503
    assert "wildberries.ru" not in r.text
    assert "401" not in r.text
    worker = r.json()["workers"][0]
    assert worker["last_success"] is False       # факт ошибки виден
    assert worker["last_error"] == ERROR_PLACEHOLDER


def test_error_text_is_still_available_to_the_operator(logged_in_client, web_db):
    """Текст никуда не делся — он на «Диагностике», под логином."""
    _hb(web_db, "dispatch", success=False,
        error="401 от https://suppliers-api.wildberries.ru/api/v3/stocks/wh-77")

    r = logged_in_client.get("/diagnostics")

    assert "wildberries.ru" in r.text


# ------------------- 15б. per-account задание, не отработавшее НИ РАЗУ

def test_a_never_run_account_job_is_missing_not_invisible(client, web_db):
    """Находка 19.09: суточная выгрузка каталога не запускалась вовсе — первый
    прогон откладывался на сутки, а процесс столько не живёт. Строки heartbeat не
    было, проверялись только существующие строки, и /health был зелёный при
    снимке каталога пятидневной давности."""
    account = _account(web_db)
    _all_required(web_db)
    _hb(web_db, f"poll_orders_account_{account.id}")
    # catalog_poll_account_<id> не писали вовсе — задание не отработало ни разу

    r = client.get("/health")

    assert r.status_code == 503
    assert r.json()["missing_workers"] == [f"catalog_poll_account_{account.id}"]


def test_account_jobs_that_did_run_keep_health_green(client, web_db):
    account = _account(web_db)
    _all_required(web_db)
    _hb(web_db, f"poll_orders_account_{account.id}")
    _hb(web_db, f"catalog_poll_account_{account.id}")

    r = client.get("/health")

    assert r.status_code == 200
    assert r.json()["missing_workers"] == []


def test_account_jobs_are_forgiven_right_after_restart(client, web_db):
    """Сразу после рестарта выгрузка каталога ещё не отработала — это не тревога."""
    _account(web_db)
    _all_required(web_db, uptime_seconds=60)

    r = client.get("/health")

    assert r.status_code == 200
    assert r.json()["missing_workers"] == []


def test_a_disabled_account_does_not_demand_its_jobs(client, web_db):
    """Снятый кабинет заданий не имеет — требовать их отчёта нельзя."""
    _account(web_db, active=False)
    _all_required(web_db)

    r = client.get("/health")

    assert r.status_code == 200
    assert r.json()["missing_workers"] == []

"""Сверка per-account заданий планировщика с БД — самоподхват новых
кабинетов без перезапуска процесса. Планировщик не запускается (start()
не вызывается), сети нет — проверяется только состав заданий."""
from apscheduler.schedulers.background import BackgroundScheduler

from app.models import Platform
from app.workers.scheduler import (
    reconcile_account_jobs, POLL_ORDERS_JOB_PREFIX, CATALOG_POLL_JOB_PREFIX,
)
from tests.factories import make_account


def _new_sched():
    # Не запускаем (.start() не вызывается) — только конфигурируем задания.
    return BackgroundScheduler(timezone="UTC")


def _job_ids(sched):
    return {j.id for j in sched.get_jobs()}


def test_reconcile_adds_jobs_for_active_accounts(db):
    a = make_account(db, Platform.wb, name="Кабинет 1")
    sched = _new_sched()

    stats = reconcile_account_jobs(sched, db)

    assert stats == {"added": 2, "removed": 0}
    ids = _job_ids(sched)
    assert f"{POLL_ORDERS_JOB_PREFIX}{a.id}" in ids
    assert f"{CATALOG_POLL_JOB_PREFIX}{a.id}" in ids


def test_reconcile_is_idempotent(db):
    make_account(db, Platform.ozon, name="Ozon")
    sched = _new_sched()

    reconcile_account_jobs(sched, db)
    stats2 = reconcile_account_jobs(sched, db)  # второй прогон — ничего не меняет

    assert stats2 == {"added": 0, "removed": 0}


def test_reconcile_picks_up_new_account_without_restart(db):
    make_account(db, Platform.wb, name="Первый")
    sched = _new_sched()
    reconcile_account_jobs(sched, db)
    before = len(sched.get_jobs())

    # Кабинет добавили «через админку» уже после старта планировщика.
    b = make_account(db, Platform.kit, name="Добавлен позже")
    stats = reconcile_account_jobs(sched, db)

    assert stats["added"] == 2
    assert len(sched.get_jobs()) == before + 2
    assert f"{POLL_ORDERS_JOB_PREFIX}{b.id}" in _job_ids(sched)


def test_reconcile_removes_jobs_for_deactivated_account(db):
    a = make_account(db, Platform.wb, name="Отключаемый")
    sched = _new_sched()
    reconcile_account_jobs(sched, db)
    assert f"{POLL_ORDERS_JOB_PREFIX}{a.id}" in _job_ids(sched)

    a.is_active = False
    db.commit()
    stats = reconcile_account_jobs(sched, db)

    assert stats["removed"] == 2
    assert f"{POLL_ORDERS_JOB_PREFIX}{a.id}" not in _job_ids(sched)
    assert f"{CATALOG_POLL_JOB_PREFIX}{a.id}" not in _job_ids(sched)


def test_reconcile_does_not_touch_static_jobs(db):
    sched = _new_sched()
    sched.add_job(lambda: None, "interval", seconds=45, id="dispatch", max_instances=1)
    make_account(db, Platform.wb, name="Кабинет")

    reconcile_account_jobs(sched, db)

    assert "dispatch" in _job_ids(sched)  # статическое задание не тронуто

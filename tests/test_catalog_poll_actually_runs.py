"""Суточная выгрузка каталога обязана хоть раз случиться — и быть видимой.

19.09 выяснилось, что она не запускалась НИ РАЗУ: триггер `interval` отсчитывает
первый прогон от момента добавления задания, задания живут в памяти процесса и
навешиваются заново при каждом старте воркера, а воркер на бою перезапускается
чаще раза в сутки. Снимок каталога Kit при этом лежал пятидневной давности (его
сделали руками со страницы «Мэппинг»), а `/health` был зелёный: строки heartbeat
у задания не было вовсе, а проверялись только существующие строки.
"""
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from app.models import Platform, WorkerHeartbeat
from app.timeutils import now_utc
from app.workers import scheduler as sch
from tests.factories import make_account


def _sched():
    return BackgroundScheduler(timezone="UTC")      # .start() не вызываем


def _catalog_job(sched, account_id):
    return sched.get_job(f"{sch.CATALOG_POLL_JOB_PREFIX}{account_id}")


# ------------------------------------------- задание доживает до первого прогона

def test_the_catalog_job_runs_soon_after_start_not_in_a_day(db):
    a = make_account(db, Platform.kit, name="КИТ")
    sched = _sched()

    sch.reconcile_account_jobs(sched, db)

    job = _catalog_job(sched, a.id)
    assert job.next_run_time is not None
    delay = job.next_run_time.replace(tzinfo=None) - now_utc()
    assert delay < timedelta(minutes=10), (
        "первый прогон отложен надолго — процесс до него не доживёт")


def test_the_catalog_job_keeps_its_daily_interval(db):
    a = make_account(db, Platform.kit, name="КИТ")
    sched = _sched()

    sch.reconcile_account_jobs(sched, db)

    assert _catalog_job(sched, a.id).trigger.interval == timedelta(
        hours=sch.CATALOG_POLL_INTERVAL_HOURS)


# ------------------------------------------- но заново на каждом рестарте не гоняем

def _run_catalog_poll(db, account_id, monkeypatch):
    """Зовёт job_catalog_poll, подменив всё, что ходит наружу. Возвращает,
    дошло ли дело до самой выгрузки."""
    calls = []
    monkeypatch.setattr(sch, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(sch, "build_client", lambda *a, **k: object())
    monkeypatch.setattr(sch, "load_platform_catalog",
                        lambda *a, **k: calls.append("load") or {})
    monkeypatch.setattr(sch, "poll_catalog", lambda *a, **k: {})
    sch.job_catalog_poll(account_id)
    return bool(calls)


def test_a_recent_successful_run_is_not_repeated(db, monkeypatch):
    a = make_account(db, Platform.kit, name="КИТ")
    db.add(WorkerHeartbeat(worker_name=f"{sch.CATALOG_POLL_JOB_PREFIX}{a.id}",
                           last_run_at=now_utc() - timedelta(hours=1), last_success=True))
    db.commit()

    assert _run_catalog_poll(db, a.id, monkeypatch) is False


def test_a_skipped_run_does_not_push_the_deadline_forward(db, monkeypatch):
    """Иначе выгрузка не случилась бы никогда: каждый рестарт сдвигал бы срок."""
    a = make_account(db, Platform.kit, name="КИТ")
    stamped = now_utc() - timedelta(hours=1)
    db.add(WorkerHeartbeat(worker_name=f"{sch.CATALOG_POLL_JOB_PREFIX}{a.id}",
                           last_run_at=stamped, last_success=True))
    db.commit()

    _run_catalog_poll(db, a.id, monkeypatch)

    hb = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == f"{sch.CATALOG_POLL_JOB_PREFIX}{a.id}").first()
    assert hb.last_run_at == stamped


def test_an_old_run_is_repeated(db, monkeypatch):
    a = make_account(db, Platform.kit, name="КИТ")
    db.add(WorkerHeartbeat(worker_name=f"{sch.CATALOG_POLL_JOB_PREFIX}{a.id}",
                           last_run_at=now_utc() - timedelta(days=2), last_success=True))
    db.commit()

    assert _run_catalog_poll(db, a.id, monkeypatch) is True


def test_a_failed_run_is_repeated_at_once(db, monkeypatch):
    """Неудача — не повод ждать сутки."""
    a = make_account(db, Platform.kit, name="КИТ")
    db.add(WorkerHeartbeat(worker_name=f"{sch.CATALOG_POLL_JOB_PREFIX}{a.id}",
                           last_run_at=now_utc() - timedelta(minutes=5), last_success=False))
    db.commit()

    assert _run_catalog_poll(db, a.id, monkeypatch) is True


def test_the_first_run_ever_happens(db, monkeypatch):
    a = make_account(db, Platform.kit, name="КИТ")

    assert _run_catalog_poll(db, a.id, monkeypatch) is True

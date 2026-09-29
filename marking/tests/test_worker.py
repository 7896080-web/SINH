from datetime import timedelta

from markapp import backup, onec
from markapp.models import WorkerHeartbeat
from markapp.timeutils import now_utc
from markapp.workers import scheduler


def _same_session(monkeypatch, db):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)


def test_exchange_job_writes_heartbeat_and_reports_unmatched(db, exchange_dirs, monkeypatch):
    _same_session(monkeypatch, db)
    onec.enqueue_ping(db)                  # есть задание — есть зачем обращаться к 1С
    db.commit()
    (exchange_dirs.ONEC_RESULTS_DIR / "result_mark_1.txt").write_text(
        "lamoda-1|OK|X|SUPPLY_MOVEMENT", encoding="utf-8")
    scheduler.job_onec_exchange()
    hb = db.get(WorkerHeartbeat, "onec_exchange")
    assert hb.last_success and "без задания: 1" in hb.last_error


def test_exchange_job_failure_is_visible(db, exchange_dirs, monkeypatch):
    _same_session(monkeypatch, db)
    onec.enqueue_ping(db)
    db.commit()

    def boom(*a, **k):
        raise OSError("нет доступа к папке заданий")
    monkeypatch.setattr(onec, "publish_pending", boom)
    scheduler.job_onec_exchange()
    hb = db.get(WorkerHeartbeat, "onec_exchange")
    assert not hb.last_success and "нет доступа" in hb.last_error


def test_backup_job_skip_does_not_touch_heartbeat(db, monkeypatch):
    """Иначе каждый перезапуск сдвигал бы срок, и копия не снималась бы никогда."""
    _same_session(monkeypatch, db)
    monkeypatch.setattr(backup, "last_backup", lambda: now_utc() - timedelta(hours=1))
    scheduler.job_backup()
    assert db.get(WorkerHeartbeat, "backup") is None

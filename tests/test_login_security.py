from datetime import datetime, timedelta
from app.timeutils import now_utc

from app.models import User, AuditLog
from app.security import hash_password
from app.login_security import (
    is_locked, lockout_remaining_minutes, record_login_failure, record_login_success,
    MAX_FAILED_ATTEMPTS, LOCKOUT_MINUTES,
)


def _make_user(db):
    user = User(username="admin", password_hash=hash_password("correct-password"))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_not_locked_initially(db):
    user = _make_user(db)
    assert is_locked(user) is False
    assert lockout_remaining_minutes(user) == 0


def test_failure_increments_counter_without_locking_below_threshold(db):
    user = _make_user(db)
    for i in range(MAX_FAILED_ATTEMPTS - 1):
        locked = record_login_failure(db, user)
        db.commit()
        assert locked is False

    assert user.failed_login_attempts == MAX_FAILED_ATTEMPTS - 1
    assert is_locked(user) is False


def test_reaching_threshold_locks_account(db):
    user = _make_user(db)
    locked = False
    for i in range(MAX_FAILED_ATTEMPTS):
        locked = record_login_failure(db, user)
        db.commit()

    assert locked is True
    assert is_locked(user) is True
    assert user.locked_until > now_utc()


def test_lockout_duration_is_30_minutes(db):
    user = _make_user(db)
    for _ in range(MAX_FAILED_ATTEMPTS):
        record_login_failure(db, user)
    db.commit()

    delta = user.locked_until - now_utc()
    assert LOCKOUT_MINUTES - 1 <= delta.total_seconds() / 60 <= LOCKOUT_MINUTES


def test_lock_writes_audit_log(db):
    user = _make_user(db)
    for _ in range(MAX_FAILED_ATTEMPTS):
        record_login_failure(db, user)
    db.commit()

    entry = db.query(AuditLog).filter(AuditLog.action == "account_locked").first()
    assert entry is not None
    assert entry.actor == "system"
    assert "admin" in entry.details


def test_success_resets_counter_and_unlocks(db):
    user = _make_user(db)
    user.failed_login_attempts = 7
    user.locked_until = now_utc() + timedelta(minutes=10)
    db.commit()

    record_login_success(db, user)
    db.commit()

    assert user.failed_login_attempts == 0
    assert user.locked_until is None
    assert is_locked(user) is False


def test_lockout_remaining_minutes_rounds_up(db):
    user = _make_user(db)
    user.locked_until = now_utc() + timedelta(seconds=61)  # чуть больше минуты
    db.commit()

    assert lockout_remaining_minutes(user) == 2  # округление вверх, не показываем "0 минут"


def test_lockout_expires_naturally_after_window(db):
    user = _make_user(db)
    user.failed_login_attempts = MAX_FAILED_ATTEMPTS
    user.locked_until = now_utc() - timedelta(seconds=1)  # блокировка уже истекла
    db.commit()

    assert is_locked(user) is False
    assert lockout_remaining_minutes(user) == 0

from app.models import AuditLog
from app.workers.circuit_breaker import record_success, record_failure, FAILURE_THRESHOLD
from tests.factories import make_account


def test_record_success_resets_counter(db):
    account = make_account(db)
    account.consecutive_failures = 3
    account.last_error = "было плохо"
    db.commit()

    record_success(db, account)
    db.commit()

    assert account.consecutive_failures == 0
    assert account.last_error is None


def test_record_failure_increments_counter(db):
    account = make_account(db)
    disabled = record_failure(db, account, "тестовая ошибка")
    db.commit()

    assert account.consecutive_failures == 1
    assert account.last_error == "тестовая ошибка"
    assert disabled is False
    assert account.is_active is True


def test_account_auto_disabled_at_threshold(db):
    account = make_account(db)

    disabled = False
    for i in range(FAILURE_THRESHOLD):
        disabled = record_failure(db, account, f"ошибка {i}")
        db.commit()

    assert account.consecutive_failures == FAILURE_THRESHOLD
    assert account.is_active is False
    assert disabled is True  # именно этот вызов отключил кабинет


def test_disabled_flag_only_true_on_the_triggering_call(db):
    account = make_account(db)

    results = []
    for i in range(FAILURE_THRESHOLD + 2):
        results.append(record_failure(db, account, "ошибка"))
        db.commit()

    # True только один раз — в момент пересечения порога
    assert results.count(True) == 1
    assert results[FAILURE_THRESHOLD - 1] is True


def test_auto_disable_writes_audit_log(db):
    account = make_account(db)
    for i in range(FAILURE_THRESHOLD):
        record_failure(db, account, "постоянная ошибка авторизации")
        db.commit()

    entry = db.query(AuditLog).filter(AuditLog.action == "account_auto_disabled").first()
    assert entry is not None
    assert entry.actor == "system"
    assert account.name in entry.details


def test_success_after_near_threshold_prevents_auto_disable(db):
    account = make_account(db)
    for i in range(FAILURE_THRESHOLD - 1):
        record_failure(db, account, "ошибка")
        db.commit()

    assert account.is_active is True

    record_success(db, account)
    db.commit()
    assert account.consecutive_failures == 0

    # ещё один сбой после сброса не должен сразу отключить
    disabled = record_failure(db, account, "ошибка")
    db.commit()
    assert disabled is False
    assert account.is_active is True

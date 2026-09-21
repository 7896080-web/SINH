"""Сроки хранения: что удаляем и, главное, чего не трогаем никогда.

Аудит 21.09: прунинг в системе был ровно один — снимков остатков на дату. Всё
остальное росло неограниченно, и быстрее всех журнал сверки: 276 856 строк на
момент замера и около сорока тысяч в сутки, то есть четырнадцать миллионов за
год. Следствие не «кончится диск», а «час сверки длиннее с каждым месяцем» и
«в журнале на четырнадцать миллионов строк уже ничего не найти».

Опасность тут ровно одна и обратная: удалить то, что ещё в работе. Запись
очереди в `pending` — это не история, а списанный у нас и не уехавший на
площадку остаток. Задание 1С в `timeout` — количество, которое прямо сейчас
считается «в пути» и влияет на остаток товара. Про них здесь тестов больше,
чем про само удаление.
"""

from datetime import timedelta

from app.models import (AnomalyReason, AnomalyStatus, AuditLog, DispatchQueueItem,
                        DispatchStatus, FtpTask, FtpTaskStatus, Platform,
                        ReconciliationClassification, ReconciliationLog, SyncAnomaly)
from app.retention import apply_retention
from app.timeutils import now_utc
from tests.factories import make_account


def _old(days: int):
    return now_utc() - timedelta(days=days)


def _recon(db, days: int):
    db.add(ReconciliationLog(uid_1c="u1", checked_at=_old(days), python_stock=1,
                             in_flight=0, expected_1c=1, actual_1c=1, delta=0,
                             classification=ReconciliationClassification.normal))


def _queue(db, days: int, status=DispatchStatus.sent):
    db.add(DispatchQueueItem(uid_1c="u1", account_id=1, quantity=5, status=status,
                             reason="test", is_test=False, created_at=_old(days)))


def _task(db, days: int, status=FtpTaskStatus.done):
    db.add(FtpTask(command="CREATE_MOVEMENT", order_id=f"o-{days}-{status.value}",
                   account_id=1, status=status, created_at=_old(days)))


# ----------------------------------------------------------- старое уходит

def test_old_reconciliation_rows_are_removed(db):
    _recon(db, 200)
    db.commit()

    assert apply_retention(db)["reconciliation_log"] == 1
    assert db.query(ReconciliationLog).count() == 0


def test_fresh_reconciliation_rows_stay(db):
    _recon(db, 10)
    db.commit()

    apply_retention(db)

    assert db.query(ReconciliationLog).count() == 1


def test_old_sent_queue_rows_are_removed(db):
    make_account(db)
    _queue(db, 90, DispatchStatus.sent)
    db.commit()

    assert apply_retention(db)["dispatch_queue"] == 1


def test_old_audit_rows_are_removed(db):
    db.add(AuditLog(actor="admin", action="test", created_at=_old(400)))
    db.commit()

    assert apply_retention(db)["audit_log"] == 1


def test_old_closed_1c_tasks_are_removed(db):
    make_account(db)
    _task(db, 200, FtpTaskStatus.done)
    db.commit()

    assert apply_retention(db)["ftp_tasks"] == 1


def test_old_resolved_anomalies_are_removed(db):
    account = make_account(db)
    db.add(SyncAnomaly(uid_1c="u1", account_id=account.id,
                       reason=AnomalyReason.order_on_disabled,
                       status=AnomalyStatus.resolved, detected_at=_old(200)))
    db.commit()

    assert apply_retention(db)["sync_anomalies"] == 1


# ------------------------------------------- а незаконченное не уходит НИКОГДА

def test_a_pending_dispatch_row_survives_any_age(db):
    """Остаток у нас списан, на площадку не уехал. Это не история — это
    единственный след того, что площадка продаёт по старому числу."""
    make_account(db)
    _queue(db, 3650, DispatchStatus.pending)
    db.commit()

    apply_retention(db)

    assert db.query(DispatchQueueItem).count() == 1


def test_a_timeout_1c_task_survives_any_age(db):
    """По `timeout` неизвестно, создан документ в 1С или нет, и его количество
    до сих пор считается «в пути». Удалить такую строку значит МОЛЧА изменить
    остаток товара."""
    make_account(db)
    _task(db, 3650, FtpTaskStatus.timeout)
    db.commit()

    apply_retention(db)

    assert db.query(FtpTask).count() == 1


def test_a_failed_1c_task_survives(db):
    """`failed` разбирает человек — пока не разобрал, строка нужна."""
    make_account(db)
    _task(db, 3650, FtpTaskStatus.failed)
    db.commit()

    apply_retention(db)

    assert db.query(FtpTask).count() == 1


def test_a_new_anomaly_survives_any_age(db):
    """Неразобранная аномалия — это заказ, который никто не разнёс."""
    account = make_account(db)
    db.add(SyncAnomaly(uid_1c="u1", account_id=account.id,
                       reason=AnomalyReason.missing_barcode,
                       status=AnomalyStatus.new, detected_at=_old(3650)))
    db.commit()

    apply_retention(db)

    assert db.query(SyncAnomaly).count() == 1


def test_an_errored_queue_row_is_kept_within_its_term(db):
    """Отказ рассылки — рабочая находка отчёта, пока не истёк срок."""
    make_account(db)
    _queue(db, 5, DispatchStatus.error)
    db.commit()

    apply_retention(db)

    assert db.query(DispatchQueueItem).count() == 1


# --------------------------------------------------------------- как удаляет

def test_deletion_goes_in_chunks(db, monkeypatch):
    """Одно DELETE на миллион строк — это длинная транзакция, то есть ровно та
    беда, от которой только что вылечили сверку: на всё её время писать в базу
    не может никто."""
    import app.retention as retention

    monkeypatch.setattr(retention, "CHUNK", 3)
    commits = []
    original = db.commit
    monkeypatch.setattr(db, "commit", lambda: (commits.append(1), original())[1])

    for _ in range(10):
        _recon(db, 200)
    original()

    retention.apply_retention(db)

    assert len(commits) >= 4, "порциями по три — это минимум четыре коммита"


def test_a_clean_base_is_not_an_error(db):
    assert sum(apply_retention(db).values()) == 0


def test_every_table_is_reported(db):
    """Отчёт задания обязан называть все таблицы, даже с нулём: пропавшая из
    словаря таблица — это молча переставшая чиститься таблица."""
    stats = apply_retention(db)

    assert set(stats) == {"reconciliation_log", "dispatch_queue", "audit_log",
                          "ftp_tasks", "sync_anomalies", "test_log"}

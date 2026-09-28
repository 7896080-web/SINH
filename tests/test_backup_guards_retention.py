"""Чистка не идёт без свежей копии, а потерянный ответ 1С доходит до человека.

Две находки аудита 23.09, и обе — про сигнал, у которого не было читателя.
"""
from datetime import timedelta

import pytest

from app.models import WorkerHeartbeat
from app.timeutils import now_utc
from app.workers import scheduler


@pytest.fixture()
def no_cleanup(monkeypatch):
    """Подменяем саму чистку: нас интересует, зовут её или нет."""
    calls = []
    monkeypatch.setattr(scheduler, "apply_retention",
                        lambda db: calls.append(1) or {"x": 0})
    return calls


def _heartbeat(db, name):
    return db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == name).first()


def _run(monkeypatch, db, job, **patches):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    for name, value in patches.items():
        monkeypatch.setattr(scheduler, name, value)
    job()


# --------------------------------------------------------------- чистка

def test_the_cleanup_refuses_when_the_copy_is_stale(monkeypatch, db, no_cleanup):
    """Сломанный бэкап — главный случай, ради которого условие и заведено.

    Пока он не снимается, чистка день за днём удаляла бы историю, имея за спиной
    копию всё большей давности, а `/health` оставался бы зелёным: своей работы
    она делает ровно столько, сколько обещала."""
    old = now_utc() - scheduler.RETENTION_REQUIRES_BACKUP_WITHIN - timedelta(hours=1)
    _run(monkeypatch, db, scheduler.job_retention, last_backup=lambda: (old, 3))

    assert no_cleanup == [], "чистка пошла без свежей копии"
    row = _heartbeat(db, "retention")
    assert row.last_success is True, "это не поломка задания, а осознанный отказ"
    assert "нет свежей копии" in (row.last_error or ""), (
        "молчаливый отказ — тот же дефект с другой стороны: чистка не идёт, "
        "а понять это неоткуда")


def test_the_cleanup_refuses_when_there_is_no_copy_at_all(monkeypatch, db, no_cleanup):
    _run(monkeypatch, db, scheduler.job_retention, last_backup=lambda: (None, 0))
    assert no_cleanup == []
    assert "копий нет вовсе" in (_heartbeat(db, "retention").last_error or "")


def test_a_copy_left_by_a_skipped_backup_is_still_good_enough(monkeypatch, db,
                                                              no_cleanup):
    """Обычный перезапуск чистку останавливать НЕ должен.

    После рестарта бэкап выходит по `BACKUP_MIN_GAP` («свежая копия уже есть»), и
    копия остаётся вчерашней — но она всё равно снята ДО чистки и содержит всё,
    что та собирается удалить. Отказывать здесь значило бы остановить чистку
    почти навсегда, а её отказ виден не сразу."""
    yesterday = now_utc() - timedelta(hours=scheduler.BACKUP_MIN_GAP.total_seconds()
                                      / 3600 - 1)
    _run(monkeypatch, db, scheduler.job_retention, last_backup=lambda: (yesterday, 5))
    assert no_cleanup == [1], "чистка не пошла при годной копии"
    assert not (_heartbeat(db, "retention").last_error or ""), (
        "оговорка на исправном прогоне приучит её не читать")


def test_the_threshold_follows_the_backup_interval(monkeypatch, db, no_cleanup):
    """Предел считается ОТ интервала бэкапа, а не числом рядом с ним.

    Разойдись они, условие однажды начало бы отказывать на исправной системе."""
    assert (scheduler.RETENTION_REQUIRES_BACKUP_WITHIN
            > timedelta(hours=scheduler.BACKUP_INTERVAL_HOURS)), (
        "предел обязан быть больше интервала бэкапа, иначе чистка будет "
        "отказывать штатно")
    assert (scheduler.RETENTION_REQUIRES_BACKUP_WITHIN
            > scheduler.BACKUP_MIN_GAP), (
        "иначе копия, оставленная пропущенным бэкапом, всегда считалась бы старой")


# ------------------------------------------------------- потерянный ответ 1С

def test_a_lost_1c_answer_reaches_the_report(db):
    """Оговорка успешной отметки — и у неё есть читатель.

    Ответ, не легший ни на одно задание, уходит в архив и не возвращается:
    повторно 1С его не пришлёт. Задание доживёт до «просрочено» и навсегда будет
    считаться «в пути». До сих пор этот факт жил только в логе."""
    from app.report import _check_ftp_receive_unmatched

    db.add(WorkerHeartbeat(worker_name="ftp_receive", last_run_at=now_utc(),
                           last_success=True,
                           last_error="ответов 1С не сопоставлено: 3"))
    db.flush()

    finding = _check_ftp_receive_unmatched(db)
    assert finding is not None, "находка молчит о потерянном ответе 1С"
    assert "3" in finding.title
    # Находка обязана называть СЛЕДСТВИЕ, а не факт: по созданиям остаток
    # занижен, по отменам завышен — и второе есть прямой оверселл.
    assert "оверселл" in finding.consequence


def test_the_report_stays_quiet_when_the_channel_is_clean(db):
    """Молчание на исправной системе — обязательное свойство находки.

    Ругать канал за то, что он работает, — верный способ приучить оператора
    пролистывать отчёт целиком."""
    from app.report import _check_ftp_receive_unmatched

    db.add(WorkerHeartbeat(worker_name="ftp_receive", last_run_at=now_utc(),
                           last_success=True, last_error=None))
    db.flush()
    assert _check_ftp_receive_unmatched(db) is None


def test_the_receiver_puts_its_unprocessed_counters_into_the_note(monkeypatch, db):
    """Правило общее: ненулевой счётчик непроведённой работы уходит в оговорку.

    У опроса заказов, сверки остатков и выгрузки каталога это закрыто; канал 1С —
    где настоящие документы и настоящий остаток — оставался без него."""
    class _Exchange:
        def list_result_files(self): return ["result_1.txt"]
        def read_result(self, name): return "ORDER-1|OK|ЦБ000000001|CREATE_MOVEMENT"
        def archive_result(self, name): pass

    monkeypatch.setattr(scheduler, "_build_ftp_exchange", lambda: _Exchange())
    monkeypatch.setattr(scheduler, "apply_result_batch",
                        lambda db, content: {"ok": 0, "error": 0, "unmatched": 2})
    monkeypatch.setattr(scheduler, "collect_stock_delta", lambda db, ex: ({}, {}))
    monkeypatch.setattr(scheduler, "finalize_stock_delta", lambda db, ex, st: None)
    monkeypatch.setattr(scheduler, "apply_stock_on_date_files",
                        lambda db, ex: {"files": 0, "rows": 0, "unmatched": 5})
    monkeypatch.setattr(scheduler, "prune_stock_date_snapshots", lambda db: 0)
    monkeypatch.setattr(scheduler, "detect_timed_out_tasks", lambda db: [])
    monkeypatch.setattr(scheduler, "detect_timed_out_stock_date_requests", lambda db: [])
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    scheduler.job_ftp_receive()

    row = _heartbeat(db, "ftp_receive")
    assert row.last_success is True, "это не поломка задания"
    assert "ответов 1С не сопоставлено: 2" in row.last_error
    assert "строк выгрузки на дату мимо заявок: 5" in row.last_error

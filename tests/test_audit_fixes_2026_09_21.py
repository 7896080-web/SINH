"""Пять находок аудита 21.09 — закреплены здесь, чтобы не вернулись.

Все пять объединяет одно свойство: **их поломка не видна в тот день, когда она
случилась.** Бэкап, которого нет, не мешает работать; пропущенное задание
планировщика не оставляет следа; логи растут молча; длинная транзакция просто
делает страницы чуть медленнее; история копится незаметно. Узнают про всё это
одинаково — по последствиям и поздно.
"""

import re
from datetime import timedelta

from app.report import BACKUP_STALE, _check_backup_missing
from app.routers.health import REQUIRED_WORKERS
from app.timeutils import now_utc


# ------------------------------------- 1. бэкап виден мониторингу и отчёту

def test_the_backup_job_is_required_by_health(db):
    """Единственное задание, поломка которого ничего не ломает СЕГОДНЯ — и
    ровно поэтому его отсутствие заметить некому."""
    assert "backup" in REQUIRED_WORKERS


def _backup_job_has_run(db):
    """Отчёт спрашивает про копии только у системы, где задание уже работало:
    «не отработало ни разу» — вопрос /health, а не отчёта."""
    from app.models import WorkerHeartbeat
    db.add(WorkerHeartbeat(worker_name="backup", last_run_at=now_utc(),
                           last_success=True))
    db.commit()


def test_no_backup_is_a_critical_finding(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./x.db")
    _backup_job_has_run(db)

    finding = _check_backup_missing(db)

    assert finding is not None
    assert finding.level == "critical"


def test_a_fresh_backup_is_silent(db, tmp_path, monkeypatch):
    from app import backup
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./x.db")
    _backup_job_has_run(db)
    (tmp_path / f"{backup.NAME_PREFIX}{now_utc():%Y%m%d-%H%M%S}.db").write_bytes(b"x")

    assert _check_backup_missing(db) is None


def test_a_stale_backup_is_reported(db, tmp_path, monkeypatch):
    from app import backup
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./x.db")
    _backup_job_has_run(db)
    old = now_utc() - BACKUP_STALE - timedelta(days=1)
    (tmp_path / f"{backup.NAME_PREFIX}{old:%Y%m%d-%H%M%S}.db").write_bytes(b"x")

    assert _check_backup_missing(db) is not None


def test_a_non_sqlite_install_is_not_nagged(db, monkeypatch):
    """У PostgreSQL свой механизм. Ругать установку за то, чего мы не умеем, —
    верный способ приучить оператора пролистывать отчёт."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    assert _check_backup_missing(db) is None


# ------------------------------- 2. пропущенное задание выполняется, а не тонет

def test_the_scheduler_gives_jobs_room_to_be_late():
    """Умолчание APScheduler — ОДНА СЕКУНДА: занят в момент запуска, значит не
    выполнять. Для рассылки раз в 45 секунд безобидно, а вот часовой запрос
    выгрузки у 1С пропускается на час, суточный справочник баркодов — на сутки.
    Молча."""
    from app.workers.scheduler import MISFIRE_GRACE_SECONDS

    assert MISFIRE_GRACE_SECONDS >= 60


def test_the_scheduler_actually_passes_it_on():
    """Константа, которую никуда не передали, — это комментарий, а не настройка."""
    source = open("app/workers/scheduler.py", encoding="utf-8").read()
    block = source.split("BlockingScheduler(", 1)[1][:400]

    assert "misfire_grace_time" in block
    assert "coalesce" in block


# ------------------------------------------------- 3. логи не растут вечно

def test_the_installer_turns_on_log_rotation():
    """Без ротации worker.err.log растёт, пока есть диск: однажды его нельзя
    будет ни открыть, ни найти в нём строку — то есть именно тогда, когда он
    нужен."""
    source = open("deploy/install_windows.ps1", encoding="utf-8", errors="replace").read()

    assert "AppRotateFiles 1" in source
    assert "AppRotateBytes" in source


def test_there_is_a_way_to_turn_it_on_for_live_services():
    """Установщик на боевом отработал раньше, чем эта настройка появилась, и
    заново его не запускают."""
    source = open("deploy/enable_log_rotation.ps1", encoding="utf-8").read()

    assert "sync_admin_worker" in source
    assert "AppRotateFiles" in source


# --------------------------- 4. сверка не держит базу одной транзакцией

def test_reconciliation_commits_in_chunks():
    """Вся выгрузка — около полутора тысяч товаров — шла одной транзакцией, и
    всё это время писать в базу не мог никто: ни рассылка, ни приём заказов, ни
    оператор в браузере."""
    from app.workers.reconciliation import RECONCILE_COMMIT_EVERY

    assert 1 < RECONCILE_COMMIT_EVERY <= 1000

    source = open("app/workers/reconciliation.py", encoding="utf-8").read()
    body = source.split("def run_reconciliation", 1)[1]

    assert "RECONCILE_COMMIT_EVERY" in body, "константа объявлена, но не применена"


def test_the_reconciliation_still_commits_at_the_end(db):
    """Порции порциями, а последняя, неполная, обязана дойти до базы."""
    source = open("app/workers/reconciliation.py", encoding="utf-8").read()
    body = source.split("def run_reconciliation", 1)[1]

    assert re.search(r"\n    db\.commit\(\)\n    return stats", body)


# ------------------------------------------------- 5. история не растёт вечно

def test_the_retention_job_is_required_by_health():
    """Её отсутствие не видно вообще ничем, кроме растущей базы."""
    assert "retention" in REQUIRED_WORKERS


def test_the_backup_runs_before_the_cleanup():
    """Порядок важен: если чистка когда-нибудь удалит лишнее, копия, снятая ДО
    неё, окажется тем единственным, что это исправит."""
    from app.workers.scheduler import (BACKUP_FIRST_RUN_DELAY,
                                       RETENTION_FIRST_RUN_DELAY)

    assert BACKUP_FIRST_RUN_DELAY < RETENTION_FIRST_RUN_DELAY


def test_daily_jobs_ask_for_an_early_first_run():
    """Суточное задание на `interval` до первого запуска не доживает: APScheduler
    считает от момента добавления, а воркер перезапускается чаще раза в сутки.
    На этом уже пропала выгрузка каталога — не запускалась НИ РАЗУ."""
    source = open("app/workers/scheduler.py", encoding="utf-8").read()

    for job in ('id="backup"', 'id="retention"'):
        block = source.split(job, 1)[1][:200]
        assert "next_run_time" in block, f"{job} не доживёт до первого прогона"


def test_a_fresh_install_is_not_nagged_about_backups(db, tmp_path, monkeypatch):
    """Задание бэкапа ещё ни разу не отрабатывало — воркер только поднялся.
    «Не отработало ни разу» — вопрос /health, и там он задан. Отчёт на
    исправной системе обязан молчать."""
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./x.db")

    assert _check_backup_missing(db) is None

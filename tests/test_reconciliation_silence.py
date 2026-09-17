"""Молчаливо остановившаяся сверка.

На боевом 17.09.2026 воркер два часовых цикла подряд писал в лог
«reconciliation: нет свежего файла выгрузки остатков — пропуск», а `/health`
всё это время отдавал 200. Формально всё честно: задание отработало, ошибки не
было, сверять оказалось нечего — heartbeat зелёный.

Но для системы про остатки «сверка запускалась» и «остатки сверены» — разные
вещи. Пока 1С не отдаёт выгрузку, остаток в приложении живёт сам по себе:
расходится с реальным складом и в этом виде уезжает на площадки. Это прямая
дорога к оверселлу, и увидеть её было нельзя — ни по `/health`, ни по странице
«Диагностика».

Поэтому факт применения снимка отмечается отдельно (`reconciliation_applied`) и
проверяется на возраст наравне с воркерами.
"""
from datetime import timedelta

from app.models import WorkerHeartbeat
from app.routers.health import REQUIRED_WORKERS, SCHEDULER_START_MARKER
from app.timeutils import now_utc
from app.workers import scheduler
from app.workers.scheduler import RECONCILIATION_APPLIED


# ------------------------------------------------ прогон задания сверки

def _run_reconciliation(monkeypatch, db, rows):
    """Задание сверки на подменённом обмене с 1С.

    `rows` — то, что вернула выгрузка: непустой список означает, что снимок
    пришёл, пустой — что свежего файла нет. Сама сверка и импорт номенклатуры
    подменены: здесь проверяется только бухгалтерия отметок, у их логики свои
    тесты (`test_reconciliation.py`).
    """
    monkeypatch.setattr(db, "close", lambda: None)      # задание закрывает сессию в finally
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(scheduler, "_build_ftp_exchange", lambda: object())
    monkeypatch.setattr(scheduler, "fetch_stock_export_snapshot",
                        lambda exchange, not_older_than=None: (rows, now_utc()))
    monkeypatch.setattr(scheduler, "import_product_master", lambda db, rows: {"created": 0})
    monkeypatch.setattr(scheduler, "run_reconciliation",
                        lambda db, stock, **kw: {"normal": len(stock)})
    scheduler.job_reconciliation()


def _mark(db, name: str):
    return db.query(WorkerHeartbeat).filter(WorkerHeartbeat.worker_name == name).first()


def test_applied_snapshot_leaves_both_marks(monkeypatch, db):
    """Снимок пришёл и применён — отмечается и запуск задания, и сам факт сверки."""
    _run_reconciliation(monkeypatch, db, [{"barcodes": ["4601234567890"], "quantity": 7}])

    assert _mark(db, "reconciliation") is not None
    assert _mark(db, RECONCILIATION_APPLIED) is not None


def test_skipped_cycle_keeps_the_job_green(monkeypatch, db):
    """Пропуск — не ошибка задания: 1С просто не успела ответить, воркер жив.
    Красить его в аварию значило бы держать мониторинг красным на ровном месте."""
    _run_reconciliation(monkeypatch, db, [])

    job = _mark(db, "reconciliation")
    assert job is not None
    assert job.last_success is True


def test_skipped_cycle_leaves_no_applied_mark(monkeypatch, db):
    """Главное в этой находке: сверять было нечем — значит остатки НЕ сверены,
    и отметка об этом появляться не должна."""
    _run_reconciliation(monkeypatch, db, [])

    assert _mark(db, RECONCILIATION_APPLIED) is None


def test_skipped_cycle_does_not_refresh_an_older_mark(monkeypatch, db):
    """Отметка обязана СТАРЕТЬ, пока 1С молчит. Если бы пропуск её обновлял,
    она была бы вечно свежей и не отличалась бы от heartbeat самого задания —
    то есть ровно так же не показала бы остановку сверки."""
    _run_reconciliation(monkeypatch, db, [{"barcodes": ["4601234567890"], "quantity": 7}])
    applied = _mark(db, RECONCILIATION_APPLIED)
    applied.last_run_at = now_utc() - timedelta(hours=5)
    db.commit()
    stale_at = applied.last_run_at

    _run_reconciliation(monkeypatch, db, [])            # 1С опять не ответила

    db.refresh(applied)
    assert applied.last_run_at == stale_at


# ------------------------------------------------ /health видит остановку

def _hb(web_db, name: str, *, age_seconds: int = 0):
    web_db.add(WorkerHeartbeat(worker_name=name, last_success=True,
                               last_run_at=now_utc() - timedelta(seconds=age_seconds)))
    web_db.commit()


def _all_required(web_db, uptime_seconds: int = 3600, skip: str = ""):
    _hb(web_db, SCHEDULER_START_MARKER, age_seconds=uptime_seconds)
    for name in REQUIRED_WORKERS:
        if name != skip:
            _hb(web_db, name)


def test_health_is_green_while_snapshots_are_applied(web_db, client):
    _all_required(web_db)

    r = client.get("/health")

    assert r.status_code == 200


def test_health_goes_red_when_snapshots_stopped_coming(web_db, client):
    """Тот самый боевой случай: все воркеры живы и зелены, а сверка уже
    несколько часов не применяла ни одного снимка."""
    _all_required(web_db, skip=RECONCILIATION_APPLIED)
    _hb(web_db, RECONCILIATION_APPLIED, age_seconds=4 * 3600)

    r = client.get("/health")

    assert r.status_code == 503
    marks = {w["worker"]: w for w in r.json()["workers"]}
    assert marks[RECONCILIATION_APPLIED]["stale"] is True


def test_a_single_late_snapshot_is_not_an_alarm(web_db, client):
    """Порог — три часовых цикла, а не «сразу». Один пропуск бывает штатно: 1С
    отвечает дольше, чем окно в 5 минут между запросом выгрузки и сверкой.
    Красить мониторинг на каждом таком случае значит приучить смотреть мимо."""
    _all_required(web_db, skip=RECONCILIATION_APPLIED)
    _hb(web_db, RECONCILIATION_APPLIED, age_seconds=2 * 3600)

    r = client.get("/health")

    assert r.status_code == 200


def test_health_waits_out_the_first_cycles_after_restart(web_db, client):
    """Сразу после рестарта отметки ещё нет — это норма, а не авария: сверка
    идёт через 5 минут после запроса выгрузки, и первый снимок может не успеть."""
    _all_required(web_db, uptime_seconds=600, skip=RECONCILIATION_APPLIED)

    r = client.get("/health")

    assert r.status_code == 200


def test_health_reports_a_snapshot_that_never_arrived(web_db, client):
    """А вот если снимка нет спустя три часовых цикла — 1С не отдаёт выгрузку
    вовсе, и молчать об этом нельзя."""
    _all_required(web_db, uptime_seconds=4 * 3600, skip=RECONCILIATION_APPLIED)

    r = client.get("/health")

    assert r.status_code == 503
    assert RECONCILIATION_APPLIED in r.json()["missing_workers"]


# ------------------------------------------------ оператор видит то же самое

def test_diagnostics_shows_when_the_snapshot_was_last_applied(logged_in_client, web_db):
    """`/health` читает мониторинг, а человек — «Диагностику»: там обе строки
    должны стоять рядом, иначе разъехавшиеся времена не с чем сравнить."""
    _hb(web_db, "reconciliation")
    _hb(web_db, RECONCILIATION_APPLIED, age_seconds=4 * 3600)

    body = logged_in_client.get("/diagnostics").text

    assert RECONCILIATION_APPLIED in body

"""Отметки фоновых заданий и их сроки протухания для /health.

Добавил задание — впиши срок, не меньше двух интервалов: суточное задание со
сроком по умолчанию протухло бы сразу после прогона (у sync_admin так было трижды).
"""
from datetime import timedelta

from sqlalchemy.orm import Session

from priceapp.models import WorkerHeartbeat
from priceapp.timeutils import now_utc

# Задание -> (срок протухания, с; обязательно ли). Обязательное, не отработавшее
# ни разу, тоже считается протухшим — иначе не навесившееся задание невидимо.
EXPECTED = {
    "onec_exchange": (180, True),          # каждые 30 с
    "price_dispatch": (600, True),         # каждые 2 минуты
    "rate": (2 * 6 * 3600 + 600, True),    # раз в 6 часов
    "daily_refresh": (2 * 26 * 3600, False),
    "backup": (2 * 26 * 3600, False),
}


def beat(db: Session, name: str, ok: bool = True, error: str = "") -> None:
    if not db.is_active:
        db.rollback()
    row = db.get(WorkerHeartbeat, name)
    if row is None:
        row = WorkerHeartbeat(name=name)
        db.add(row)
    row.last_run_at = now_utc()
    row.last_success = ok
    row.last_error = (error or "")[:2000]
    db.commit()


def stale_workers(db: Session) -> list[str]:
    out = []
    now = now_utc()
    for name, (seconds, required) in EXPECTED.items():
        row = db.get(WorkerHeartbeat, name)
        if row is None or row.last_run_at is None:
            if required:
                out.append(f"{name}: не отработало ни разу")
            continue
        if now - row.last_run_at > timedelta(seconds=seconds):
            out.append(f"{name}: последний прогон {row.last_run_at:%d.%m %H:%M} UTC")
        elif required and not row.last_success:
            out.append(f"{name}: ошибка — {row.last_error[:200]}")
    return out

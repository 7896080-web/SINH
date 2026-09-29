"""Отметки фоновых заданий и их сроки протухания для /health.

Добавил задание — впиши срок сюда, не меньше двух интервалов: суточное
задание со сроком по умолчанию протухло бы сразу после своего прогона и
держало /health красным (у sync_admin так уже было трижды).
"""
from datetime import timedelta

from sqlalchemy.orm import Session

from markapp.models import WorkerHeartbeat
from markapp.timeutils import now_utc

# Задание -> (срок протухания в секундах, обязательно ли оно быть).
# Обязательное, не отработавшее ни разу, тоже считается протухшим: иначе
# не навесившееся задание было бы невидимо.
EXPECTED = {
    "onec_exchange": (180, True),          # каждые 30 с
    "nk_fetch": (300, True),               # каждую минуту
    "backup": (2 * 26 * 3600, False),      # раз в сутки; первый прогон — через 10 мин после старта
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
    row.last_error = error[:2000]
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
            # Свежая отметка с ошибкой — задание живо, но работы не делает. Раньше
            # /health смотрел только на возраст и был зелёным, пока обмен с 1С
            # падал каждые 30 секунд.
            out.append(f"{name}: ошибка — {row.last_error[:200]}")
    return out

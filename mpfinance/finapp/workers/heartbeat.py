"""Отметки фоновых заданий и их сроки протухания для /health.

Добавил задание — впиши срок, не меньше двух интервалов: суточное задание со
сроком по умолчанию протухло бы сразу после прогона.
"""
from datetime import timedelta

from sqlalchemy.orm import Session

from finapp.models import WorkerHeartbeat
from finapp.timeutils import now_utc

# Задание -> (срок протухания, с; обязательно ли). Обязательное, не отработавшее
# ни разу, тоже считается протухшим — иначе не навесившееся задание невидимо.
EXPECTED = {
    "onec_exchange": (900, True),               # каждые 30 с
    # Каждые 5 минут, но одна заявка WB идёт десятки минут (запрос в минуту);
    # отметка ставится после каждого шага.
    "finance_fetch": (2 * 3600, True),
    "daily": (2 * 26 * 3600, False),            # раз в сутки: справочник, себестоимость, чистка
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
    from finapp.templating import local

    out = []
    now = now_utc()
    for name, (seconds, required) in EXPECTED.items():
        row = db.get(WorkerHeartbeat, name)
        if row is None or row.last_run_at is None:
            if required:
                out.append(f"{name}: не отработало ни разу")
            continue
        last = row.last_run_at
        if name == "backup":
            # Свежую копию снимает и накат — тогда задание пропускает работу и
            # отметку не пишет; судить надо по самой копии.
            from finapp.backup import last_backup
            lb = last_backup()
            last = max(last, lb) if lb else last
        if now - last > timedelta(seconds=seconds):
            out.append(f"{name}: последний прогон {local(last)}")
        elif required and not row.last_success:
            out.append(f"{name}: ошибка — {row.last_error[:200]}")
    return out

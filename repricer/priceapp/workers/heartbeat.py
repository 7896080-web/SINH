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
    # Каждые 30 с, но в одном потоке с отправкой цен: пачка WB/Kit с повторами идёт минутами,
    # и 180 с давали ложное 503 (аудит 14.10).
    "onec_exchange": (900, True),
    "price_dispatch": (600, True),         # каждые 2 минуты
    "rate": (2 * 6 * 3600 + 600, True),    # раз в 6 часов
    "daily_refresh": (2 * 26 * 3600, False),
    "wb_poll": (2 * 6 * 3600 + 1800, False),   # опрос WB: при запуске и раз в 6 часов
    "backup": (2 * 26 * 3600, False),
    "wb_promo": (2 * 3 * 3600 + 1800, False),   # календарь акций WB: раз в 3 часа
    # Выход из акций по сроку: раз в 5 минут, но в тяжёлом потоке за суточным обновлением и опросом WB.
    "wb_promo_leave": (2 * 3600, False),
    # Сбор данных WB: каждые 30 с, но в тяжёлом потоке за суточным обновлением и опросом WB.
    "wb_data": (2 * 3600, False),
    # Раз в 10 минут, при правках — сразу; но в тяжёлом потоке за суточным обновлением и опросом
    # WB, которые на боевом каталоге идут десятки минут.
    "attention": (2 * 3600, False),
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
        last = row.last_run_at
        if name == "backup":
            # Свежую копию снимает и накат — тогда задание пропускает работу и отметку не
            # пишет; судить надо по самой копии, а не по отметке (аудит 14.10).
            from priceapp.backup import last_backup
            lb = last_backup()
            last = max(last, lb) if lb else last
        if now - last > timedelta(seconds=seconds):
            from priceapp.templating import local
            out.append(f"{name}: последний прогон {local(last)}")
        elif required and not row.last_success:
            out.append(f"{name}: ошибка — {row.last_error[:200]}")
    return out


def failed_or_noted(db: Session) -> list[tuple[str, str, bool]]:
    """(задание, текст, это ошибка) — у ЛЮБОГО задания последняя отметка неуспешна
    или с оговоркой. Для «Внимания»: упавшие суточное обновление и копия базы
    раньше были видны только в таблице «Диагностики»."""
    out = []
    for row in db.query(WorkerHeartbeat):
        if not row.last_success:
            out.append((row.name, row.last_error[:200], True))
        elif row.last_error and row.name in ("daily_refresh", "onec_exchange", "price_dispatch", "backup", "wb_promo", "wb_promo_leave",
                                                    "wb_data"):
            out.append((row.name, row.last_error[:200], False))
    return out

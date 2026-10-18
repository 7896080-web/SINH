"""Смены и график работы (решение владельца 18.10.2026).

* Смена — состав продавцов одного магазина. Каждую среду смены меняются магазинами, чтобы шансы на торговлю были
  равными. Магазин смены на любую дату считается от «якоря»: среды, с которой смена стоит в `crews.store`.
* Рабочий день продавца — строка в `schedule`; магазин дня — `schedule.store` (замена при болезни) или магазин
  его смены.
* Минимум в каждом магазине: в будни `staffing.weekday_min`, в субботу и воскресенье `staffing.weekend_min`.
  Сильные продавцы (`sellers.weekend_key`) должны по возможности работать в субботу и воскресенье.
"""
from __future__ import annotations

from datetime import date, timedelta

from .config import Settings
from .db import DB

WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def wednesday(d: date) -> date:
    """Среда, с которой начинается неделя смены, содержащая `d`."""
    return d - timedelta(days=(d.weekday() - 2) % 7)


def crew_store(crew, d: date, stores: list[str]) -> str:
    """Магазин смены в день `d`: каждую среду — следующий магазин по кругу."""
    weeks = (wednesday(d) - date.fromisoformat(crew["anchor"])).days // 7
    i = stores.index(crew["store"]) if crew["store"] in stores else 0
    return stores[(i + weeks) % len(stores)]


def ensure_crews(db: DB, cfg: Settings) -> None:
    """Две смены по умолчанию — по одной на магазин (переименовать и наполнить можно на странице «Смены»)."""
    if db.one("SELECT 1 FROM crews"):
        return
    anchor = wednesday(date.today()).isoformat()
    with db.tx() as c:
        for i, store in enumerate(cfg.stores, 1):
            c.execute("INSERT INTO crews(name, store, anchor) VALUES(?,?,?)", (f"Смена {i}", store, anchor))


def crews(db: DB) -> list:
    return db.q("SELECT * FROM crews ORDER BY id")


def default_store(db: DB, cfg: Settings, sid: int, d: date) -> str | None:
    s = db.seller(sid)
    crew = db.one("SELECT * FROM crews WHERE id=?", s["crew_id"]) if s and s["crew_id"] else None
    return crew_store(crew, d, cfg.stores) if crew else None


def store_on(db: DB, cfg: Settings, sid: int, d: date) -> str | None:
    """Магазин продавца в день `d`: из графика (замена) или его смены; None — не знаем."""
    r = db.one("SELECT store FROM schedule WHERE seller_id=? AND day=?", sid, d.isoformat())
    if r and r["store"]:
        return r["store"]
    return default_store(db, cfg, sid, d)


def staffing(db: DB, cfg: Settings, start: date, end: date) -> dict[tuple[str, str], list[str]]:
    """{(день, магазин): [имена]} по графику работы."""
    out: dict[tuple[str, str], list[str]] = {}
    for r in db.q("SELECT g.day, g.store, g.seller_id, s.name FROM schedule g JOIN sellers s ON s.id=g.seller_id "
                  "WHERE g.day BETWEEN ? AND ? AND s.active=1", start.isoformat(), end.isoformat()):
        store = r["store"] or default_store(db, cfg, r["seller_id"], date.fromisoformat(r["day"]))
        if store:
            out.setdefault((r["day"], store), []).append(r["name"])
    return out


def minimum(cfg: Settings, d: date) -> int:
    return cfg.staffing.weekend_min if d.weekday() >= 5 else cfg.staffing.weekday_min


def problems(db: DB, cfg: Settings, start: date, end: date) -> list[str]:
    """Нарушения правил графика: недобор продавцов в магазине и сильные продавцы не на выходных."""
    have = staffing(db, cfg, start, end)
    keys = db.q("SELECT id, name FROM sellers WHERE active=1 AND weekend_key=1")
    out = []
    d = start
    while d <= end:
        for store in cfg.stores:
            n, need = len(have.get((d.isoformat(), store), [])), minimum(cfg, d)
            if n < need:
                out.append(f"{WD[d.weekday()]} {d:%d.%m}, {store}: {n} из {need}")
        if d.weekday() >= 5:
            off = [k["name"] for k in keys
                   if not db.one("SELECT 1 FROM schedule WHERE seller_id=? AND day=?", k["id"], d.isoformat())]
            if off:
                out.append(f"{WD[d.weekday()]} {d:%d.%m}: не работает {', '.join(off)} (сильные продавцы — на выходные)")
        d += timedelta(days=1)
    return out

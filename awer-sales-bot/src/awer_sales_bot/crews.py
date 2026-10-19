"""Смены и график работы (решение владельца 18.10.2026).

* Смена — состав продавцов одного магазина. Каждую среду смены меняются магазинами, чтобы шансы на торговлю были
  равными. Магазин смены на любую дату считается от «якоря»: среды, с которой смена стоит в `crews.store`.
* Рабочий день продавца — строка в `schedule`; магазин дня — `schedule.store` (замена при болезни) или магазин
  его смены.
* Минимум в каждом магазине: в будни `staffing.weekday_min`, в субботу и воскресенье `staffing.weekend_min`.
  Сильные продавцы (`sellers.weekend_key`) должны по возможности работать в субботу и воскресенье.
"""
from __future__ import annotations

import calendar
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
                  "WHERE g.day BETWEEN ? AND ? AND s.active=1 AND s.role<>'test'", start.isoformat(), end.isoformat()):
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


MAX_RUN = 5  # не больше рабочих дней подряд в предложении графика


def suggest(db: DB, cfg: Settings, month: str) -> tuple[dict[int, dict[int, str | None]], list[str]]:
    """Предложение графика на месяц по правилам владельца (18.10.2026). Ничего не сохраняет — владелец смотрит,
    правит галочки и сохраняет сам.

    * Продавец работает в магазине своей смены (по средам смены меняются магазинами).
    * Сначала минимум: в каждом магазине в будни weekday_min, в сб и вс weekend_min; сильные продавцы — на выходных.
    * Потом добор до нормы norm_shifts рабочих дней — в дни, где в его магазине меньше всего людей.
    * Не больше MAX_RUN дней подряд. Продавцы без смены и тестовые карточки не планируются.
    Возвращает ({seller_id: {число: None}}, [чего не хватило]); None — магазин его смены."""
    y, m = map(int, month.split("-"))
    days = [date(y, m, k) for k in range(1, calendar.monthrange(y, m)[1] + 1)]
    cs = {c["id"]: c for c in crews(db)}
    people = [s for s in db.sellers() if s["role"] in ("seller", "admin") and s["crew_id"] in cs]
    target = min(cfg.norm_shifts, len(days))
    work: dict[int, set[int]] = {s["id"]: set() for s in people}
    staff: dict[tuple[int, str], int] = {}
    store = {(s["id"], d.day): crew_store(cs[s["crew_id"]], d, cfg.stores) for s in people for d in days}

    def run_if_added(sid: int, k: int) -> int:
        left = right = 0
        while k - left - 1 in work[sid]:
            left += 1
        while k + right + 1 in work[sid]:
            right += 1
        return left + right + 1

    def add(sid: int, k: int) -> None:
        work[sid].add(k)
        staff[(k, store[(sid, k)])] = staff.get((k, store[(sid, k)]), 0) + 1

    short = []
    for d in days:  # 1. минимум в каждом магазине
        weekend = d.weekday() >= 5
        for st in cfg.stores:
            pool = [s for s in people if store[(s["id"], d.day)] == st]
            need = minimum(cfg, d)
            pool.sort(key=lambda s: (run_if_added(s["id"], d.day) > MAX_RUN, len(work[s["id"]]) >= target,
                                     not (weekend and s["weekend_key"]), len(work[s["id"]])))
            for s in pool[:need]:
                add(s["id"], d.day)
            if len(pool) < need:
                short.append(f"{WD[d.weekday()]} {d:%d.%m}, {st}: в смене {len(pool)} из {need} нужных")
    for s in people:  # 2. добор до нормы — в дни, где в его магазине меньше людей
        sid = s["id"]
        while len(work[sid]) < target:
            free = [d for d in days if d.day not in work[sid] and run_if_added(sid, d.day) <= MAX_RUN]
            if not free:
                break
            best = min(free, key=lambda d: (staff.get((d.day, store[(sid, d.day)]), 0),
                                            not (s["weekend_key"] and d.weekday() >= 5), d.day))
            add(sid, best.day)
    return {sid: {k: None for k in sorted(ks)} for sid, ks in work.items()}, short


def advice(db: DB, cfg: Settings, month: str) -> list[str]:
    """Рекомендации по рабочим дням для страницы «График работы»."""
    sellers = [s for s in db.sellers() if s["role"] in ("seller", "admin")]
    if not sellers:
        return ["Продавцов в списке пока нет: они появятся после загрузки истории продаж из 1С. Потом на странице "
                "«Смены» распределите их по сменам — и здесь можно будет предложить график."]
    out = []
    no_crew = [s["name"] for s in sellers if not s["crew_id"]]
    if no_crew:
        out.append("Не в смене: " + ", ".join(no_crew) + " — отметьте на странице «Смены»: без смены бот не знает, "
                   "в каком магазине они работают, и не предлагает им дни.")
    saved = db.schedule_days(month)
    if not saved:
        out.append(f"График на {month} не заполнен. Нажмите «Предложить график на месяц»: бот расставит рабочие дни "
                   f"по правилам (норма {cfg.norm_shifts} дней, минимум в магазинах, сильные — на выходных), вы "
                   "проверите и сохраните. Пока графика нет, рабочими считаются дни с продажами.")
        return out
    low = [f"{s['name']} — {len(saved.get(s['id'], {}))}" for s in sellers
           if s["crew_id"] and len(saved.get(s["id"], {})) < cfg.norm_shifts]
    high = [f"{s['name']} — {len(saved[s['id']])}" for s in sellers if len(saved.get(s["id"], {})) > cfg.norm_shifts]
    if low:
        out.append(f"Меньше нормы {cfg.norm_shifts} рабочих дней: " + ", ".join(low) +
                   ". План месяца у них будет меньше полного — в той же доле.")
    if high:
        out.append(f"Больше нормы {cfg.norm_shifts}: " + ", ".join(high) + ". План растёт вместе с рабочими днями.")
    return out

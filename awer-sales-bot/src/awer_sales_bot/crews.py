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
import random
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
# цена нарушений в предложении графика (чем больше, тем важнее правило)
W_MISS, W_KEY, W_LONG = 100, 40, 30  # недобор в магазине; старший не на выходных; больше MAX_RUN дней подряд
W_ON, W_OFF = 3, 2  # отступление от 3/2: рабочих дней подряд не 3, выходных подряд не 2
W_DAYS, W_WEEKEND, W_SWAP, W_OVER = 1.5, 4, 5, 1  # дни ≠ доле; выходные не поровну; замена; сверх минимума
STEPS = 15000  # перестановок при подборе (≈ 0,4 с; больше — тот же результат)


def suggest(db: DB, cfg: Settings, month: str) -> tuple[dict[int, dict[int, str | None]], list[str]]:
    """Предложение графика на месяц по правилам владельца (18–19.10.2026). Ничего не сохраняет — владелец смотрит,
    правит и сохраняет сам.

    * В каждом магазине в будни weekday_min, в сб и вс weekend_min.
    * Старшие (и отмеченные «сильный продавец») — в каждую субботу и воскресенье.
    * У всех по возможности 3/2: три дня работы, два выходных; не больше MAX_RUN дней подряд.
    * Остальные работают в выходные примерно поровну; рабочих дней у всех примерно поровну.
    * Состав смен сохраняется: продавец — в магазине своей смены (по средам смены меняются магазинами), замена в
      другой магазин — только когда без неё не набрать минимум или не уравнять выходные.
    Заготовка 3/2, затем перебор перестановок (замена дня между продавцами, сдвиг выходного, магазин): остаётся то,
    что меньше нарушает правила (цены W_*). Перебор с постоянным зерном — для того же месяца тот же результат.
    Возвращает ({seller_id: {число: магазин замены или None — магазин своей смены}}, [где не хватило людей])."""
    y, m = map(int, month.split("-"))
    days = [date(y, m, k) for k in range(1, calendar.monthrange(y, m)[1] + 1)]
    n, stores = len(days), cfg.stores
    cs = {c["id"]: c for c in crews(db)}
    people = [s for s in db.sellers() if s["role"] in ("seller", "admin") and s["crew_id"] in cs]
    if not people:
        return {}, []
    ids = [s["id"] for s in people]
    key = {s["id"]: s["role"] == "admin" or bool(s["weekend_key"]) for s in people}
    own = {sid: [crew_store(cs[s["crew_id"]], d, stores) for d in days] for sid, s in zip(ids, people)}
    wk = [d.weekday() >= 5 for d in days]
    need = [minimum(cfg, d) for d in days]
    target = max(cfg.norm_shifts, sum(need) * len(stores) / len(ids))  # доля каждого
    plan: dict[int, list[str | None]] = {sid: [None] * n for sid in ids}

    # заготовка: старшие — выходные + понедельник и четверг; остальные — 3/2 со сдвигом внутри смены
    by_crew: dict[int, list[int]] = {}
    for s in people:
        by_crew.setdefault(s["crew_id"], []).append(s["id"])
    for members in by_crew.values():
        plain = [sid for sid in members if not key[sid]]
        for i, sid in enumerate(plain):
            off = i * 5 // max(1, len(plain))
            for k in range(n):
                if (k + off) % 5 < 3:
                    plan[sid][k] = own[sid][k]
        for sid in members:
            if key[sid]:
                for k, d in enumerate(days):
                    if wk[k] or d.weekday() in (0, 3):
                        plan[sid][k] = own[sid][k]

    def seller_cost(sid: int) -> float:
        row, c, k = plan[sid], 0.0, 0
        while k < n:
            j = k
            while j < n and (row[j] is None) == (row[k] is None):
                j += 1
            length, edge = j - k, k == 0 or j == n
            if row[k] is not None:
                c += W_LONG * max(0, length - MAX_RUN) + (0 if edge and length < 3 else W_ON * abs(length - 3))
            elif not edge:
                c += W_OFF * abs(length - 2)
            k = j
        worked = sum(1 for x in row if x is not None)
        c += W_DAYS * (worked - target) ** 2
        c += W_SWAP * sum(1 for k in range(n) if row[k] is not None and row[k] != own[sid][k])
        if key[sid]:
            c += W_KEY * sum(1 for k in range(n) if wk[k] and row[k] is None)
        return c

    def day_cost(k: int) -> float:
        c = 0.0
        for st in stores:
            have = sum(1 for sid in ids if plan[sid][k] == st)
            c += W_MISS * max(0, need[k] - have) + W_OVER * max(0, have - need[k])
        return c

    def weekend_cost() -> float:
        counts = [sum(1 for k in range(n) if wk[k] and plan[sid][k] is not None) for sid in ids if not key[sid]]
        if not counts:
            return 0.0
        mean = sum(counts) / len(counts)
        return W_WEEKEND * sum((x - mean) ** 2 for x in counts)

    sc = {sid: seller_cost(sid) for sid in ids}
    dc = [day_cost(k) for k in range(n)]
    wc = weekend_cost()
    rng = random.Random(y * 100 + m)
    for _ in range(STEPS):
        r = rng.random()
        k = rng.randrange(n)
        changed: list[tuple[int, int, str | None]] = []  # (продавец, день, прежнее значение)
        if r < 0.4:  # день переходит от одного продавца к другому, магазин тот же
            on = [sid for sid in ids if plan[sid][k] is not None]
            off = [sid for sid in ids if plan[sid][k] is None]
            if not on or not off:
                continue
            a_, b_ = rng.choice(on), rng.choice(off)
            changed = [(a_, k, plan[a_][k]), (b_, k, None)]
            plan[b_][k], plan[a_][k] = plan[a_][k], None
        elif r < 0.65:  # сдвиг: выходной и рабочий день меняются местами
            sid = rng.choice(ids)
            j = k + 1 if k + 1 < n else k - 1
            if (plan[sid][k] is None) == (plan[sid][j] is None):
                continue
            changed = [(sid, k, plan[sid][k]), (sid, j, plan[sid][j])]
            plan[sid][k], plan[sid][j] = (own[sid][k] if plan[sid][j] else None), (own[sid][j] if plan[sid][k] else None)
        elif r < 0.85:  # выйти или не выйти
            sid = rng.choice(ids)
            changed = [(sid, k, plan[sid][k])]
            plan[sid][k] = None if plan[sid][k] else own[sid][k]
        else:  # другой магазин (замена) или обратно в свой
            sid = rng.choice(ids)
            if plan[sid][k] is None:
                continue
            changed = [(sid, k, plan[sid][k])]
            plan[sid][k] = rng.choice([st for st in stores if st != plan[sid][k]])
        who = {c[0] for c in changed}
        when = {c[1] for c in changed}
        new_sc = {sid: seller_cost(sid) for sid in who}
        new_dc = {d: day_cost(d) for d in when}
        new_wc = weekend_cost()
        delta = sum(new_sc[x] - sc[x] for x in who) + sum(new_dc[d] - dc[d] for d in when) + new_wc - wc
        if delta <= 0:
            sc.update(new_sc)
            for d, v in new_dc.items():
                dc[d] = v
            wc = new_wc
        else:
            for sid, d, old in reversed(changed):
                plan[sid][d] = old
    short = []
    for k, d in enumerate(days):
        for st in stores:
            have = sum(1 for sid in ids if plan[sid][k] == st)
            if have < need[k]:
                short.append(f"{WD[d.weekday()]} {d:%d.%m}, {st}: {have} из {need[k]} — не хватает людей в сменах")
    return {sid: {k + 1: (None if plan[sid][k] == own[sid][k] else plan[sid][k])
                  for k in range(n) if plan[sid][k] is not None} for sid in ids}, short


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

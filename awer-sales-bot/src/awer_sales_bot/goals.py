"""Цель недели — один показатель качества продаж, где продавец больше всего отстаёт от команды.

Неделя — понедельник–воскресенье. Цель ставится на неделю, в которую уходит утреннее сообщение, по данным
4 недель до неё (`motivation.goal_lookback_days`): половина разрыва с командой, но не меньше +5 % к своему
уровню. Если отставания нет — +5 % к своим вещам за смену (в чеке). Итог недели подводится в понедельник.
На ставку цель не влияет (docs/SPEC.md). Создание и закрытие — только из jobs (`ensure_week`), сообщения читают.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import metrics as M
from .config import Settings
from .db import DB

# показатель: (название, больше — лучше, тема урока)
METRICS = {
    "upt": ("вещей в чеке", True, "items"),
    "items_per_shift": ("вещей за смену", True, "items"),
    "avg_price": ("средняя цена вещи", True, "avg_price"),
    "discount_share": ("доля скидок", False, "discount"),
}
MIN_SHIFTS = 3  # меньше смен за 4 недели — цели нет (новичку — программа новичка)


@dataclass
class Goal:
    week: date
    seller_id: int
    metric: str
    baseline: float | None
    team: float | None
    target: float
    result: float | None = None
    done: int | None = None

    @property
    def name(self) -> str:
        return METRICS[self.metric][0]

    @property
    def topic(self) -> str:
        return METRICS[self.metric][2]

    def fmt(self, v: float | None) -> str:
        return fmt(self.metric, v)

    def reached(self, v: float | None) -> bool:
        if v is None:
            return False
        return v >= self.target if METRICS[self.metric][1] else v <= self.target


def fmt(metric: str, v: float | None) -> str:
    if v is None:
        return "—"
    if metric == "avg_price":
        return f"{v:,.0f}".replace(",", " ") + " ₽"
    if metric == "discount_share":
        return f"{v:.0%}"
    return f"{v:.1f}".replace(".", ",")


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def value(m: M.SellerMetrics, metric: str) -> float | None:
    return getattr(m, metric)


def choose(db: DB, cfg: Settings, sid: int, week: date) -> Goal | None:
    start = week - timedelta(days=cfg.motivation.goal_lookback_days)
    end = week - timedelta(days=1)
    me, tm = M.seller(db, sid, start, end), M.team(db, start, end)
    if me.shifts < MIN_SHIFTS:
        return None
    items = "upt" if me.has_receipts else "items_per_shift"
    best: tuple[float, str] | None = None
    for metric in (items, "avg_price", "discount_share"):
        mv, tv = value(me, metric), value(tm, metric)
        if mv is None or not tv:
            continue
        gap = (tv - mv) / tv if METRICS[metric][1] else (mv - tv) / tv
        if gap > 0.05 and (best is None or gap > best[0]):
            best = (gap, metric)
    if best is None:  # отставания нет — расти от своего уровня
        mv = value(me, items)
        if not mv:
            return None
        return Goal(week, sid, items, mv, value(tm, items), mv * 1.05)
    metric = best[1]
    mv, tv = value(me, metric), value(tm, metric)
    if METRICS[metric][1]:
        target = max(mv + (tv - mv) / 2, mv * 1.05)
    else:
        target = min(mv - (mv - tv) / 2, mv * 0.95)
    return Goal(week, sid, metric, mv, tv, target)


def _row(r) -> Goal:
    return Goal(date.fromisoformat(r["week"]), r["seller_id"], r["metric"], r["baseline"], r["team"], r["target"],
                r["result"], r["done"])


def get(db: DB, sid: int, week: date) -> Goal | None:
    r = db.one("SELECT * FROM weekly_goals WHERE week=? AND seller_id=?", week.isoformat(), sid)
    return _row(r) if r else None


def progress(db: DB, g: Goal, end: date) -> float | None:
    """Показатель цели с понедельника недели по `end` включительно."""
    last = min(end, g.week + timedelta(days=6))
    if last < g.week:
        return None
    m = M.seller(db, g.seller_id, g.week, last)
    return value(m, g.metric) if m.shifts else None


def ensure_week(db: DB, cfg: Settings, today: date) -> list[Goal]:
    """Закрывает прошлые недели и ставит цели на неделю `today`. Повторный вызов ничего не меняет.

    Возвращает цели, закрытые этим вызовом (для «кого похвалить» и листка старшего)."""
    if not cfg.motivation.weekly_goal:
        return []
    week = monday(today)
    now = datetime.now().isoformat(timespec="seconds")
    closed = []
    for r in db.q("SELECT * FROM weekly_goals WHERE week<? AND closed_at IS NULL", week.isoformat()):
        g = _row(r)
        g.result = progress(db, g, g.week + timedelta(days=6))
        g.done = None if g.result is None else int(g.reached(g.result))
        with db.tx() as c:
            c.execute("UPDATE weekly_goals SET result=?, done=?, closed_at=? WHERE week=? AND seller_id=?",
                      (g.result, g.done, now, r["week"], g.seller_id))
        closed.append(g)
    have = {r["seller_id"] for r in db.q("SELECT seller_id FROM weekly_goals WHERE week=?", week.isoformat())}
    for s in db.sellers():
        if s["id"] in have:
            continue
        g = choose(db, cfg, s["id"], week)
        if g:
            with db.tx() as c:
                c.execute("INSERT OR IGNORE INTO weekly_goals(week,seller_id,metric,baseline,team,target) "
                          "VALUES(?,?,?,?,?,?)", (week.isoformat(), g.seller_id, g.metric, g.baseline, g.team, g.target))
    return closed


def lines(db: DB, sid: int, day: date) -> list[str]:
    """Строки для утреннего сообщения за `day` (сообщение уходит на следующий день)."""
    today = day + timedelta(days=1)
    week = monday(today)
    out = []
    if today == week:  # понедельник: итог прошлой недели
        prev = get(db, sid, week - timedelta(days=7))
        if prev and prev.done is not None:
            mark = "выполнена ✅" if prev.done else "не выполнена"
            out.append(f"Итог недели: цель «{prev.name} {prev.fmt(prev.target)}» {mark}, у вас {prev.fmt(prev.result)}")
    g = get(db, sid, week)
    if g:
        if today == week:
            out.append(f"🎯 **Цель новой недели:** {g.name} {g.fmt(g.target)} (сейчас {g.fmt(g.baseline)}, "
                       f"команда {g.fmt(g.team)})")
        else:
            now = g.fmt(progress(db, g, day))
            out.append(f"🎯 Цель недели: {g.name} {g.fmt(g.target)} — с понедельника у вас {now}")
    return out

"""Листок для разговоров 1:1 — по понедельникам старшим (роль admin) и владельцу (motivation.coach_sheet).

По каждому рядовому продавцу, работавшему последние 4 недели: за что похвалить (показатель, где он лучше
команды), что обсудить (цель недели), выполнена ли прошлая цель, урок недели и ответы на квиз прошлой недели.
Продавцам этот листок не уходит (скилл ton-dlya-prodavtsov: «кто что видит»).
"""
from __future__ import annotations

from datetime import date, timedelta

from . import goals
from . import metrics as M
from .config import Settings
from .db import DB


def strength(me: M.SellerMetrics, tm: M.SellerMetrics) -> str | None:
    """Показатель, в котором продавец сильнее всего обгоняет команду (от 5 %)."""
    items = "upt" if me.has_receipts else "items_per_shift"
    best: tuple[float, str] | None = None
    for metric in (items, "avg_price", "manual_share", "per_shift"):
        mv, tv = getattr(me, metric), getattr(tm, metric)
        if mv is None or not tv:
            continue
        if metric == "manual_share" and tv - mv < goals.MANUAL_GAP:
            continue  # «сильная сторона» в долях процента скидок — не сильная сторона
        higher = metric != "manual_share"
        gap = (mv - tv) / tv if higher else (tv - mv) / tv
        if gap > 0.05 and (best is None or gap > best[0]):
            best = (gap, metric)
    if best is None:
        return None
    m = best[1]
    if m == "per_shift":
        name, show = "продажи за рабочий день", lambda v: goals.fmt("avg_price", v)
    else:
        name, show = goals.METRICS[m][0], lambda v: goals.fmt(m, v)
    return f"{name} {show(getattr(me, m))} (команда {show(getattr(tm, m))})"


def sheet(db: DB, cfg: Settings, week: date) -> str:
    start, end = week - timedelta(days=cfg.motivation.goal_lookback_days), week - timedelta(days=1)
    prev = week - timedelta(days=7)
    tm = M.team(db, start, end)
    rows = []
    for s in db.sellers():
        if s["role"] != "seller":
            continue
        sid = s["id"]
        me = M.seller(db, sid, start, end)
        if not me.shifts:
            continue
        parts = []
        st = strength(me, tm)
        if st:
            parts.append(f"похвалить: {st}")
        g = goals.get(db, sid, week)
        if g:
            parts.append(f"обсудить: {g.name} {g.fmt(g.baseline)} → цель {g.fmt(g.target)}")
        pg = goals.get(db, sid, prev)
        if pg and pg.done is not None:
            parts.append("прошлая цель " + ("выполнена ✅" if pg.done else f"не выполнена ({pg.fmt(pg.result)})"))
        lesson = db.one("SELECT text FROM message_log WHERE day=? AND kind='lesson' AND seller_id=? AND status='ok'",
                        week.isoformat(), sid)
        if lesson:  # первая строка урока: «📚 **Урок недели: Название**»
            title = lesson["text"].splitlines()[0].split(": ", 1)[-1].strip("*")
            parts.append(f"урок недели «{title}»")
        q = db.one("SELECT COUNT(*) n, COALESCE(SUM(correct),0) ok FROM quiz_answers WHERE seller_id=? "
                   "AND answered_at>=? AND answered_at<?", sid, prev.isoformat(), week.isoformat())
        if q["n"]:
            parts.append(f"квиз прошлой недели: {q['ok']} из {q['n']}")
        rows.append(f"**{s['name']}** — " + "; ".join(parts or ["данных мало, просто спросите, как дела"]))
    if not rows:
        return ""
    return "\n".join([f"**Разговоры 1:1, неделя с {week:%d.%m}**",
                      "По 5 минут: сначала похвалить за сильное, потом одно — цель недели.", "", *rows])


def run(db: DB, cfg: Settings, sender, today: date) -> None:
    """Раз в неделю: старшим и владельцу. Повтор в ту же неделю не уходит."""
    if not cfg.motivation.coach_sheet:
        return
    week = goals.monday(today)
    text = sheet(db, cfg, week)
    if not text:
        return
    for s in db.sellers():
        if s["role"] == "admin" and s["max_user_id"]:
            sender.to_seller(week.isoformat(), "coach", s["id"], text)
    sender.to_owners(week.isoformat(), "coach", text, once=True)

"""Тексты сообщений (MAX, format=markdown). Все суммы — в рублях, округлены."""
from __future__ import annotations

from datetime import date, timedelta

from . import metrics as M
from .config import Settings
from .db import DB
from .planning.engine import threshold_amounts
from .planning.service import month_params, status
from .recommendations import advise

MONTHS = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]
MONTHS_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]


def rub(x: float | None) -> str:
    return "—" if x is None else f"{x:,.0f}".replace(",", " ") + " ₽"


def d1(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}".replace(".", ",")


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def daily(db: DB, cfg: Settings, sid: int, day: date) -> str:
    """Сообщение продавцу на следующий день после закрытия дня `day`."""
    s = db.seller(sid)
    st = status(db, cfg, sid, day)
    y = M.seller(db, sid, day, day)
    mtd = M.seller(db, sid, day.replace(day=1), day)
    tm = M.team(db, day.replace(day=1), day, exclude_admins=True)
    L = [f"Доброе утро, {s['first_name'] or s['name']}! Итоги {day.day} {MONTHS_GEN[day.month]}.", ""]
    if y.receipts or y.sales:
        L.append(f"**Вчера:** {rub(y.sales)} · чеков {y.receipts} · средний чек {rub(y.avg_check)} · "
                 f"вещей в чеке {d1(y.upt)}" if y.upt else f"**Вчера:** {rub(y.sales)}")
    else:
        L.append("**Вчера:** продаж нет (выходной?)")
    L.append("")
    if st.plan_month:
        L += [f"**{MONTHS[day.month].capitalize()}**, смен {st.worked} из {st.planned_shifts}:",
              f"Продано {rub(st.sales)} — {pct(st.sales / st.plan_month)} плана месяца",
              f"Базовый план: {rub(st.plan_month)}"]
        if st.to_base:
            per = f" (≈ {rub(st.per_shift_needed_base)} за смену)" if st.per_shift_needed_base else ""
            L.append(f"До базы осталось: **{rub(st.to_base)}**{per}")
        else:
            L.append("Базовый план выполнен ✅")
        if st.next_level and st.to_next is not None:
            per = f" (≈ {rub(st.per_shift_needed_next)} за смену)" if st.per_shift_needed_next else ""
            L.append(f"До ступени {pct(st.next_level)} (ставка {rub(st.next_rate)}): **{rub(st.to_next)}**{per}")
        elif st.plan_month:
            L.append("Максимальная ступень достигнута 🏆")
        if st.pace_per_shift:
            L.append(f"Ваш темп: {rub(st.pace_per_shift)} за смену → выполнение {pct(st.performance)}, "
                     f"ставка по итогам месяца при таком темпе {rub(st.projected_rate)}")
    else:
        L.append("План на месяц ещё не рассчитан — уточните у администратора.")
    L += ["", f"**Средний чек** за месяц: {rub(mtd.avg_check)} (команда {rub(tm.avg_check)})",
          f"**Вещей в чеке**: {d1(mtd.upt)} (команда {d1(tm.upt)})"]
    tips = advise(db, cfg, sid, st, day)
    if tips:
        L += ["", "💡 " + "\n💡 ".join(tips)]
    return "\n".join(L)


def month_start(db: DB, cfg: Settings, sid: int, month: str) -> str:
    s = db.seller(sid)
    p = month_params(db, cfg, month)
    r = db.one("SELECT plan_full FROM plans WHERE month=? AND seller_id=?", month, sid)
    if not r or not r["plan_full"]:
        return ""
    planned = db.planned_shifts(sid, month) or cfg.norm_shifts
    k = planned / cfg.norm_shifts
    admin = s["role"] == "admin"
    L = [f"{s['first_name'] or s['name']}, ваш план на {MONTHS[int(month[5:])]} ({planned} смен):", ""]
    for amount, rate in threshold_amounts(r["plan_full"], p):
        lvl = round(amount / r["plan_full"] * 100)
        add = f" + {rub(p.admin_bonus)} админ." if admin else ""
        L.append(f"{lvl}% — {rub(amount * k)} → ставка {rub(rate)}{add}")
    L += ["", f"Каждые +{p.step:.0%} сверх плана = +{rub(p.step_bonus)} к ставке. Удачного месяца!"]
    return "\n".join(L)


def month_result(row: dict) -> str:
    return (f"Итоги месяца: продажи {rub(row['sales'])}, выполнение плана {pct(row['performance'])}, "
            f"ставка {rub(row['rate'])} (за полный месяц; при неполном — пропорционально сменам).")


def owner_summary(db: DB, cfg: Settings, day: date) -> str:
    rows = []
    for s in db.sellers():
        st = status(db, cfg, s["id"], day)
        if not st.sales:
            continue
        m = M.seller(db, s["id"], day.replace(day=1), day)
        rows.append((s["name"], st, m))
    tot = M.team(db, day.replace(day=1), day)
    L = [f"**Сводка на {day:%d.%m.%Y}**", f"Магазины с начала месяца: {rub(tot.sales)} · средний чек "
         f"{rub(tot.avg_check)} · вещей в чеке {d1(tot.upt)}", ""]
    for name, st, m in sorted(rows, key=lambda x: -(x[1].performance or 0)):
        L.append(f"{name}: {rub(st.sales)} · выполнение {pct(st.performance)} · ставка {rub(st.projected_rate or st.current_rate)}"
                 f" · чек {rub(m.avg_check)} · {d1(m.upt)} вещ.")
    return "\n".join(x for x in L if x is not None)


def yesterday(today: date) -> date:
    return today - timedelta(days=1)

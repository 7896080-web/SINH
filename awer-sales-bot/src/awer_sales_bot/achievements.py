"""Личные рекорды и серии — мотивация без денег (docs/SPEC.md, «Мотивация и обучение»).

Продавец сравнивается только с собой: рекорд смены, лучшая смена месяца, рекорд по вещам, серия смен выше нормы.
Нормой смены считается личный план на полный месяц / норма смен. Те же достижения владелец видит в блоке
«Кого похвалить» — там только заметные (рекорды и круглые серии), чтобы не повторяться каждый день.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import Settings
from .db import DB

STREAK_MARKS = (3, 5, 7, 10, 15, 20)  # длины серии, о которых говорим владельцу


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(3, "смена", "смены", "смен") -> "смены"."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


@dataclass
class Achievement:
    kind: str  # record_sales | best_month | record_items | streak
    text: str  # для продавца
    notable: bool  # показывать владельцу в «Кого похвалить»


def shifts(db: DB, sid: int, end: date) -> list[tuple[str, float, float]]:
    """[(день, продажи, вещей)] по всем сменам продавца до `end` включительно, по возрастанию дня."""
    rows = db.q("SELECT day, SUM(amount) s, SUM(CASE WHEN qty>0 THEN qty ELSE 0 END) n FROM sales_lines "
                "WHERE seller_id=? AND day<=? GROUP BY day ORDER BY day", sid, end.isoformat())
    return [(r["day"], r["s"], r["n"]) for r in rows]


def streak(hist: list[tuple[str, float, float]], norm: float) -> int:
    """Сколько последних смен подряд продажи были не ниже нормы смены."""
    n = 0
    for _, s, _ in reversed(hist):
        if s < norm:
            break
        n += 1
    return n


def for_day(db: DB, cfg: Settings, sid: int, day: date, plan_full: float | None) -> list[Achievement]:
    """Достижения за смену `day`. Пусто, если продавец в этот день не работал."""
    if not cfg.motivation.records:
        return []
    hist = shifts(db, sid, day)
    if not hist or hist[-1][0] != day.isoformat():
        return []
    _, sales, items = hist[-1]
    prev = hist[:-1]
    out: list[Achievement] = []
    enough = len(prev) >= cfg.motivation.record_min_shifts
    best_prev = max((s for _, s, _ in prev), default=0.0)
    month_prev = [s for d, s, _ in prev if d[:7] == day.isoformat()[:7]]
    if enough and sales > best_prev > 0:
        out.append(Achievement("record_sales", f"Личный рекорд: {_rub(sales)} за смену (прежний — {_rub(best_prev)})",
                               True))
    elif len(month_prev) >= 2 and sales > max(month_prev):
        out.append(Achievement("best_month", f"Лучшая смена месяца: {_rub(sales)}", len(month_prev) >= 4))
    best_items = max((n for _, _, n in prev), default=0.0)
    if enough and items > best_items > 0:
        out.append(Achievement("record_items", f"Рекорд по вещам: {items:.0f} за смену (прежний — {best_items:.0f})",
                               True))
    if plan_full:
        norm = plan_full / cfg.norm_shifts
        n = streak(hist, norm)
        if n >= cfg.motivation.streak_min:
            word = plural(n, "смена", "смены", "смен")
            out.append(Achievement("streak", f"Серия: {n} {word} подряд не ниже нормы смены ({_rub(norm)})",
                                   n in STREAK_MARKS))
    return out

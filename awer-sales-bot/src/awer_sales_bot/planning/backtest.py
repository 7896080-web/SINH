"""Проверка на прошлых месяцах: какой план поставил бы бот на месяц по данным до его начала и сколько продавец
продал на самом деле — по нынешним правилам (норма, шаг, ставки — docs/SPEC.md).

Ничего не записывает: план не фиксируется, итоги не сохраняются — только расчёт для владельца (страница
«Проверка на прошлых месяцах», `awer-bot backtest`). План месяца M считается так же, как 1-го числа:
по истории до M (прошлые 3 месяца и тот же месяц год назад); факт — продажи M из 1С, а где 1С нет — из ведомостей.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field

from ..config import Settings
from ..db import DB
from . import service
from .engine import Params, StorePlan, evaluate, month_add, seller_plan, store_plan


@dataclass
class Row:
    seller_id: int
    name: str
    role: str
    method: str  # personal — по личной истории, newcomer — план новичка, none — плана нет
    plan: float | None  # план на полный месяц
    sales: float  # факт
    fte: float  # отработано: доля полного месяца (рабочие дни / норма или ставка из ведомости)
    days: int | None  # рабочих дней по 1С (None — месяц из ведомостей)
    performance: float | None  # (факт / отработано) / план
    steps: int
    rate: float  # ставка по схеме за полный месяц (с доплатой старшему)

    @property
    def plan_worked(self) -> float | None:
        """План на отработанное время — с ним сравнивается факт."""
        return self.plan * self.fte if self.plan else None


@dataclass
class Month:
    month: str
    params: Params
    store: StorePlan
    store_fact: float
    source: str  # «1С», «ведомости» или «нет данных»
    coverage: float  # доля дней месяца с выгрузкой 1С (1 — полный месяц)
    rows: list[Row] = field(default_factory=list)

    @property
    def done(self) -> int:
        return sum(1 for r in self.rows if r.performance is not None and r.performance >= 1)

    @property
    def rates(self) -> float:
        return sum(r.rate * min(r.fte, 1.0) for r in self.rows)  # при неполном месяце — пропорционально


def run(db: DB, cfg: Settings, month: str) -> Month:
    p = service.month_params(db, cfg, month)
    before = db.history(cfg.norm_shifts, until_month=month_add(month, -1))
    sp = store_plan(before, month, p, trend_override=service.trend_manual(db, month))
    h = db.history(cfg.norm_shifts, until_month=month)
    fact = h.months.get(month, {})
    daily = month in db.daily_months()
    days_in = calendar.monthrange(*map(int, month.split("-")))[1]
    n_days = db.covered_days(month)
    out = Month(month, p, sp, h.store_total(month), "1С" if daily else "ведомости" if fact else "нет данных",
                min(1.0, n_days / days_in) if daily else 1.0)
    for sid, x in fact.items():
        s = db.seller(int(sid))
        if not s or s["role"] in ("system", "test") or (x.sales <= 0 and x.fte <= 0):
            continue
        calc = seller_plan(before, month, sid, sp, p)
        res = evaluate(x.sales, x.fte, calc.plan_full, h.role(sid), p)
        days = db.worked_shifts(int(sid), month) if daily else None
        out.rows.append(Row(int(sid), s["name"], s["role"], calc.method, calc.plan_full, x.sales, x.fte, days,
                            res.performance, res.steps, res.rate))
    out.rows.sort(key=lambda r: (r.role != "admin", r.name))
    return out


def months_between(first: str, last: str) -> list[str]:
    out, m = [], first
    while m <= last and len(out) < 24:
        out.append(m)
        m = month_add(m, 1)
    return out

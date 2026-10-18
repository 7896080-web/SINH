"""Расчёт планов и ставок — чистые функции без БД.

Магазинная часть 1-в-1 повторяет согласованную Excel-модель «План_и_ставка_продавцов.xlsx»; личный план —
доля в магазине за 12 мес. (решение владельца 19.10.2026, docs/SPEC.md п. 6):

1. Тренд магазина = Σ общего выторга за 3 месяца перед расчётным
   / Σ за те же месяцы прошлого года (только пары месяцев, где есть оба значения).
2. План магазина = выторг расчётного месяца прошлого года × тренд.
3. Коэффициент месяца = план магазина / средний месяц магазина за 3 последних.
4. Личный план (на полный отработанный месяц) = доля × план магазина, где
   доля = Σ личных продаж за personal_window мес. / Σ (отработанное в месяце × выторг магазина за месяц).
   Нужно отработанного за это время не меньше min_fte. Если за 3 мес. меньше min_fte, а за год набралось —
   «после перерыва»: расчёт тот же, отличается только пометка.
5. Новичок (за personal_window мес. < min_fte) =
   средний рядовой продавец (без админов) в этом месяце прошлого года × тренд.
6. Выполнение = (продажи за месяц / отработано) / личный план.
   Ставка = база + надбавка × полные шаги перевыполнения (шаг 10 %), не выше максимума;
   админам + фиксированная доплата.

«Общий выторг на полный месяц» = сумма продаж всех продавцов / доля месяца в данных
(доля < 1, если за месяц есть только часть данных — например, только 16–31 число).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["seller", "admin"]


# ----------------------------------------------------------------------------- months
def month_add(month: str, delta: int) -> str:
    """'2026-10' + (-3) -> '2026-07'."""
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_range(start: str, end: str) -> list[str]:
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur = month_add(cur, 1)
    return out


# ----------------------------------------------------------------------------- data
@dataclass
class SellerMonth:
    sales: float  # продажи за месяц, ₽ (обе точки)
    fte: float  # отработано, доля полного месяца (1 = полный)


@dataclass
class History:
    """Помесячная история: month -> seller -> SellerMonth."""

    months: dict[str, dict[str, SellerMonth]] = field(default_factory=dict)
    roles: dict[str, Role] = field(default_factory=dict)
    coverage: dict[str, float] = field(default_factory=dict)  # доля месяца в данных, по умолчанию 1

    def add(self, month: str, seller: str, sales: float, fte: float) -> None:
        self.months.setdefault(month, {})[seller] = SellerMonth(sales, fte)

    def get(self, month: str, seller: str) -> SellerMonth | None:
        return self.months.get(month, {}).get(seller)

    def role(self, seller: str) -> Role:
        return self.roles.get(seller, "seller")

    def store_total(self, month: str) -> float:
        return sum(x.sales for x in self.months.get(month, {}).values())

    def store_full(self, month: str) -> float:
        """Общий выторг на полный месяц (0, если данных нет)."""
        tot = self.store_total(month)
        if tot == 0:
            return 0.0
        return tot / self.coverage.get(month, 1.0)

    def regular_avg(self, month: str) -> float:
        """Средний рядовой продавец (без админов) на полный месяц."""
        s = f = 0.0
        for name, x in self.months.get(month, {}).items():
            if self.role(name) == "seller":
                s += x.sales
                f += x.fte
        return s / f if f else 0.0


@dataclass
class Params:
    base_rate: float = 25_000  # базовая ставка, ₽/мес
    admin_bonus: float = 13_000  # доплата администратору, ₽/мес
    step: float = 0.10  # шаг перевыполнения
    step_bonus: float = 5_000  # надбавка за шаг, ₽
    max_rate: float = 45_000  # максимальная ставка без доплаты админа
    min_fte: float = 1.0  # минимум отработанного (в полных месяцах) для личного плана
    window: int = 3  # окно тренда и среднего месяца магазина, мес.
    personal_window: int = 12  # окно доли продавца в магазине, мес.
    sales_percent: float = 0.04  # процент от продаж (для ведомости)
    stabilizer: Literal["none", "one_step"] = "none"  # см. docs/SPEC.md


# ----------------------------------------------------------------------------- store level
@dataclass
class StorePlan:
    month: str
    last_year: float  # выторг этого месяца прошлого года (на полный месяц)
    seasonality: float | None  # прошлый год: месяц / предыдущий месяц
    trend: float | None
    plan: float | None
    recent_avg: float | None  # средний месяц магазина за окно
    coef: float | None  # коэффициент месяца
    newcomer_plan: float | None
    trend_calc: float | None = None  # расчётный тренд (до ручной правки)


def store_plan(h: History, month: str, p: Params = Params(), trend_override: float | None = None) -> StorePlan:
    """trend_override — ручной коэффициент год к году (если задан на странице настроек)."""
    ly = h.store_full(month_add(month, -12))
    ly_prev = h.store_full(month_add(month, -13))
    seasonality = ly / ly_prev if ly and ly_prev else None

    num = den = 0.0
    for k in range(1, p.window + 1):
        ty_m = month_add(month, -k)
        ty, lyv = h.store_full(ty_m), h.store_full(month_add(ty_m, -12))
        if ty > 0 and lyv > 0:
            num += ty
            den += lyv
    trend_calc = num / den if den else None
    trend = trend_override if trend_override is not None else trend_calc

    plan = ly * trend if (ly and trend is not None) else None

    recent = [h.store_full(month_add(month, -k)) for k in range(1, p.window + 1)]
    recent = [x for x in recent if x > 0]
    recent_avg = sum(recent) / len(recent) if recent else None

    coef = plan / recent_avg if (plan is not None and recent_avg) else None
    reg_ly = h.regular_avg(month_add(month, -12))
    newcomer = reg_ly * trend if (reg_ly and trend is not None) else None
    return StorePlan(month, ly, seasonality, trend, plan, recent_avg, coef, newcomer, trend_calc)


# ----------------------------------------------------------------------------- seller level
METHOD_NAMES = {"personal": "по личной истории", "returning": "после перерыва — по своим месяцам за год",
                "newcomer": "новичок", "manual": "вручную", "none": "плана нет"}


@dataclass
class SellerPlan:
    month: str
    seller: str
    plan_full: float | None  # личный план на полный месяц, ₽
    method: Literal["personal", "returning", "newcomer", "manual", "none"]
    level: float | None  # его обычный месяц: доля за год × средний месяц магазина за 3 мес. (× коэф. = план)


def seller_plan(h: History, month: str, seller: str, sp: StorePlan | None = None, p: Params = Params(),
                manual: float | None = None) -> SellerPlan:
    """manual — ручной личный план на полный месяц (перекрывает расчёт)."""
    sp = sp or store_plan(h, month, p)
    if manual is not None:
        return SellerPlan(month, seller, manual, "manual", None)
    if sp.plan is None:
        return SellerPlan(month, seller, None, "none", None)
    share, worked = personal_share(h, month, seller, p.personal_window)
    if share is None or worked < p.min_fte:
        return SellerPlan(month, seller, sp.newcomer_plan, "newcomer", None)
    recent = sum(x.fte for k in range(1, p.window + 1) if (x := h.get(month_add(month, -k), seller)))
    plan = share * sp.plan
    return SellerPlan(month, seller, plan, "personal" if recent >= p.min_fte else "returning",
                      plan / sp.coef if sp.coef else None)  # уровень: его обычный месяц; × коэф. месяца = план


def personal_share(h: History, month: str, seller: str, months: int = 12) -> tuple[float | None, float]:
    """(доля продавца в выторге магазина, отработано) за `months` месяцев перед `month`:
    Σ продаж / Σ (отработанное × выторг магазина на полный месяц) — с поправкой на сезон каждого месяца."""
    s = f = base = 0.0
    for k in range(1, months + 1):
        m = month_add(month, -k)
        x, store = h.get(m, seller), h.store_full(m)
        if x and store > 0:
            s, f, base = s + x.sales, f + x.fte, base + x.fte * store
    return (s / base if base > 0 else None), f


# ----------------------------------------------------------------------------- evaluation
@dataclass
class Result:
    performance: float | None  # факт на полный месяц / план
    steps: int
    rate: float  # ставка за полный месяц, ₽ (с доплатой админа)


def steps_for(performance: float | None, p: Params = Params()) -> int:
    if performance is None:
        return 0
    over = round(performance - 1, 6)
    return max(0, math.floor(over / p.step + 1e-9))


def rate_for(steps: int, role: Role, p: Params = Params()) -> float:
    r = min(p.max_rate, p.base_rate + p.step_bonus * steps)
    return r + (p.admin_bonus if role == "admin" else 0)


def evaluate(sales: float, fte: float, plan_full: float | None, role: Role,
             p: Params = Params(), prev_steps: int | None = None) -> Result:
    if not plan_full or fte <= 0:
        return Result(None, 0, rate_for(0, role, p))
    perf = (sales / fte) / plan_full
    st = steps_for(perf, p)
    if p.stabilizer == "one_step" and prev_steps is not None:
        st = max(prev_steps - 1, min(prev_steps + 1, st))
    return Result(perf, st, rate_for(st, role, p))


def threshold_amounts(plan_full: float, p: Params = Params(), n_steps: int | None = None) -> list[tuple[float, float]]:
    """[(сумма на полный месяц, ставка без доплаты)...] для 100 %, 110 %, ... до максимума."""
    if n_steps is None:
        n_steps = int(round((p.max_rate - p.base_rate) / p.step_bonus))
    return [(plan_full * (1 + p.step * k), min(p.max_rate, p.base_rate + p.step_bonus * k)) for k in range(n_steps + 1)]

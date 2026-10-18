"""Показатели качества продаж.

Источник — «Отчеты о розничных продажах»: строки знают продавца, но не чек. Поэтому:
  * средний чек и вещей в чеке — по МАГАЗИНУ (выручка и штуки / «Количество чеков» из шапок ОРП);
  * по ПРОДАВЦУ — продажи за смену, вещей за смену, средняя цена вещи, доля скидок, возвраты.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .db import DB


@dataclass
class SellerMetrics:
    sales: float  # продажи за вычетом возвратов
    items: float  # продано штук (без возвратов)
    gross: float  # продажи без возвратов
    returns: float  # сумма возвратов (положительное число)
    discount: float  # сумма скидок
    shifts: int

    @property
    def per_shift(self) -> float | None:
        return self.sales / self.shifts if self.shifts else None

    @property
    def items_per_shift(self) -> float | None:
        return self.items / self.shifts if self.shifts else None

    @property
    def avg_price(self) -> float | None:
        """Средняя цена проданной вещи (со скидкой)."""
        return self.gross / self.items if self.items else None

    @property
    def discount_share(self) -> float | None:
        base = self.gross + self.discount
        return self.discount / base if base else None

    @property
    def returns_share(self) -> float | None:
        return self.returns / self.gross if self.gross else None


@dataclass
class StoreMetrics:
    revenue: float
    items: float
    checks: int

    @property
    def avg_check(self) -> float | None:
        return self.revenue / self.checks if self.checks else None

    @property
    def upt(self) -> float | None:
        """Вещей в чеке (комплектность) по магазину."""
        return self.items / self.checks if self.checks else None


def _seller_sql(where: str, args: list, db: DB) -> SellerMetrics:
    r = db.one(f"""SELECT COALESCE(SUM(amount),0) sales,
                          COALESCE(SUM(CASE WHEN qty>0 THEN qty ELSE 0 END),0) items,
                          COALESCE(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),0) gross,
                          COALESCE(-SUM(CASE WHEN amount<0 THEN amount ELSE 0 END),0) returns,
                          COALESCE(SUM(CASE WHEN amount>0 THEN discount ELSE 0 END),0) discount,
                          COUNT(DISTINCT seller_id||'|'||day) shifts
                   FROM sales_lines WHERE {where}""", *args)
    return SellerMetrics(r["sales"], r["items"], r["gross"], r["returns"], r["discount"], r["shifts"])


def seller(db: DB, sid: int, start: date, end: date) -> SellerMetrics:
    return _seller_sql("seller_id=? AND day BETWEEN ? AND ?", [sid, start.isoformat(), end.isoformat()], db)


def team(db: DB, start: date, end: date, exclude_admins: bool = True) -> SellerMetrics:
    """Средние по команде считаются на смену: продажи команды / смены команды."""
    where = "day BETWEEN ? AND ?"
    if exclude_admins:
        where += " AND seller_id IN (SELECT id FROM sellers WHERE role='seller')"
    return _seller_sql(where, [start.isoformat(), end.isoformat()], db)


def store(db: DB, start: date, end: date) -> StoreMetrics:
    """Средний чек и вещей в чеке по магазинам — только дни, где есть и шапки, и строки ОРП."""
    a = [start.isoformat(), end.isoformat()]
    r = db.one("""SELECT COALESCE(SUM(checks),0) checks, COALESCE(SUM(revenue),0) revenue FROM store_days
                  WHERE day BETWEEN ? AND ?""", *a)
    items = db.one("""SELECT COALESCE(SUM(CASE WHEN qty>0 THEN qty ELSE 0 END),0) n FROM sales_lines
                      WHERE day BETWEEN ? AND ? AND day||'|'||store IN (SELECT day||'|'||store FROM store_days)""", *a)["n"]
    return StoreMetrics(r["revenue"], items, r["checks"])


def cross_sell(db: DB, start: date, end: date, top: int = 3) -> dict[str, list[tuple[str, float]]]:
    """Что продают вместе — по продавцу за смену (чека в ОРП нет, поэтому «корзина» = продавец + день).

    Это приближение: показывает категории, которые продавцы чаще продают в один день, а не в одном чеке."""
    rows = db.q("""SELECT seller_id||'|'||day k, category FROM sales_lines
                   WHERE qty>0 AND amount>0 AND category IS NOT NULL AND category<>'' AND day BETWEEN ? AND ?""",
                start.isoformat(), end.isoformat())
    baskets: dict[str, set[str]] = {}
    for r in rows:
        baskets.setdefault(r["k"], set()).add(r["category"])
    cnt: dict[str, int] = {}
    pair: dict[tuple[str, str], int] = {}
    for cats in baskets.values():
        for a in cats:
            cnt[a] = cnt.get(a, 0) + 1
            for b in cats:
                if a != b:
                    pair[(a, b)] = pair.get((a, b), 0) + 1
    out: dict[str, list[tuple[str, float]]] = {}
    for (a, b), n in pair.items():
        out.setdefault(a, []).append((b, n / cnt[a]))
    return {a: sorted(v, key=lambda x: -x[1])[:top] for a, v in out.items() if cnt.get(a, 0) >= 10}


def category_mix(db: DB, sid: int | None, start: date, end: date) -> dict[str, float]:
    """Доля категорий в штуках: {категория: доля}."""
    where, args = "qty>0 AND amount>0 AND day BETWEEN ? AND ? AND category IS NOT NULL AND category<>''", \
        [start.isoformat(), end.isoformat()]
    if sid is not None:
        where += " AND seller_id=?"
        args.append(sid)
    rows = db.q(f"SELECT category, SUM(qty) n FROM sales_lines WHERE {where} GROUP BY category", *args)
    tot = sum(r["n"] for r in rows) or 1
    return {r["category"]: r["n"] / tot for r in rows}

"""Показатели качества продаж.

Источник строк — чеки ККМ (есть номер чека и время) или «Отчеты о розничных продажах» (номера чека нет).
  * Есть чеки → по продавцу настоящие: чеков, средний чек, вещей в чеке, доля чеков «одна вещь», часы продаж.
  * Только ОРП → средний чек и вещей в чеке по МАГАЗИНУ (шапки ОРП); по продавцу — «покупки по картам»:
    строки одной дисконтной карты в одном документе = одна покупка (честный средний чек хотя бы по части покупок).
  * Всегда: продажи / вещей за смену, средняя цена вещи, скидки (в т.ч. ручные), возвраты.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .db import DB


@dataclass
class SellerMetrics:
    sales: float  # продажи за вычетом возвратов
    items: float  # продано штук (без возвратов)
    gross: float  # продажи без возвратов
    returns: float  # сумма возвратов (положительное число)
    discount: float  # скидки, ₽
    manual_discount: float  # из них ручные, ₽
    shifts: int
    receipts: int  # чеков продажи (0, если данные из ОРП)
    receipt_items: float  # штук в этих чеках
    single_item_receipts: int
    card_purchases: int  # покупок с дисконтной картой (карта + документ/чек)
    card_sales: float
    card_items: float

    @property
    def has_receipts(self) -> bool:
        return self.receipts > 0

    @property
    def per_shift(self) -> float | None:
        return self.sales / self.shifts if self.shifts else None

    @property
    def items_per_shift(self) -> float | None:
        return self.items / self.shifts if self.shifts else None

    @property
    def avg_price(self) -> float | None:
        return self.gross / self.items if self.items else None

    @property
    def avg_check(self) -> float | None:
        return self.gross / self.receipts if self.receipts else None

    @property
    def upt(self) -> float | None:
        return self.receipt_items / self.receipts if self.receipts else None

    @property
    def single_share(self) -> float | None:
        return self.single_item_receipts / self.receipts if self.receipts else None

    @property
    def card_avg(self) -> float | None:
        return self.card_sales / self.card_purchases if self.card_purchases else None

    @property
    def card_upt(self) -> float | None:
        return self.card_items / self.card_purchases if self.card_purchases else None

    @property
    def card_share(self) -> float | None:
        return self.card_sales / self.gross if self.gross else None

    @property
    def discount_share(self) -> float | None:
        base = self.gross + self.discount
        return self.discount / base if base else None

    @property
    def manual_share(self) -> float | None:
        base = self.gross + self.discount
        return self.manual_discount / base if base else None

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
        return self.items / self.checks if self.checks else None


def _seller_sql(where: str, args: list, db: DB) -> SellerMetrics:
    r = db.one(f"""
        SELECT COALESCE(SUM(amount),0) sales,
               COALESCE(SUM(CASE WHEN qty>0 THEN qty ELSE 0 END),0) items,
               COALESCE(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),0) gross,
               COALESCE(-SUM(CASE WHEN amount<0 THEN amount ELSE 0 END),0) returns,
               COALESCE(SUM(CASE WHEN amount>0 THEN discount ELSE 0 END),0) discount,
               COALESCE(SUM(CASE WHEN amount>0 THEN manual_discount ELSE 0 END),0) manual_discount,
               COUNT(DISTINCT seller_id||'|'||day) shifts
        FROM sales_lines WHERE {where}""", *args)
    rc = db.one(f"""
        WITH rc AS (SELECT store, day, receipt, SUM(amount) amt, SUM(CASE WHEN qty>0 THEN qty ELSE 0 END) q
                    FROM sales_lines WHERE {where} AND receipt IS NOT NULL GROUP BY store, day, receipt)
        SELECT COALESCE(SUM(CASE WHEN amt>0 THEN 1 ELSE 0 END),0) n,
               COALESCE(SUM(CASE WHEN amt>0 THEN q ELSE 0 END),0) q,
               COALESCE(SUM(CASE WHEN amt>0 AND q<=1 THEN 1 ELSE 0 END),0) single FROM rc""", *args)
    cd = db.one(f"""
        WITH cp AS (SELECT store, day, COALESCE(receipt, doc) d, card, SUM(amount) amt,
                           SUM(CASE WHEN qty>0 THEN qty ELSE 0 END) q
                    FROM sales_lines WHERE {where} AND card IS NOT NULL AND card<>'' GROUP BY store, day, d, card)
        SELECT COALESCE(SUM(CASE WHEN amt>0 THEN 1 ELSE 0 END),0) n, COALESCE(SUM(CASE WHEN amt>0 THEN amt ELSE 0 END),0) s,
               COALESCE(SUM(CASE WHEN amt>0 THEN q ELSE 0 END),0) q FROM cp""", *args)
    return SellerMetrics(r["sales"], r["items"], r["gross"], r["returns"], r["discount"], r["manual_discount"],
                         r["shifts"], rc["n"], rc["q"], rc["single"], cd["n"], cd["s"], cd["q"])


def seller(db: DB, sid: int, start: date, end: date) -> SellerMetrics:
    return _seller_sql("seller_id=? AND day BETWEEN ? AND ?", [sid, start.isoformat(), end.isoformat()], db)


def team(db: DB, start: date, end: date, exclude_admins: bool = True) -> SellerMetrics:
    """Команда: продавцы (без админов, если exclude_admins), без строк «(без продавца)»."""
    role = "('seller')" if exclude_admins else "('seller','admin')"
    where = f"day BETWEEN ? AND ? AND seller_id IN (SELECT id FROM sellers WHERE role IN {role})"
    return _seller_sql(where, [start.isoformat(), end.isoformat()], db)


def unattributed(db: DB, start: date, end: date, name: str = "(без продавца)") -> float:
    """Продажи с пустым продавцом в 1С (служебные «Одежда сотрудникам» сюда не входят)."""
    r = db.one("""SELECT COALESCE(SUM(amount),0) s FROM sales_lines WHERE day BETWEEN ? AND ?
                  AND seller_id IN (SELECT id FROM sellers WHERE role='system' AND name=?)""",
               start.isoformat(), end.isoformat(), name)
    return r["s"]


def store(db: DB, start: date, end: date) -> StoreMetrics:
    """Средний чек и вещей в чеке по магазинам: из чеков, если есть, иначе из шапок ОРП."""
    a = [start.isoformat(), end.isoformat()]
    m = _seller_sql("day BETWEEN ? AND ?", a, db)
    if m.receipts:
        return StoreMetrics(m.gross, m.receipt_items, m.receipts)
    r = db.one("SELECT COALESCE(SUM(checks),0) checks, COALESCE(SUM(revenue),0) revenue FROM store_days "
               "WHERE day BETWEEN ? AND ?", *a)
    items = db.one("""SELECT COALESCE(SUM(CASE WHEN qty>0 THEN qty ELSE 0 END),0) n FROM sales_lines
                      WHERE day BETWEEN ? AND ? AND day||'|'||store IN (SELECT day||'|'||store FROM store_days)""", *a)["n"]
    return StoreMetrics(r["revenue"], items, r["checks"])


def hours(db: DB, start: date, end: date, sid: int | None = None) -> dict[int, float]:
    """Продажи по часам (только при выгрузке из чеков): {час: сумма}."""
    where, args = "time IS NOT NULL AND amount>0 AND day BETWEEN ? AND ?", [start.isoformat(), end.isoformat()]
    if sid is not None:
        where += " AND seller_id=?"
        args.append(sid)
    rows = db.q(f"SELECT CAST(substr(time,1,2) AS INTEGER) h, SUM(amount) s FROM sales_lines WHERE {where} GROUP BY h",
                *args)
    return {r["h"]: r["s"] for r in rows}


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


def cross_sell(db: DB, start: date, end: date, top: int = 3) -> dict[str, list[tuple[str, float]]]:
    """Что покупают вместе: «корзина» = чек (если есть), иначе дисконтная карта в документе."""
    rows = db.q("""SELECT store||'|'||day||'|'||COALESCE(receipt, doc||'|'||card) k, category FROM sales_lines
                   WHERE qty>0 AND amount>0 AND category IS NOT NULL AND category<>'' AND day BETWEEN ? AND ?
                     AND (receipt IS NOT NULL OR (card IS NOT NULL AND card<>''))""",
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


# ----------------------------------------------------------------------------- остатки и поступления
def stock_day(db: DB) -> str | None:
    r = db.one("SELECT MAX(day) d FROM stock")
    return r["d"] if r else None


def stock_by_category(db: DB, store: str | None = None) -> dict[str, tuple[int, float]]:
    """Последний снимок: {категория: (моделей, штук)}."""
    d = stock_day(db)
    if not d:
        return {}
    where, args = "day=?", [d]
    if store:
        where += " AND store=?"
        args.append(store)
    rows = db.q(f"SELECT category, COUNT(DISTINCT item) m, SUM(qty) q FROM stock WHERE {where} GROUP BY category", *args)
    return {r["category"]: (r["m"], r["q"]) for r in rows if r["category"]}


def arrivals_summary(db: DB, day: date, store: str | None = None, top: int = 3) -> list[tuple[str, int, float]]:
    """Поступления за день: [(категория, моделей, штук)] по убыванию штук."""
    where, args = "day=?", [day.isoformat()]
    if store:
        where += " AND store=?"
        args.append(store)
    rows = db.q(f"""SELECT COALESCE(category,'Прочее') c, COUNT(DISTINCT item) m, SUM(qty) q FROM arrivals WHERE {where}
                    GROUP BY c ORDER BY q DESC LIMIT ?""", *args, top)
    return [(r["c"], r["m"], r["q"]) for r in rows]


ONE_SIZE = ("", "U", "UNI", "ONE SIZE", "ONESIZE", "Б/Р", "БЕЗ РАЗМЕРА")


def broken_sizes(db: DB, day: date, min_sold: float = 3, max_sizes: int = 2, limit: int = 10) -> list[tuple]:
    """Ходовые модели (продано ≥ min_sold шт. за 30 дней) с остатком в ≤ max_sizes размерах.
    Безразмерные позиции (U, б/р — ремни, галстуки) не учитываются."""
    d = stock_day(db)
    if not d:
        return []
    marks = ",".join("?" * len(ONE_SIZE))
    rows = db.q(f"""
        WITH sold AS (SELECT item, SUM(qty) n FROM sales_lines WHERE qty>0 AND day BETWEEN ? AND ? GROUP BY item),
             st AS (SELECT store, item, COUNT(DISTINCT size) sizes, SUM(qty) q FROM stock
                    WHERE day=? AND UPPER(TRIM(size)) NOT IN ({marks}) GROUP BY store, item)
        SELECT st.store, st.item, st.sizes, st.q, sold.n FROM st JOIN sold USING(item)
        WHERE sold.n>=? AND st.sizes<=? ORDER BY sold.n DESC LIMIT ?""",
                (day - timedelta(days=30)).isoformat(), day.isoformat(), d, *ONE_SIZE, min_sold, max_sizes, limit)
    return [(r["store"], r["item"], r["sizes"], r["q"], r["n"]) for r in rows]


def stale_stock(db: DB, day: date, days: int = 90) -> tuple[int, float]:
    """Залежавшийся товар: позиции на остатке без продаж за `days` дней → (моделей, штук).

    Пока продаж в базе меньше `days` дней, «без продаж» не отличить от «бот ещё не видел» — (0, 0) (аудит С3);
    пришедшее за эти дни не залежалось."""
    d = stock_day(db)
    since = (day - timedelta(days=days)).isoformat()
    first = db.one("SELECT MIN(day) d FROM sales_lines")["d"]
    if not d or not first or first > since:
        return 0, 0.0
    r = db.one("""SELECT COUNT(DISTINCT item) m, COALESCE(SUM(qty),0) q FROM stock WHERE day=? AND item NOT IN
                  (SELECT DISTINCT item FROM sales_lines WHERE qty>0 AND day BETWEEN ? AND ?)
                  AND item NOT IN (SELECT DISTINCT item FROM arrivals WHERE day BETWEEN ? AND ?)""",
               d, since, day.isoformat(), since, day.isoformat())
    return r["m"], r["q"]

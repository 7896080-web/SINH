"""Показатели качества продаж: средний чек, комплектность (вещей в чеке), возвраты."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .db import DB


@dataclass
class Metrics:
    sales: float  # продажи за вычетом возвратов
    receipts: int  # чеков продажи
    items: float  # штук в чеках продажи
    returns: float  # сумма возвратов (положительное число)

    @property
    def avg_check(self) -> float | None:
        return self.sales / self.receipts if self.receipts else None

    @property
    def upt(self) -> float | None:
        """Комплектность — вещей в чеке."""
        return self.items / self.receipts if self.receipts else None


def _calc(db: DB, where: str, args: list) -> Metrics:
    r = db.one(f"""
        WITH rc AS (
            SELECT store, day, receipt, SUM(amount) amt, SUM(CASE WHEN qty>0 THEN qty ELSE 0 END) q
            FROM sales_lines WHERE {where} GROUP BY store, day, receipt)
        SELECT COALESCE(SUM(amt),0) sales,
               COALESCE(SUM(CASE WHEN amt>0 THEN 1 ELSE 0 END),0) receipts,
               COALESCE(SUM(CASE WHEN amt>0 THEN q ELSE 0 END),0) items,
               COALESCE(-SUM(CASE WHEN amt<0 THEN amt ELSE 0 END),0) returns
        FROM rc""", *args)
    return Metrics(r["sales"], r["receipts"], r["items"], r["returns"])


def seller(db: DB, sid: int, start: date, end: date) -> Metrics:
    return _calc(db, "seller_id=? AND day BETWEEN ? AND ?", [sid, start.isoformat(), end.isoformat()])


def team(db: DB, start: date, end: date, exclude_admins: bool = False) -> Metrics:
    where = "day BETWEEN ? AND ?"
    if exclude_admins:
        where += " AND seller_id IN (SELECT id FROM sellers WHERE role='seller')"
    return _calc(db, where, [start.isoformat(), end.isoformat()])


def single_item_share(db: DB, sid: int, start: date, end: date) -> float | None:
    r = db.one("""
        WITH rc AS (SELECT store, day, receipt, SUM(amount) amt, SUM(CASE WHEN qty>0 THEN qty ELSE 0 END) q
                    FROM sales_lines WHERE seller_id=? AND day BETWEEN ? AND ? GROUP BY store, day, receipt)
        SELECT SUM(CASE WHEN q<=1 AND amt>0 THEN 1 ELSE 0 END)*1.0 / NULLIF(SUM(CASE WHEN amt>0 THEN 1 ELSE 0 END),0) s
        FROM rc""", sid, start.isoformat(), end.isoformat())
    return r["s"]


def avg_item_price(db: DB, start: date, end: date, sid: int | None = None) -> float | None:
    where, args = "qty>0 AND amount>0 AND day BETWEEN ? AND ?", [start.isoformat(), end.isoformat()]
    if sid is not None:
        where += " AND seller_id=?"
        args.append(sid)
    r = db.one(f"SELECT SUM(amount)/NULLIF(SUM(qty),0) p FROM sales_lines WHERE {where}", *args)
    return r["p"]


def cross_sell(db: DB, start: date, end: date, top: int = 3) -> dict[str, list[tuple[str, float]]]:
    """Что докупают вместе: категория -> [(другая категория, доля чеков с ней)], по всем продавцам."""
    rows = db.q("""SELECT store||'|'||day||'|'||receipt k, category FROM sales_lines
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


def top_single_category(db: DB, sid: int, start: date, end: date) -> str | None:
    """Категория, которую продавец чаще всего продаёт «одной вещью в чеке»."""
    r = db.one("""
        WITH rc AS (SELECT store, day, receipt, SUM(CASE WHEN qty>0 THEN qty ELSE 0 END) q, MAX(category) cat
                    FROM sales_lines WHERE seller_id=? AND day BETWEEN ? AND ? AND amount>0
                    GROUP BY store, day, receipt)
        SELECT cat, COUNT(*) n FROM rc WHERE q<=1 AND cat IS NOT NULL AND cat<>'' GROUP BY cat ORDER BY n DESC LIMIT 1""",
               sid, start.isoformat(), end.isoformat())
    return r["cat"] if r else None

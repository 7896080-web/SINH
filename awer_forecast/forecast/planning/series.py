"""Дневные ряды спроса по размеру и каналу с минимальным цензурированием (раздел 5.1).

Дни без остатка — не нулевой спрос: если в день среза остаток был 0 и продаж не было,
день исключается из обучения (`is_stockout_corrected`). Для Lamoda остаток — сумма по
складам FBO; для каналов, которые тянут с ЦС напрямую (розница, позже WB/Ozon/сайт), —
остаток ЦС: дефицит там общий для всех таких каналов сразу.

Полное цензурирование — правая граница по лагу возвратов и хвост восстановления после
stockout — Фаза 2 (раздел 12); поля demand_timeseries под них уже есть.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import and_, delete, func, select
from sqlalchemy.engine import Connection

from forecast.catalog.resolver import SkuResolver
from forecast.db.schema import (
    t_channel_sales_daily, t_demand_timeseries, t_sales_daily, t_stock_1c, t_stock_fbo,
)


@dataclass
class DemandSeries:
    product_size_id: int
    channel: str
    start: date
    values: list[float] = field(default_factory=list)
    excluded: list[bool] = field(default_factory=list)

    @property
    def training(self) -> list[float]:
        return [v for v, skip in zip(self.values, self.excluded) if not skip]

    @property
    def stockout_days(self) -> int:
        return sum(self.excluded)

    def dates(self):
        return (self.start + timedelta(days=i) for i in range(len(self.values)))


def _build(sales: dict[int, dict[date, float]], stock: dict[int, dict[date, float]],
           first_seen: dict[int, date], window_start: date, window_end: date, channel: str) -> dict:
    out = {}
    for psid, seen in first_seen.items():
        start = max(seen, window_start)
        if start > window_end:
            continue
        days = (window_end - start).days + 1
        s_sales = sales.get(psid, {})
        s_stock = stock.get(psid, {})
        series = DemandSeries(psid, channel, start)
        for i in range(days):
            day = start + timedelta(days=i)
            value = float(s_sales.get(day, 0.0))
            snapshot = s_stock.get(day)
            series.values.append(value)
            series.excluded.append(snapshot is not None and snapshot <= 0 and value <= 0)
        out[psid] = series
    return out


def _stock_by_day(conn: Connection, table, window_start: date, window_end: date) -> dict[int, dict[date, float]]:
    date_col = table.c.snapshot_date
    rows = conn.execute(
        select(table.c.product_size_id, date_col, func.sum(table.c.qty))
        .where(and_(date_col >= window_start, date_col <= window_end))
        .group_by(table.c.product_size_id, date_col))
    out: dict[int, dict[date, float]] = defaultdict(dict)
    for psid, day, qty in rows:
        out[psid][day] = float(qty or 0)
    return out


def lamoda_series(conn: Connection, as_of: date, history_days: int) -> dict[int, DemandSeries]:
    """Ряд net-спроса Lamoda: выкуплено − возвращено (sales_daily), до вчерашнего дня
    включительно (сегодняшний ещё не закончился). Ряд начинается с первого дня, когда у
    размера была продажа или ненулевой остаток FBO, — дни «до появления на Lamoda» не нули."""
    window_end = as_of - timedelta(days=1)
    window_start = as_of - timedelta(days=history_days)
    sales: dict[int, dict[date, float]] = defaultdict(dict)
    first_seen: dict[int, date] = {}
    for psid, day, delivered, returned in conn.execute(
        select(t_sales_daily.c.product_size_id, t_sales_daily.c.sale_date, t_sales_daily.c.qty_delivered,
               t_sales_daily.c.qty_returned)
        .where(and_(t_sales_daily.c.sale_date >= window_start, t_sales_daily.c.sale_date <= window_end))):
        sales[psid][day] = float((delivered or 0) - (returned or 0))
        if delivered and (psid not in first_seen or day < first_seen[psid]):
            first_seen[psid] = day
    stock = _stock_by_day(conn, t_stock_fbo, window_start, window_end)
    for psid, by_day in stock.items():
        positive = [d for d, q in by_day.items() if q > 0]
        if positive:
            first = min(positive)
            if psid not in first_seen or first < first_seen[psid]:
                first_seen[psid] = first
    return _build(sales, stock, first_seen, window_start, window_end, "lamoda")


def channel_series(conn: Connection, resolver: SkuResolver, as_of: date,
                   history_days: int) -> tuple[dict[str, dict[int, DemandSeries]], dict]:
    """Ряды каналов из channel_sales_daily (баркод → размер через весь пул). Цензурирование —
    по остатку ЦС. Возвращает {канал: {размер: ряд}} и сводку несопоставленных баркодов."""
    window_end = as_of - timedelta(days=1)
    window_start = as_of - timedelta(days=history_days)
    per_channel: dict[str, dict[int, dict[date, float]]] = defaultdict(lambda: defaultdict(dict))
    first_seen: dict[str, dict[int, date]] = defaultdict(dict)
    unmatched: dict[str, int] = defaultdict(int)
    for channel, barcode, day, qty in conn.execute(
        select(t_channel_sales_daily.c.channel, t_channel_sales_daily.c.barcode, t_channel_sales_daily.c.sale_date,
               t_channel_sales_daily.c.qty)
        .where(and_(t_channel_sales_daily.c.sale_date >= window_start,
                    t_channel_sales_daily.c.sale_date <= window_end))):
        psids = resolver.resolve_barcodes([barcode])
        if len(psids) != 1:
            unmatched[channel] += 1
            continue
        psid = next(iter(psids))
        bucket = per_channel[channel][psid]
        bucket[day] = bucket.get(day, 0.0) + float(qty)
        if qty > 0 and (psid not in first_seen[channel] or day < first_seen[channel][psid]):
            first_seen[channel][psid] = day
    stock = _stock_by_day(conn, t_stock_1c, window_start, window_end)
    out = {channel: _build(sales, stock, first_seen[channel], window_start, window_end, channel)
           for channel, sales in per_channel.items()}
    return out, dict(unmatched)


def store_lamoda_timeseries(conn: Connection, series: dict[int, DemandSeries], as_of: date, history_days: int) -> int:
    """demand_timeseries — ряд Lamoda в окне истории, с флагами цензурирования."""
    window_start = as_of - timedelta(days=history_days)
    conn.execute(delete(t_demand_timeseries).where(t_demand_timeseries.c.demand_date >= window_start))
    rows = []
    for s in series.values():
        for day, value, skip in zip(s.dates(), s.values, s.excluded):
            rows.append({"product_size_id": s.product_size_id, "demand_date": day, "net_demand": value,
                         "is_stockout_corrected": skip, "is_return_censored": False,
                         "is_recovery_tail_corrected": False})
    if rows:
        conn.execute(t_demand_timeseries.insert(), rows)
    return len(rows)

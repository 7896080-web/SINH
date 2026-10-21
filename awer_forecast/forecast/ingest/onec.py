"""Приём выгрузок 1С: остаток ЦС (stock_1c) и продажи не-Lamoda каналов (channel_sales_daily)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path

from sqlalchemy import and_, delete, select
from sqlalchemy.engine import Connection

from forecast.catalog.resolver import SkuResolver
from forecast.db.ops import upsert
from forecast.db.schema import t_channel_sales_daily, t_product_sizes, t_stock_1c
from forecast.integrations.onec.files import (
    parse_sales, parse_stock_rows, read_text, sales_file_period, stock_file_time,
)
from forecast.timeutil import MSK, to_naive_utc

MAX_LISTED = 200


def ingest_stock_file(conn: Connection, path: Path, resolver: SkuResolver) -> dict:
    """Снимок остатков ЦС из `stock_*.txt` на день выгрузки.

    Строка 1С сопоставляется с размером по ЛЮБОМУ своему баркоду из пула. Разные
    номенклатуры 1С на один размер суммируются; строка, чьи баркоды указывают на
    разные размеры, — конфликт, в остаток не идёт. Размеры справочника, которых в
    выгрузке нет, получают 0: выгрузка — это весь склад ЦС, отсутствие = закончился.
    Отрицательный остаток (пересортица в 1С) сохраняется как есть, в расчёте — 0."""
    taken_at = stock_file_time(path)
    snapshot_date = taken_at.date()
    rows = parse_stock_rows(read_text(path))
    per_size: dict[int, int] = defaultdict(int)
    report = {"file": path.name, "taken_at": taken_at.isoformat(sep=" "), "snapshot_date": snapshot_date.isoformat(),
              "rows": len(rows), "matched": 0, "unmatched": 0, "conflicts": [], "negative": 0}
    for row in rows:
        psids = resolver.resolve_barcodes(row["barcodes"])
        if not psids:
            report["unmatched"] += 1
            continue
        if len(psids) > 1:
            if len(report["conflicts"]) < MAX_LISTED:
                report["conflicts"].append(f"{row['article']} {row['size']} {row['color']}: баркоды "
                                           f"{','.join(row['barcodes'])} указывают на разные размеры")
            continue
        report["matched"] += 1
        if row["quantity"] < 0:
            report["negative"] += 1
        per_size[next(iter(psids))] += row["quantity"]
    all_sizes = [r[0] for r in conn.execute(select(t_product_sizes.c.product_size_id))]
    taken_at_utc = to_naive_utc(taken_at.replace(tzinfo=MSK))      # имя файла — время сервера 1С (Москва)
    values = [{"product_size_id": psid, "snapshot_date": snapshot_date, "qty": per_size.get(psid, 0),
               "taken_at": taken_at_utc} for psid in all_sizes]
    upsert(conn, t_stock_1c, values, keys=["product_size_id", "snapshot_date"], update=["qty", "taken_at"])
    report["sizes_in_stock"] = sum(1 for v in per_size.values() if v > 0)
    return report


def ingest_sales_files(conn: Connection, files: list[Path]) -> dict:
    """Продажи из `sales_*.txt`. Файл — полная картина своего периода: строки его каналов
    за этот период заменяются целиком (исправленная повторная выгрузка не задваивает,
    а удалённая в 1С продажа исчезает и здесь)."""
    report = {"files": [], "rows": 0, "errors": []}
    for path in files:
        period = sales_file_period(path)
        parsed = parse_sales(read_text(path))
        for err in parsed.errors[:50]:
            report["errors"].append(f"{path.name}: {err}")
        totals: dict[tuple[str, str, date], list] = defaultdict(lambda: [0, 0])
        for row in parsed.rows:
            if period and not (period[0] <= row["sale_date"] <= period[1]):
                report["errors"].append(f"{path.name}: дата {row['sale_date']} вне периода файла — пропущена")
                continue
            bucket = totals[(row["channel"], row["barcode"], row["sale_date"])]
            bucket[0] += row["qty"]
            bucket[1] += row["revenue"] or 0
        channels = {key[0] for key in totals}
        if period:
            for channel in channels:
                conn.execute(delete(t_channel_sales_daily).where(and_(
                    t_channel_sales_daily.c.channel == channel,
                    t_channel_sales_daily.c.sale_date >= period[0],
                    t_channel_sales_daily.c.sale_date <= period[1])))
        rows = [{"channel": c, "barcode": b, "sale_date": d, "qty": q, "revenue": r, "source": path.name}
                for (c, b, d), (q, r) in totals.items()]
        upsert(conn, t_channel_sales_daily, rows, keys=["channel", "barcode", "sale_date"],
               update=["qty", "revenue", "source"])
        report["files"].append({"file": path.name, "rows": len(rows), "channels": sorted(channels)})
        report["rows"] += len(rows)
    return report

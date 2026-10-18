"""Импорт ежедневной выгрузки продаж из 1С (строки чеков) — Excel (.xlsx) или CSV (;).

Ожидаемые колонки (названия настраиваются в config.yaml → onec.columns):
    Дата | Магазин | Продавец | Чек | Номенклатура | Номенклатурная группа | Количество | Сумма
Одна строка = одна позиция чека. Возврат — отрицательные количество и сумма.

Импорт идемпотентен: строки за (день, магазин), встретившиеся в файле, полностью заменяются.
Каждая пара (день, магазин) из файла отмечается в imported_days — это признак «день закрыт».
"""
from __future__ import annotations

import csv
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import openpyxl

from ..config import Settings
from ..db import DB


@dataclass
class ImportReport:
    file: str
    lines: int
    days: list[tuple[str, str]]  # (день, магазин)
    skipped: int
    unknown_sellers: set[str]


def _num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _day(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if v is None:
        return None
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", str(v))
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(v))
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _rows(path: Path) -> list[list]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
    return [row for row in csv.reader(text.splitlines(), dialect)]


def _find_header(rows: list[list], cfg: Settings) -> tuple[int, dict[str, int]]:
    want = {k: v.strip().lower() for k, v in cfg.onec.columns.model_dump().items()}
    for i, row in enumerate(rows[:50]):
        cells = [str(c).strip().lower() if c is not None else "" for c in row]
        if want["seller"] in cells and want["amount"] in cells:
            idx = {k: cells.index(v) for k, v in want.items() if v in cells}
            return i, idx
    raise ValueError("Не найдена строка заголовков: нужны хотя бы колонки "
                     f"«{cfg.onec.columns.seller}» и «{cfg.onec.columns.amount}» (см. docs/1C_EXPORT.md)")


def split_store(name: str, cfg: Settings) -> tuple[str, str | None]:
    name = " ".join(str(name).split())
    for suf, store in cfg.onec.seller_store_suffixes.items():
        if name.endswith(suf.strip()) and name != suf.strip():
            return name[: -len(suf.strip())].strip(), store
    return name, None


def import_file(path: str | Path, db: DB, cfg: Settings, fallback_day: date | None = None) -> ImportReport:
    path = Path(path)
    rows = _rows(path)
    h, idx = _find_header(rows, cfg)
    excl = {s.strip().lower() for s in cfg.onec.exclude_sellers}
    data: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    skipped = 0
    unknown: set[str] = set()
    for row in rows[h + 1:]:
        if not row or all(c in (None, "") for c in row):
            continue
        get = lambda k: row[idx[k]] if k in idx and idx[k] < len(row) else None  # noqa: E731
        raw_seller = get("seller")
        if raw_seller is None or str(raw_seller).strip().lower() in excl or str(raw_seller).lower().startswith("итог"):
            skipped += 1
            continue
        d = _day(get("date")) or fallback_day
        if d is None:
            skipped += 1
            continue
        seller, store_from_name = split_store(raw_seller, cfg)
        store = str(get("store") or store_from_name or cfg.onec.default_store).strip()
        sid = db.seller_id(seller, create=False)
        if sid is None:
            unknown.add(seller)
            sid = db.seller_id(seller, create=True)
        receipt = str(get("receipt") or f"{d.isoformat()}:{sid}")  # без номера чека средний чек = продажи за смену
        data[(d.isoformat(), store)].append(
            (d.isoformat(), store, sid, receipt, get("item"), get("category"), _num(get("qty")), _num(get("amount")),
             path.name))
    n = 0
    with db.tx() as c:
        for (day, store), lines in data.items():
            c.execute("DELETE FROM sales_lines WHERE day=? AND store=?", (day, store))
            c.executemany("INSERT INTO sales_lines(day,store,seller_id,receipt,item,category,qty,amount,source) "
                          "VALUES(?,?,?,?,?,?,?,?,?)", lines)
            c.execute("INSERT INTO imported_days(day,store,source,lines,imported_at) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(day,store) DO UPDATE SET source=excluded.source, lines=excluded.lines, "
                      "imported_at=excluded.imported_at",
                      (day, store, path.name, len(lines), datetime.now().isoformat(timespec="seconds")))
            n += len(lines)
    return ImportReport(path.name, n, sorted(data), skipped, unknown)


def import_inbox(db: DB, cfg: Settings) -> list[ImportReport]:
    """Импортирует все файлы из папки входящих и переносит их в архив."""
    inbox, archive = Path(cfg.onec.inbox), Path(cfg.onec.archive)
    archive.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(inbox.glob("*")):
        if f.suffix.lower() not in (".xlsx", ".xlsm", ".csv") or f.name.startswith("~$"):
            continue
        out.append(import_file(f, db, cfg))
        shutil.move(str(f), archive / f"{datetime.now():%Y%m%d-%H%M%S}_{f.name}")
    return out

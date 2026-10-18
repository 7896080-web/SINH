"""Импорт выгрузки из 1С по «Отчетам о розничных продажах» (ОРП) — CSV (;) или Excel.

Два вида файлов (тип определяется по заголовкам; названия колонок — config.yaml → onec):

1. Строки ОРП (ТЧ «Товары» и «Возвращенные товары»), файл orp_lines_*.csv:
       Дата;Склад;Документ;Продавец;Номенклатура;Номенклатурная группа;Количество;Сумма;Скидка
   Возврат — отрицательные количество и сумма.
2. Шапки ОРП, файл orp_docs_*.csv:
       Дата;Магазин;Касса;Документ;Чеков;Чеков возврата;Выручка

Импорт идемпотентен: данные за (день, магазин) из файла полностью заменяются.
Пары (день, магазин) отмечаются в imported_days — признак «день закрыт» (ОРП проведён).
"""
from __future__ import annotations

import csv
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import openpyxl

from ..config import Settings
from ..db import DB


@dataclass
class ImportReport:
    file: str
    kind: str  # lines | docs
    lines: int
    days: list[tuple[str, str]]
    skipped: int = 0
    unknown_sellers: set[str] = field(default_factory=set)


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
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    raw = path.read_bytes()
    text = ""
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return [row for row in csv.reader(text.splitlines(), delimiter=";")]


def _header(rows: list[list], want: dict[str, str], required: tuple[str, ...]) -> tuple[int, dict[str, int]] | None:
    want = {k: v.strip().lower() for k, v in want.items()}
    for i, row in enumerate(rows[:50]):
        cells = [str(c).strip().lower() if c is not None else "" for c in row]
        if all(want[k] in cells for k in required):
            return i, {k: cells.index(v) for k, v in want.items() if v in cells}
    return None


def store_name(raw, cfg: Settings) -> str:
    """«ГОРОД_склад», «P1 Город_Касса_Основная», «ГОРОД» → «Город» по onec.store_map."""
    s = " ".join(str(raw or "").split())
    low = s.lower()
    for key, name in sorted(cfg.onec.store_map.items(), key=lambda kv: -len(kv[0])):
        if key.lower() in low:
            return name
    return s or cfg.onec.default_store


def split_store(name: str, cfg: Settings) -> tuple[str, str | None]:
    """«Букалова Юлия МСЛ» → («Букалова Юлия», «МСЛ»)."""
    name = " ".join(str(name).split())
    for suf, store in cfg.onec.seller_store_suffixes.items():
        if name.endswith(suf.strip()) and name != suf.strip():
            return name[: -len(suf.strip())].strip(), store
    return name, None


def _mark_days(c, pairs, source):
    now = datetime.now().isoformat(timespec="seconds")
    for (day, store), n in pairs.items():
        c.execute("INSERT INTO imported_days(day,store,source,lines,imported_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(day,store) DO UPDATE SET source=excluded.source, lines=excluded.lines, "
                  "imported_at=excluded.imported_at", (day, store, source, n, now))


def _import_lines(path: Path, rows, h, idx, db: DB, cfg: Settings) -> ImportReport:
    excl = {s.strip().lower() for s in cfg.onec.exclude_sellers}
    data: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    skipped, unknown = 0, set()
    for row in rows[h + 1:]:
        if not row or all(c in (None, "") for c in row):
            continue
        get = lambda k: row[idx[k]] if k in idx and idx[k] < len(row) else None  # noqa: E731
        raw_seller = get("seller")
        if raw_seller is None or str(raw_seller).strip().lower() in excl or str(raw_seller).lower().startswith("итог"):
            skipped += 1
            continue
        d = _day(get("date"))
        if d is None:
            skipped += 1
            continue
        seller, store_from_name = split_store(raw_seller, cfg)
        store = store_name(get("store") or store_from_name, cfg)
        sid = db.seller_id(seller, create=False)
        if sid is None:
            unknown.add(seller)
            sid = db.seller_id(seller, create=True)
        data[(d.isoformat(), store)].append(
            (d.isoformat(), store, sid, str(get("doc") or ""), get("item"), get("category"), _num(get("qty")),
             _num(get("amount")), abs(_num(get("discount"))), path.name))
    n = 0
    with db.tx() as c:
        for (day, store), lines in data.items():
            c.execute("DELETE FROM sales_lines WHERE day=? AND store=?", (day, store))
            c.executemany("INSERT INTO sales_lines(day,store,seller_id,doc,item,category,qty,amount,discount,source) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)", lines)
            n += len(lines)
        _mark_days(c, {k: len(v) for k, v in data.items()}, path.name)
    return ImportReport(path.name, "lines", n, sorted(data), skipped, unknown)


def _import_docs(path: Path, rows, h, idx, db: DB, cfg: Settings) -> ImportReport:
    data: dict[tuple[str, str, str], list] = {}
    for row in rows[h + 1:]:
        if not row or all(c in (None, "") for c in row):
            continue
        get = lambda k: row[idx[k]] if k in idx and idx[k] < len(row) else None  # noqa: E731
        d = _day(get("date"))
        if d is None:
            continue
        kassa = " ".join(str(get("kassa") or "").split())
        store = store_name(get("store") or kassa, cfg)
        key = (d.isoformat(), store, kassa)
        cur = data.setdefault(key, [str(get("doc") or ""), 0, 0, 0.0])
        cur[1] += int(_num(get("checks")))
        cur[2] += int(_num(get("return_checks")))
        cur[3] += _num(get("revenue"))
    with db.tx() as c:
        for (day, store, kassa), (doc, ch, rch, rev) in data.items():
            c.execute("INSERT INTO store_days(day,store,kassa,doc,checks,return_checks,revenue,source) "
                      "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(day,store,kassa) DO UPDATE SET doc=excluded.doc, "
                      "checks=excluded.checks, return_checks=excluded.return_checks, revenue=excluded.revenue, "
                      "source=excluded.source", (day, store, kassa, doc, ch, rch, rev, path.name))
    days = sorted({(d, s) for d, s, _ in data})
    return ImportReport(path.name, "docs", len(data), days)


def import_file(path: str | Path, db: DB, cfg: Settings) -> ImportReport:
    path = Path(path)
    rows = _rows(path)
    dc = cfg.onec.doc_columns.model_dump()
    hd = _header(rows, dc, ("checks", "revenue"))
    if hd:
        return _import_docs(path, rows, hd[0], hd[1], db, cfg)
    hl = _header(rows, cfg.onec.columns.model_dump(), ("seller", "amount"))
    if hl:
        return _import_lines(path, rows, hl[0], hl[1], db, cfg)
    raise ValueError(f"{path.name}: не найдены заголовки ни строк ОРП («{cfg.onec.columns.seller}», "
                     f"«{cfg.onec.columns.amount}»), ни шапок («{cfg.onec.doc_columns.checks}», "
                     f"«{cfg.onec.doc_columns.revenue}») — см. docs/1C.md")


def import_inbox(db: DB, cfg: Settings) -> list[ImportReport]:
    """Импортирует все файлы из папки входящих (сначала шапки, потом строки) и переносит в архив."""
    inbox, archive = Path(cfg.onec.inbox), Path(cfg.onec.archive)
    archive.mkdir(parents=True, exist_ok=True)
    out = []
    files = [f for f in inbox.glob("*") if f.suffix.lower() in (".xlsx", ".xlsm", ".csv") and not f.name.startswith("~$")]
    for f in sorted(files, key=lambda f: (0 if "doc" in f.name.lower() else 1, f.name)):
        out.append(import_file(f, db, cfg))
        shutil.move(str(f), archive / f"{datetime.now():%Y%m%d-%H%M%S}_{f.name}")
    return out

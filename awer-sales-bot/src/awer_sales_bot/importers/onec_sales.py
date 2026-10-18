"""Импорт выгрузок из 1С — CSV (;) или Excel. Тип файла определяется по заголовкам
(названия колонок — config.yaml → onec):

1. Строки продаж, sales_*.csv — из чеков ККМ (есть «Чек» и «Время») или из ОРП (без них):
       Дата;Время;Склад;Документ;Чек;Продавец;Номенклатура;Номенклатурная группа;Количество;Цена;Сумма;Скидка;
       Ручная скидка %;Дисконтная карта
   Возврат — отрицательные количество и сумма. Пустой продавец → «(без продавца)».
2. Шапки ОРП, orp_docs_*.csv:  Дата;Магазин;Касса;Документ;Чеков;Чеков возврата;Выручка
3. Остатки, stock_*.csv:       Дата;Склад;Номенклатура;Номенклатурная группа;Характеристика;Остаток
4. Поступления, arrivals_*.csv: Дата;Склад;Документ;Номенклатура;Номенклатурная группа;Характеристика;Поступило
   [;Бренд;Цвет] — для карточек новинок в боте «Обучение»

Импорт идемпотентен: данные за (день, магазин) из файла полностью заменяются.
Пары (день, магазин) отмечаются в imported_days — признак «день закрыт» (ОРП проведён).
"""
from __future__ import annotations

import csv
import logging
import re
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import openpyxl

from ..config import Settings
from ..db import DB

log = logging.getLogger(__name__)


@dataclass
class ImportReport:
    file: str
    kind: str  # lines | docs | stock | arrivals
    lines: int
    days: list[tuple[str, str]]
    skipped: int = 0
    unknown_sellers: set[str] = field(default_factory=set)
    error: str = ""  # файл не распознан / ошибка выгрузки из 1С (kind onec_error) — перенесён в archive/bad
    foreign: dict[str, float] = field(default_factory=dict)  # склад/касса не магазина → сумма (строки не загружены)


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


def known_store(raw, cfg: Settings) -> str | None:
    """Магазин из cfg.stores или None: склад, которого нет в onec.store_map (ЦС Склад, маркетплейсы), не магазин —
    его продажи не входят ни в выручку магазинов, ни продавцам (аудит С1)."""
    store = store_name(raw, cfg)
    return store if store in cfg.stores else None


def split_store(name: str, cfg: Settings) -> tuple[str, str | None]:
    """«Букалова Юлия МСЛ» → («Букалова Юлия», «МСЛ»)."""
    name = " ".join(str(name).split())
    for suf, store in cfg.onec.seller_store_suffixes.items():
        if name.endswith(suf.strip()) and name != suf.strip():
            return name[: -len(suf.strip())].strip(), store
    return name, None


def service_seller(name: str, cfg: Settings) -> bool:
    """Не человек («Одежда сотрудникам Город», «Стажер»): выручка магазина без личного плана (onec.service_sellers).
    Имя целиком: «Стажер Эльмаз» — продавец."""
    norm = lambda x: " ".join(x.split()).lower().replace("ё", "е")  # noqa: E731
    return norm(name) in {norm(x) for x in cfg.onec.service_sellers}


def _mark_days(c, pairs, source):
    now = datetime.now().isoformat(timespec="seconds")
    for (day, store), n in pairs.items():
        c.execute("INSERT INTO imported_days(day,store,source,lines,imported_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(day,store) DO UPDATE SET source=excluded.source, lines=excluded.lines, "
                  "imported_at=excluded.imported_at", (day, store, source, n, now))


def _import_lines(path: Path, rows, h, idx, db: DB, cfg: Settings) -> ImportReport:
    excl = {s.strip().lower() for s in cfg.onec.exclude_sellers}
    data: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    skipped, unknown, foreign, service = 0, set(), defaultdict(float), set()
    for row in rows[h + 1:]:
        if not row or all(c in (None, "") for c in row):
            continue
        get = lambda k: row[idx[k]] if k in idx and idx[k] < len(row) else None  # noqa: E731
        raw_seller = str(get("seller") or "").strip()
        if raw_seller.lower() in excl or raw_seller.lower().startswith("итог"):
            skipped += 1
            continue
        if not raw_seller:
            raw_seller = cfg.onec.no_seller_name
        d = _day(get("date"))
        if d is None:
            skipped += 1
            continue
        seller, store_from_name = split_store(raw_seller, cfg)
        store = known_store(get("store") or store_from_name, cfg)
        if store is None:
            foreign[" ".join(str(get("store") or store_from_name).split())] += _num(get("amount"))
            skipped += 1
            continue
        sid = db.seller_id(seller, create=False)
        is_service = seller == cfg.onec.no_seller_name or service_seller(seller, cfg)
        if sid is None:
            sid = db.seller_id(seller, create=True)
            if not is_service:
                unknown.add(seller)
        if is_service:
            service.add(sid)
        qty, amount = _num(get("qty")), _num(get("amount"))
        disc = abs(_num(get("discount")))
        if not disc and "price" in idx and amount > 0:
            disc = max(0.0, _num(get("price")) * qty - amount)
        manual = abs(_num(get("price")) * qty) * _num(get("manual_pct")) / 100 if amount > 0 else 0.0
        receipt = str(get("receipt") or "").strip() or None
        t = get("time")
        tm = t.strftime("%H:%M") if hasattr(t, "strftime") else (str(t).strip()[:5] if t else None)
        card = str(get("card") or "").strip() or None
        data[(d.isoformat(), store)].append(
            (d.isoformat(), store, sid, str(get("doc") or ""), receipt, tm, get("item"), get("category"), qty,
             amount, disc, min(manual, disc) if disc else manual, card, path.name))
    n = 0
    with db.tx() as c:
        c.executemany("UPDATE sellers SET role='system', active=0 WHERE id=? AND role<>'system'",
                      [(sid,) for sid in service])
        for (day, store), lines in data.items():
            c.execute("DELETE FROM sales_lines WHERE day=? AND store=?", (day, store))
            c.executemany("INSERT INTO sales_lines(day,store,seller_id,doc,receipt,time,item,category,qty,amount,"
                          "discount,manual_discount,card,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lines)
            n += len(lines)
        _mark_days(c, {k: len(v) for k, v in data.items()}, path.name)
    return ImportReport(path.name, "lines", n, sorted(data), skipped, unknown, foreign=dict(foreign))


def _import_docs(path: Path, rows, h, idx, db: DB, cfg: Settings) -> ImportReport:
    data: dict[tuple[str, str, str], list] = {}
    foreign: dict[str, float] = defaultdict(float)
    for row in rows[h + 1:]:
        if not row or all(c in (None, "") for c in row):
            continue
        get = lambda k: row[idx[k]] if k in idx and idx[k] < len(row) else None  # noqa: E731
        d = _day(get("date"))
        if d is None:
            continue
        kassa = " ".join(str(get("kassa") or "").split())
        store = known_store(get("store") or kassa, cfg)
        if store is None:
            foreign[kassa or " ".join(str(get("store")).split())] += _num(get("revenue"))
            continue
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
    return ImportReport(path.name, "docs", len(data), days, foreign=dict(foreign))


def _brand(value, not_brands: set[str]) -> str | None:
    v = str(value or "").strip()
    return v if v and v.upper() not in not_brands else None


def _import_stock(path: Path, rows, h, idx, db: DB, cfg: Settings, kind: str) -> ImportReport:
    not_brands = {b.strip().upper() for b in cfg.onec.ignore_brands}
    recs = []
    for row in rows[h + 1:]:
        if not row or all(c in (None, "") for c in row):
            continue
        get = lambda k: row[idx[k]] if k in idx and idx[k] < len(row) else None  # noqa: E731
        d = _day(get("date"))
        if d is None or not get("item"):
            continue
        qty = _num(get("stock" if kind == "stock" else "arrived"))
        store = known_store(get("store"), cfg)
        if store is None:  # обработка 1С уже отбирает склады магазинов; сюда попадает только чужое
            continue
        recs.append((d.isoformat(), store, str(get("item")).strip(),
                     get("category"), str(get("size") or "").strip(), qty, str(get("doc") or ""),
                     _brand(get("brand"), not_brands), str(get("color") or "").strip() or None))
    pairs = sorted({(r[0], r[1]) for r in recs})
    with db.tx() as c:
        for day, store in pairs:
            c.execute(f"DELETE FROM {kind} WHERE day=? AND store=?", (day, store))
        if kind == "stock":
            # одинаковые позиции (разные партии) складываем
            agg: dict[tuple, list] = {}
            for r in recs:
                k = r[:3] + (r[4],)
                agg.setdefault(k, [r[3], 0.0])[1] += r[5]
            c.executemany("INSERT INTO stock(day,store,item,size,category,qty) VALUES(?,?,?,?,?,?)",
                          [(k[0], k[1], k[2], k[3], v[0], v[1]) for k, v in agg.items() if v[1] > 0])
        else:
            c.executemany("INSERT INTO arrivals(day,store,item,category,size,qty,doc,brand,color) "
                          "VALUES(?,?,?,?,?,?,?,?,?)", [r for r in recs if r[5] > 0])
    return ImportReport(path.name, kind, len(recs), pairs)


def import_file(path: str | Path, db: DB, cfg: Settings) -> ImportReport:
    path = Path(path)
    rows = _rows(path)
    sc = cfg.onec.stock_columns.model_dump()
    hs = _header(rows, sc, ("store", "item", "stock"))
    if hs:
        return _import_stock(path, rows, hs[0], hs[1], db, cfg, "stock")
    ha = _header(rows, sc, ("store", "item", "arrived"))
    if ha:
        return _import_stock(path, rows, ha[0], ha[1], db, cfg, "arrivals")
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


FRESH_SECONDS = 60  # файл моложе минуты может ещё дописываться 1С — заберём в следующий раз


def archive_name(archive: Path, name: str) -> Path:
    """Имя в архиве со временем приёма — файл с тем же именем не затирает прошлый (аудит Н6)."""
    return archive / f"{datetime.now():%Y%m%d-%H%M%S}_{name}"


def import_inbox(db: DB, cfg: Settings) -> list[ImportReport]:
    """Импортирует все файлы из папки входящих (сначала шапки, потом строки) и переносит в архив.

    Файл, который не удалось прочитать, не останавливает остальные (аудит В1): он уходит в archive/bad,
    в отчёте — error, и владелец получает сообщение (jobs.daily_run)."""
    inbox, archive = Path(cfg.onec.inbox), Path(cfg.onec.archive)
    archive.mkdir(parents=True, exist_ok=True)
    out = []
    now = time.time()
    files = [f for f in inbox.glob("*") if f.suffix.lower() in (".xlsx", ".xlsm", ".csv")
             and not f.name.startswith(("~$", ".")) and now - f.stat().st_mtime >= FRESH_SECONDS]
    order = lambda f: (0 if "doc" in f.name.lower() else 2 if f.name.lower().startswith(("stock", "arrival")) else 1, f.name)  # noqa: E731
    for f in sorted(files, key=order):
        try:
            out.append(import_file(f, db, cfg))
            shutil.move(str(f), archive_name(archive, f.name))
        except Exception as e:  # noqa: BLE001
            log.exception("файл %s не импортирован", f.name)
            bad = archive / "bad"
            bad.mkdir(exist_ok=True)
            shutil.move(str(f), archive_name(bad, f.name))
            out.append(ImportReport(f.name, "error", 0, [], error=str(e)[:300]))
    # обработка 1С пишет error_*.txt, если раздел не выгрузился (неверное имя реквизита и т. п.)
    for f in sorted(inbox.glob("error_*.txt")):
        if now - f.stat().st_mtime < FRESH_SECONDS:
            continue
        text = " ".join(f.read_text(encoding="utf-8-sig", errors="replace").split())
        (archive / "bad").mkdir(exist_ok=True)
        shutil.move(str(f), archive_name(archive / "bad", f.name))
        out.append(ImportReport(f.name, "onec_error", 0, [], error=text[:500] or "без описания"))
    return out

"""Импорт выгрузок из 1С — CSV (;) или Excel. Тип файла определяется по заголовкам
(названия колонок — config.yaml → onec):

1. Строки продаж:
       Дата;Время;Склад;Документ;Чек;Продавец;Номенклатура;Номенклатурная группа;Количество;Цена;Сумма;Скидка;
       Ручная скидка %;Дисконтная карта[;Сертификат]
   * sales_*.csv из «Отчетов о розничных продажах» (колонка «Чек» пуста) → sales_lines — ИСТИНА для денег: план,
     выполнение, ставка, выторг магазина (решение владельца 19.10.2026). В деньги идёт «Сумма» минус «Сертификат» —
     часть, оплаченная подарочным сертификатом: его продажа уже засчитана тому, кто продал (SPEC «Откуда деньги»);
   * receipts_*.csv из чеков ККМ (есть «Чек» и «Время»; так же — старый sales_*.csv, выгруженный из чеков) →
     receipt_lines — только средний чек, вещей в чеке, часы продаж, покупки по картам, связки товаров.
   Возврат — отрицательные количество и сумма (уменьшает день возврата). Пустой продавец → «(без продавца)».
2. Шапки ОРП, orp_docs_*.csv:  Дата;Магазин;Касса;Документ;Чеков;Чеков возврата;Выручка[;Оплата сертификатами]
3. Остатки, stock_*.csv:       Дата;Склад;Номенклатура;Номенклатурная группа;Характеристика;Остаток
4. Поступления, arrivals_*.csv: Дата;Склад;Документ;Номенклатура;Номенклатурная группа;Характеристика;Поступило
   [;Бренд;Цвет] — для карточек новинок в боте «Обучение»

Импорт идемпотентен: строки за весь период файла (из имени «…_ГГГГММДД_ГГГГММДД») по обоим магазинам заменяются
целиком — чек, перенесённый в 1С на другой день, не удваивается (аудит И2). Без периода в имени заменяются только
пары (день, магазин), которые есть в файле.
Пары (день, магазин) из строк ОРП отмечаются в imported_days — признак «день закрыт» (ОРП проведён).
"""
from __future__ import annotations

import csv
import logging
import os
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
    kind: str  # lines (ОРП) | receipts (чеки) | docs | stock | arrivals | error | onec_error
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
        try:
            return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        finally:
            wb.close()  # на Windows открытый файл не перенести в архив
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


_PERIOD = re.compile(r"(20\d{6})_(20\d{6})")


def _period(name: str) -> tuple[str, str] | None:
    """Период выгрузки из имени файла sales_20261017_20261018.csv. Без него (файл по одному магазину, ручной
    отчёт) строки заменяются только по парам (день, магазин), которые есть в файле."""
    m = _PERIOD.search(name)
    if not m:
        return None
    a, b = (f"{x[:4]}-{x[4:6]}-{x[6:]}" for x in m.groups())
    return a, b


def _mark_days(c, pairs, source):
    now = datetime.now().isoformat(timespec="seconds")
    for (day, store), n in pairs.items():
        c.execute("INSERT INTO imported_days(day,store,source,lines,imported_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(day,store) DO UPDATE SET source=excluded.source, lines=excluded.lines, "
                  "imported_at=excluded.imported_at", (day, store, source, n, now))


def _import_lines(path: Path, rows, h, idx, db: DB, cfg: Settings, name: str) -> ImportReport:
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
        qty, amount, cert = _num(get("qty")), _num(get("amount")), _num(get("cert"))
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
             amount, disc, min(manual, disc) if disc else manual, card, name, cert))
    # чеки — только для показателей; деньги — по ОРП. Файл с номерами чеков — это чеки, как бы он ни назывался
    receipts = name.lower().startswith("receipts") or any(ln[4] for ls in data.values() for ln in ls)
    table = "receipt_lines" if receipts else "sales_lines"
    if not receipts:  # деньги продавцу — без оплаты сертификатом; в чеках сумма остаётся размером покупки
        data = {k: [ln[:9] + (ln[9] - ln[14],) + ln[10:] for ln in ls] for k, ls in data.items()}
    period = _period(name)
    n = 0
    with db.tx() as c:
        c.executemany("UPDATE sellers SET role='system', active=0 WHERE id=? AND role NOT IN ('system', 'test')",
                      [(sid,) for sid in service])
        if period:
            c.execute(f"DELETE FROM {table} WHERE day BETWEEN ? AND ?", period)
        for (day, store), lines in data.items():
            if not period:
                c.execute(f"DELETE FROM {table} WHERE day=? AND store=?", (day, store))
            c.executemany(f"INSERT INTO {table}(day,store,seller_id,doc,receipt,time,item,category,qty,amount,"
                          "discount,manual_discount,card,source,cert) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lines)
            n += len(lines)
        if not receipts:
            if period:  # день и магазин, которых больше нет в выгрузке ОРП, — снова «нет данных» (не «закрыто»)
                have = {f"{d}|{st}" for d, st in data}
                for r in c.execute("SELECT day, store FROM imported_days WHERE day BETWEEN ? AND ? AND source<>'closed'",
                                   period).fetchall():
                    if f"{r['day']}|{r['store']}" not in have:
                        c.execute("DELETE FROM imported_days WHERE day=? AND store=?", (r["day"], r["store"]))
            _mark_days(c, {k: len(v) for k, v in data.items()}, name)
    return ImportReport(name, "receipts" if receipts else "lines", n, sorted(data), skipped, unknown,
                        foreign=dict(foreign))


def _import_docs(path: Path, rows, h, idx, db: DB, cfg: Settings, name: str) -> ImportReport:
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
        cur = data.setdefault(key, [str(get("doc") or ""), 0, 0, 0.0, 0.0])
        cur[1] += int(_num(get("checks")))
        cur[2] += int(_num(get("return_checks")))
        cur[3] += _num(get("revenue"))
        cur[4] += _num(get("certs"))
    with db.tx() as c:
        for (day, store, kassa), (doc, ch, rch, rev, certs) in data.items():
            c.execute("INSERT INTO store_days(day,store,kassa,doc,checks,return_checks,revenue,source,certs) "
                      "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(day,store,kassa) DO UPDATE SET doc=excluded.doc, "
                      "checks=excluded.checks, return_checks=excluded.return_checks, revenue=excluded.revenue, "
                      "source=excluded.source, certs=excluded.certs", (day, store, kassa, doc, ch, rch, rev, name, certs))
    days = sorted({(d, s) for d, s, _ in data})
    return ImportReport(name, "docs", len(data), days, foreign=dict(foreign))


def _brand(value, not_brands: set[str]) -> str | None:
    v = str(value or "").strip()
    return v if v and v.upper() not in not_brands else None


def _import_stock(path: Path, rows, h, idx, db: DB, cfg: Settings, kind: str, name: str) -> ImportReport:
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
    return ImportReport(name, kind, len(recs), pairs)


def import_file(path: str | Path, db: DB, cfg: Settings, name: str | None = None) -> ImportReport:
    """name — исходное имя файла (по нему видно чеки и период), если файл забран под другим именем."""
    path = Path(path)
    name = name or path.name
    rows = _rows(path)
    sc = cfg.onec.stock_columns.model_dump()
    hs = _header(rows, sc, ("store", "item", "stock"))
    if hs:
        return _import_stock(path, rows, hs[0], hs[1], db, cfg, "stock", name)
    ha = _header(rows, sc, ("store", "item", "arrived"))
    if ha:
        return _import_stock(path, rows, ha[0], ha[1], db, cfg, "arrivals", name)
    dc = cfg.onec.doc_columns.model_dump()
    hd = _header(rows, dc, ("checks", "revenue"))
    if hd:
        return _import_docs(path, rows, hd[0], hd[1], db, cfg, name)
    hl = _header(rows, cfg.onec.columns.model_dump(), ("seller", "amount"))
    if hl:
        return _import_lines(path, rows, hl[0], hl[1], db, cfg, name)
    raise ValueError(f"{name}: не найдены заголовки ни строк ОРП («{cfg.onec.columns.seller}», "
                     f"«{cfg.onec.columns.amount}»), ни шапок («{cfg.onec.doc_columns.checks}», "
                     f"«{cfg.onec.doc_columns.revenue}») — см. docs/1C.md")


FRESH_SECONDS = 60  # файл моложе минуты может ещё дописываться 1С — заберём в следующий раз


def archive_name(archive: Path, name: str) -> Path:
    """Имя в архиве со временем приёма — файл с тем же именем не затирает прошлый (аудит Н6)."""
    return archive / f"{datetime.now():%Y%m%d-%H%M%S}_{name}"


def _claim(f: Path) -> Path | None:
    """Забрать файл переименованием: служба и ручной `awer-bot import-sales` не возьмут его оба (аудит И3)."""
    work = f.with_name(f".work-{os.getpid()}-{f.name}")
    try:
        os.replace(f, work)
    except (FileNotFoundError, PermissionError):  # уже забрали или 1С ещё держит файл — в следующий раз
        return None
    return work


def import_inbox(db: DB, cfg: Settings) -> list[ImportReport]:
    """Импортирует все файлы из папки входящих (шапки ОРП, строки ОРП, чеки, остатки) и переносит в архив.

    Файл, который не удалось прочитать, не останавливает остальные (аудит В1): он уходит в archive/bad,
    в отчёте — error, и владелец получает сообщение (jobs.daily_run)."""
    inbox, archive = Path(cfg.onec.inbox), Path(cfg.onec.archive)
    archive.mkdir(parents=True, exist_ok=True)
    out = []
    now = time.time()
    files = [f for f in inbox.glob("*") if f.suffix.lower() in (".xlsx", ".xlsm", ".csv")
             and not f.name.startswith(("~$", ".")) and now - f.stat().st_mtime >= FRESH_SECONDS]
    rank = {"orp_docs": 0, "sales": 1, "receipts": 2}
    order = lambda f: (next((v for k, v in rank.items() if f.name.lower().startswith(k)), 3), f.name)  # noqa: E731
    for f in sorted(files, key=order):
        work = _claim(f)
        if work is None:
            continue
        try:
            out.append(import_file(work, db, cfg, f.name))
            shutil.move(str(work), archive_name(archive, f.name))
        except Exception as e:  # noqa: BLE001
            log.exception("файл %s не импортирован", f.name)
            bad = archive / "bad"
            bad.mkdir(exist_ok=True)
            shutil.move(str(work), archive_name(bad, f.name))
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


@dataclass
class Mismatch:
    day: str
    store: str
    lines: float  # строки ОРП (без оплаты сертификатом)
    total: float  # итог ОРП («Выручка» шапки)
    certs: float  # оплата сертификатами по шапке
    cert_lines: float  # оплата сертификатами, указанная в строках

    @property
    def certs_lost(self) -> bool:
        """Разница — оплата сертификатами, которую 1С не указала в строках («СкидкаСертификатами» пуста)."""
        return self.certs > 0 and abs(self.lines - self.total - (self.certs - self.cert_lines)) < 1

    def text(self, rub) -> str:
        tail = " — оплата сертификатами не разнесена по продавцам" if self.certs_lost else ""
        return f"строки ОРП {rub(self.lines)}, итог ОРП {rub(self.total)}{tail}"


def reconcile(db: DB, days: set[str], threshold: float = 1000) -> list[Mismatch]:
    """Строки ОРП (без оплаты сертификатом) против итога ОРП по дням и магазинам, где разница ≥ threshold.
    Должны совпадать: расхождение — ошибка выгрузки, склад не магазина в документе (аудит И1) или сертификат,
    не указанный в строках (Mismatch.certs_lost)."""
    if not days:
        return []
    marks = ",".join("?" * len(days))
    rows = db.q(f"""SELECT d.day, d.store, d.rev, d.certs, COALESCE(l.amt, 0) amt, COALESCE(l.cert, 0) cert FROM
                    (SELECT day, store, SUM(revenue) rev, SUM(certs) certs FROM store_days WHERE day IN ({marks})
                     GROUP BY day, store) d
                    LEFT JOIN (SELECT day, store, SUM(amount) amt, SUM(cert) cert FROM sales_lines WHERE day IN ({marks})
                               GROUP BY day, store) l USING(day, store)""", *sorted(days), *sorted(days))
    return [Mismatch(r["day"], r["store"], r["amt"], r["rev"], r["certs"], r["cert"]) for r in rows
            if abs(r["amt"] - r["rev"]) >= threshold]

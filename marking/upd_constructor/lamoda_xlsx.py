"""Чтение выгрузки Lamoda «Поставки FBO», лист «Поставка».
Строки 1-2: «Дата поставки» / «Номер поставки» (значение во 2-й ячейке), далее строка заголовков и данные.
Колонки: SKU (это НАИМЕНОВАНИЕ товара), Количество, Стоимость (без НДС), EAN, Вес, Data Matrix, Lamoda SKU."""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
import openpyxl

ALIASES = {
    "name": ("наименование", "название", "товар", "sku"),
    "gtin": ("гтин", "код товара", "gtin", "ean"),
    "kiz": ("киз", "код маркировки", "маркировка", "data matrix", "datamatrix"),
    "price": ("цена", "стоимость", "price"),
    "qty": ("количество", "кол-во", "qty"),
}


@dataclass
class RawRow:
    excel_row: int
    name: str
    gtin: str
    kiz: str
    price: Decimal | None
    qty: Decimal


@dataclass
class Supply:
    number: str | None
    date: str | None          # ДД.ММ.ГГГГ
    rows: list = field(default_factory=list)
    problems: list = field(default_factory=list)


def _match(header) -> str | None:
    low = str(header or "").strip().lower()
    for key, names in ALIASES.items():   # порядок ключей важен: name раньше kiz/gtin
        if any(n in low for n in names):
            return key
    return None


def _ru_date(v) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%d.%m.%Y")
    return str(v).strip()


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():   # EAN, прочитанный как float
        return str(int(v))
    return str(v).strip()


def read_supply(path) -> Supply:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb["Поставка"] if "Поставка" in wb.sheetnames else wb[wb.sheetnames[0]]
    aoa = [list(r) for r in ws.iter_rows(values_only=True)]
    sup = Supply(None, None)
    hdr_idx, cols = -1, {}
    for i, row in enumerate(aoa[:10]):
        c0 = str(row[0] or "").strip().lower() if row else ""
        if "дата поставки" in c0:
            sup.date = _ru_date(row[1])
        elif "номер поставки" in c0:
            sup.number = _cell_str(row[1]) or None
        elif hdr_idx == -1:
            found = {}
            for j, h in enumerate(row):
                k = _match(h)
                if k and k not in found:      # первое вхождение (SKU раньше «Lamoda SKU»)
                    found[k] = j
            if len(found) >= 3:
                hdr_idx, cols = i, found
    if hdr_idx == -1:
        sup.problems.append("не найдена строка заголовков")
        return sup
    for i, row in enumerate(aoa[hdr_idx + 1:], start=hdr_idx + 2):
        if not any(c not in (None, "") for c in row):
            continue
        get = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else None
        try:
            price = Decimal(str(get("price")).replace(",", ".")) if get("price") not in (None, "") else None
        except Exception:
            price = None
        qraw = get("qty")
        qty = Decimal(str(qraw).replace(",", ".")) if qraw not in (None, "") else Decimal(1)
        r = RawRow(i, _cell_str(get("name")), _cell_str(get("gtin")), _cell_str(get("kiz")), price, qty)
        if not r.name: sup.problems.append(f"строка {i}: нет наименования")
        if not r.kiz: sup.problems.append(f"строка {i}: нет КИЗ (Data Matrix)")
        if not r.gtin: sup.problems.append(f"строка {i}: нет EAN/ГТИН")
        if r.price is None: sup.problems.append(f"строка {i}: нет цены")
        sup.rows.append(r)
    kizs = [r.kiz for r in sup.rows if r.kiz]
    if len(kizs) != len(set(kizs)):
        sup.problems.append("есть дубли КИЗ")
    return sup

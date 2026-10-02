"""Страница «Цены товаров»: строка — SKU 1С (артикул, цвет, размер), колонки —
базовая цена и цена каждой площадки с коэффициентом маржинальности.

Цена площадки здесь одна на ВСЮ площадку (у WB три ИП): что задано на этой
странице, уходит во все её кабинеты, если у кабинета нет своей ручной цены.
Коэффициент маржинальности — тот же, что везде в программе:
к получению / себестоимость ₽ (2 = +100%).

Страница ничего не отправляет: заданные цены попадают в предложения при
«Рассчитать», дальше — пол наценки и подтверждение, как у всех цен.
Пустая ячейка в файле ничего не меняет, «-» снимает значение.
"""
from __future__ import annotations

import math
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import audit, mapping, rates
from priceapp.database import get_db
from priceapp.deps import get_current_user
from priceapp.excel import xlsx_response
from priceapp.flash import flash
from priceapp.models import BasePrice, OnecBarcode, OnecCost, PlatformPrice, ProductPrice, User
from priceapp.pages import render
from priceapp.platforms import PLATFORMS
from priceapp.pricing import (BLOCK_FLOOR, _ru, decide_for, load_inputs, price_for_coef, round_price)
from priceapp.routers.prices import (BULK_LIMIT, CLEAR_CELL, ROWS_LIMIT, _accounts, _cell, _f,
                                     _import_flash, _num, _platforms, _read_upload, parse_manual)

router = APIRouter()

FILTERS = {"": "все", "no_base": "без базовой цены", "has_manual": "с ценой, заданной на площадке",
           "below_floor": "где-то ниже пола", "no_cost": "нет себестоимости"}
ACTIONS = {
    "coef_cost": "Коэффициент от себестоимости",
    "set": "Цена = число",
    "mult": "Умножить текущую на коэффициент",
    "from_base": "Цена площадки = базовая × коэффициент",
    "clear": "Снять (вернуть к правилу)",
}
KEEP = ("art", "barcode", "color", "size", "flt")
BASE_COL = "Базовая цена, ₽"


def _back(**params) -> RedirectResponse:
    q = urlencode({k: v for k, v in params.items() if v not in ("", None)})
    return RedirectResponse("/sku-prices" + (f"?{q}" if q else ""), status_code=303)


def _keep(src) -> dict:
    return {k: str(src.get(k) or "") for k in KEEP}


def manual_col(platform: str) -> str:
    return f"{PLATFORMS[platform]}: цена на площадке, ₽"


def build(db: Session, art: str = "", barcode: str = "", color: str = "", size: str = "",
          flt: str = "") -> tuple[list[dict], list[str]]:
    """Строки страницы. Цена площадки считается ТЕМ ЖЕ `decide_for`, что и
    расчёт предложений, — на уровне площадки, без ручной цены кабинета."""
    accs = _accounts(db)
    plats = _platforms(accs)
    on_platform: dict[str, set[str]] = {}
    own_manual: dict[tuple, int] = {}
    plat_barcodes: dict[str, set[str]] = {}
    for a in accs:
        for item_id, items in mapping.account_items(db, a.id).items():
            on_platform.setdefault(item_id, set()).add(a.platform)
            plat_barcodes.setdefault(item_id, set()).update(i.barcode for i in items)
    for pp in db.query(ProductPrice).filter(ProductPrice.manual_price.isnot(None)):
        acc = next((a for a in accs if a.id == pp.account_id), None)
        if acc:
            own_manual[(pp.item_id, acc.platform)] = own_manual.get((pp.item_id, acc.platform), 0) + 1
    # Колонка — только площадка, где есть хоть один товар: пустая колонка (кабинет
    # без каталога) только занимает место и выглядит как «цен нет».
    present = set().union(*on_platform.values()) if on_platform else set()
    plats = [p for p in plats if p in present]
    base = {b.item_id: b.price for b in db.query(BasePrice)}
    item_ids = set(on_platform) | set(base)
    info: dict[str, OnecBarcode] = {}
    barcodes: dict[str, list[str]] = {}
    ids = list(item_ids)
    for i in range(0, len(ids), 500):
        for b in db.query(OnecBarcode).filter(OnecBarcode.item_id.in_(ids[i:i + 500])):
            info.setdefault(b.item_id, b)
            barcodes.setdefault(b.item_id, []).append(b.barcode)
    costs = {c.item_id: c.cost_usd for c in db.query(OnecCost).filter(OnecCost.item_id.in_(ids))}
    rate = rates.current(db)
    usd = rate.usd_rub if rate else None
    inputs = {p: load_inputs(db, p) for p in plats}
    a_l, b_l, c_l, s_l = art.strip().lower(), barcode.strip(), color.strip().lower(), size.strip().lower()
    rows = []
    for item_id in item_ids:
        sku = info.get(item_id)
        if a_l and a_l not in (sku.article.lower() if sku else item_id.lower()):
            continue
        if b_l and not any(b_l in bc for bc in [*barcodes.get(item_id, []), *plat_barcodes.get(item_id, ())]):
            continue
        if c_l and c_l not in (sku.color.lower() if sku else ""):
            continue
        if s_l and s_l != (sku.size.lower() if sku else ""):
            continue
        cost_usd = costs.get(item_id)
        cost_rub = (Decimal(str(cost_usd)) * usd).quantize(Decimal("0.01")) if cost_usd and usd else None
        bp = base.get(item_id)
        cells = {}
        for p in plats:
            if p not in on_platform.get(item_id, set()):
                cells[p] = None
                continue
            d = decide_for(inputs[p], item_id, cost_usd, usd, None)
            cells[p] = {"price": d.new_price, "coef": d.markup_coef, "source": d.source, "note": d.note,
                        "floor": d.block_reason == BLOCK_FLOOR,
                        "manual": inputs[p].platform_prices.get(item_id),
                        "own": own_manual.get((item_id, p), 0)}
        row = {"item_id": item_id, "sku": sku, "barcodes": barcodes.get(item_id, []), "cost_usd": cost_usd,
               "cost_rub": cost_rub, "base": bp,
               "base_coef": (Decimal(bp) / cost_rub).quantize(Decimal("0.01")) if bp and cost_rub else None,
               "cells": cells}
        if flt == "no_base" and bp:
            continue
        if flt == "has_manual" and not any(c and c["manual"] for c in cells.values()):
            continue
        if flt == "below_floor" and not any(c and c["floor"] for c in cells.values()):
            continue
        if flt == "no_cost" and cost_usd:
            continue
        rows.append(row)
    rows.sort(key=lambda r: ((r["sku"].article if r["sku"] else r["item_id"]), (r["sku"].color if r["sku"] else ""),
                             (r["sku"].size if r["sku"] else "")))
    return rows, plats


@router.get("/sku-prices")
def page(request: Request, art: str = Query(""), barcode: str = Query(""), color: str = Query(""),
         size: str = Query(""), flt: str = Query(""), db: Session = Depends(get_db),
         user: User = Depends(get_current_user)):
    rows, plats = build(db, art, barcode, color, size, flt)
    rules = {p: load_inputs(db, p).rule for p in plats}
    db.commit()
    keep = {"art": art, "barcode": barcode, "color": color, "size": size, "flt": flt}
    return render(request, "sku_prices.html", user, "sku_prices", rows=rows[:ROWS_LIMIT], total=len(rows),
                  plats=plats, names=PLATFORMS, rules=rules, filters=FILTERS, actions=ACTIONS, ru=_ru,
                  rate=rates.current(db), export_qs=urlencode(keep), **keep)


def _set(db: Session, user: User, item_id: str, target: str, value: int | None) -> bool:
    """Записать базовую цену (target == "base") или цену площадки. None — снять."""
    if target == "base":
        row = db.get(BasePrice, item_id)
        if value is None:
            if row is None:
                return False
            db.delete(row)
            return True
        if row is None:
            db.add(BasePrice(item_id=item_id, price=value, updated_by=user.username))
            db.flush()       # autoflush выключен: повтор товара в файле иначе — второй INSERT
            return True
        if row.price == value:
            return False
        row.price, row.updated_by = value, user.username
        return True
    row = db.query(PlatformPrice).filter(PlatformPrice.item_id == item_id, PlatformPrice.platform == target).first()
    if value is None:
        if row is None:
            return False
        db.delete(row)
        db.flush()
        return True
    if row is None:
        db.add(PlatformPrice(item_id=item_id, platform=target, price=value, updated_by=user.username))
        db.flush()
        return True
    if row.price == value:
        return False
    row.price, row.updated_by = value, user.username
    return True


@router.post("/sku-prices/row/{item_id}")
async def save_row(item_id: str, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Ручная правка строки. В поле страницы пустое значение — осознанное
    «снять»: человек стёр число."""
    form = await request.form()
    keep = _keep(form)
    changed, errors = 0, []
    for target in ["base", *(p for p in PLATFORMS if f"p_{p}" in form)]:
        raw = form.get("base" if target == "base" else f"p_{target}")
        if raw is None:
            continue
        kind, value = parse_manual(raw if str(raw).strip() else CLEAR_CELL)
        if kind == "bad":
            errors.append(f"«{raw}» — нужно целое число рублей больше нуля")
            continue
        changed += _set(db, user, item_id, target, value)
    if changed:
        audit.log(db, user.username, "sku_price_row", item_id, f"изменено {changed}")
    db.commit()
    _import_flash(request, f"Изменено цен: {changed}. На площадки ничего не отправлено — «Рассчитать» на «Ценах».",
                  errors)
    return _back(**keep)


def _round_base(value: Decimal) -> int:
    return int(math.ceil(value))


@router.post("/sku-prices/bulk")
async def bulk(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Массовая правка: отмеченные строки или весь отбор; цель — базовая цена,
    одна площадка или все площадки товара."""
    form = await request.form()
    keep = _keep(form)
    target, action = str(form.get("target") or ""), str(form.get("action") or "")
    rows, plats = build(db, **keep)
    targets = plats if target == "all" else [target]
    if action not in ACTIONS or not targets or any(t != "base" and t not in plats for t in targets):
        flash(request, "Выберите, что править, и действие.", "warn")
        return _back(**keep)
    if action == "from_base" and "base" in targets:
        flash(request, "«От базовой» — действие для цены площадки, а не для самой базовой.", "warn")
        return _back(**keep)
    value = None
    try:
        if action == "set":
            value = _num(form.get("value"), 1, 10_000_000, True)
        elif action != "clear":
            value = _num(form.get("value"), Decimal("0.1"), 100, False)
    except ValueError as e:
        flash(request, f"«{ACTIONS[action]}»: значение — {e}.", "warn")
        return _back(**keep)
    if form.get("all_filtered") == "1":
        chosen = rows
    else:
        ids = set(str(v) for v in form.getlist("ids"))
        chosen = [r for r in rows if r["item_id"] in ids]
    if not chosen:
        flash(request, "Ничего не отмечено.", "warn")
        return _back(**keep)
    if len(chosen) > BULK_LIMIT:
        flash(request, f"В отборе {len(chosen)} строк — больше {BULK_LIMIT}. Сузьте отбор.", "warn")
        return _back(**keep)
    rules = {p: load_inputs(db, p).rule for p in plats}
    changed = 0
    skipped: dict[str, int] = {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    for r in chosen:
        for t in targets:
            if t != "base" and r["cells"].get(t) is None:
                if target != "all":
                    skip("товара нет на площадке")
                continue
            new = None
            if action == "clear":
                new = None
            elif action == "set":
                new = value
            elif t == "base":
                if action == "coef_cost":
                    if r["cost_rub"] is None:
                        skip("нет себестоимости или курса")
                        continue
                    new = _round_base(r["cost_rub"] * value)
                else:      # mult
                    if not r["base"]:
                        skip("нет базовой цены")
                        continue
                    new = _round_base(Decimal(r["base"]) * value)
            else:
                rule = rules[t]
                if action == "coef_cost":
                    if r["cost_rub"] is None or rule.commission_percent is None:
                        skip("нет себестоимости, курса или комиссии площадки")
                        continue
                    # Коэффициент маржинальности = к получению / себестоимость.
                    raw = price_for_coef(r["cost_rub"], value, rule.commission_percent)
                elif action == "from_base":
                    if not r["base"]:
                        skip("нет базовой цены")
                        continue
                    raw = Decimal(r["base"]) * value
                else:      # mult — от того, что сейчас уходит на площадку
                    cur = r["cells"][t]["price"]
                    if not cur:
                        skip("нет текущей расчётной цены")
                        continue
                    raw = Decimal(cur) * value
                new = round_price(raw, rule.round_step, rule.round_minus)
            changed += _set(db, user, r["item_id"], t, new)
    where = "базовая цена" if target == "base" else ("все площадки" if target == "all" else PLATFORMS[target])
    audit.log(db, user.username, "sku_price_bulk", where,
              f"{ACTIONS[action]} {value if value is not None else ''}; строк {len(chosen)}; изменено {changed}; "
              f"пропущено {skipped}")
    db.commit()
    msg = (f"«{ACTIONS[action]}» ({where}) по {len(chosen)} строкам: изменено {changed}"
           + ("; пропущено: " + "; ".join(f"{n} — {w}" for w, n in skipped.items()) if skipped else "")
           + ". На площадки ничего не отправлено — «Рассчитать» на «Ценах».")
    flash(request, msg, "warn" if skipped else "ok")
    return _back(**keep)


@router.get("/sku-prices/export")
def export(art: str = Query(""), barcode: str = Query(""), color: str = Query(""), size: str = Query(""),
           flt: str = Query(""), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows, plats = build(db, art, barcode, color, size, flt)
    db.commit()
    headers = ["ID_1С", "Артикул", "Цвет", "Размер", "Наименование", "Штрихкоды", "Себестоимость, $",
               "Себестоимость, ₽", BASE_COL, "Коэффициент базовой"]
    for p in plats:
        headers += [f"{PLATFORMS[p]}: уходит, ₽", f"{PLATFORMS[p]}: коэффициент маржинальности", manual_col(p)]
    data = []
    for r in rows:
        s = r["sku"]
        line = [r["item_id"], s.article if s else "", s.color if s else "", s.size if s else "",
                s.name if s else "", ", ".join(r["barcodes"]), _f(r["cost_usd"]), _f(r["cost_rub"]),
                r["base"], _f(r["base_coef"])]
        for p in plats:
            c = r["cells"][p]
            line += [c["price"], _f(c["coef"]), c["manual"]] if c else [None, None, None]
        data.append(line)
    return xlsx_response(headers, data, "цены_товаров.xlsx")


@router.post("/sku-prices/import")
def import_file(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Читаются ТОЛЬКО «Базовая цена, ₽» и «<площадка>: цена на площадке, ₽»;
    остальное справочное. Пустая ячейка ничего не меняет, «-» снимает."""
    rows = _read_upload(request, file, "ID_1С")
    if rows is None:
        return _back()
    all_rows, plats = build(db)
    known = {r["item_id"]: r for r in all_rows}
    columns = [("base", BASE_COL)] + [(p, manual_col(p)) for p in plats]
    changed, errors = 0, []
    for i, row in enumerate(rows, start=2):
        item_id = _cell(row.get("ID_1С"))
        if not item_id:
            continue
        if item_id not in known:
            errors.append(f"строка {i}: {item_id} — нет среди товаров страницы")
            continue
        for target, col in columns:
            if col not in row:
                continue
            kind, value = parse_manual(row.get(col))
            if kind == "skip":
                continue
            if kind == "bad":
                errors.append(f"строка {i}, «{col}»: «{row.get(col)}» — не целое число рублей")
                continue
            if target != "base" and known[item_id]["cells"].get(target) is None:
                errors.append(f"строка {i}: {item_id} нет на площадке {PLATFORMS[target]}")
                continue
            changed += _set(db, user, item_id, target, value)
    audit.log(db, user.username, "sku_price_import", f"{changed} цен", f"ошибок {len(errors)}")
    db.commit()
    _import_flash(request, f"Цен изменено: {changed}. На площадки ничего не отправлено — «Рассчитать» на «Ценах».",
                  errors)
    return _back()

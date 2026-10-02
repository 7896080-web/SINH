"""Страница «Цены товаров»: коэффициент от базовой цены на АРТИКУЛ.

Работа идёт ПО ОДНОЙ ПЛОЩАДКЕ или ОДНОМУ КАБИНЕТУ («где»): выбрали, отобрали
артикулы, задали коэффициент — тут же видно новую цену и новую маржинальность
рядом с текущими, — и передали на площадку.

  * базовая цена = себестоимость 1С, $ × курс ЦБ — руками не правится;
  * цена = базовая × коэффициент, округление по правилу площадки;
  * коэффициент — на артикул, то есть сразу на все его размеры (и цвета). Строка
    по умолчанию — артикул; размеры раскрываются отдельным видом, коэффициент у
    них общий с артикулом;
  * коэффициент кабинета главнее коэффициента площадки, тот — умолчания из
    правила площадки;
  * маржинальность = к получению / себестоимость ₽, к получению = цена ×
    (1 − комиссия) — и, если на площадке стоит скидка продавца, ещё и с ней:
    покупатель платит цену со скидкой, и маржинальность без неё была бы неправдой.

Передать — это и есть подтверждение: цены встают в очередь отправки так же,
как подтверждённые на «Ценах», с теми же ограничителями. Ниже пола не уходит
никогда; изменение больше лимита шага — только с отдельной галочкой.
Пустая ячейка в файле ничего не меняет, «-» снимает коэффициент.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import audit, mapping, rates
from priceapp.database import get_db
from priceapp.deps import get_current_user
from priceapp.excel import xlsx_response
from priceapp.flash import flash
from priceapp.models import (ArticleCoef, OnecBarcode, OnecCost, PriceChange, PriceChangeStatus,
                             ProductPrice, User)
from priceapp.pages import render
from priceapp.platforms import PLATFORMS
from priceapp.pricing import (BLOCK_FLOOR, BLOCK_MAX_CHANGE, OPEN, SOURCE_LABELS, SRC_CABINET, SRC_MANUAL, _dec,
                              _ru, article_of, base_price, change_percent, coef_for, decide_for, load_inputs,
                              markup)
from priceapp.routers.prices import (BULK_LIMIT, CLEAR_CELL, ROWS_LIMIT, _accounts, _cell, _f, _import_flash,
                                     _num, _read_upload)
from priceapp.timeutils import now_utc

router = APIRouter()

FILTERS = {
    "": "все",
    "changes": "новая цена ≠ текущей",
    "floor": "ниже пола",
    "big_step": "изменение больше лимита",
    "own": "со своим коэффициентом",
    "default": "по умолчанию площадки",
    "no_cost": "нет себестоимости",
    "no_price": "цена не считается",
}
ACTIONS = {
    "set": "Коэффициент = число",
    "mult": "Умножить коэффициент на",
    "clear": "Снять (по умолчанию площадки)",
}
KEEP = ("scope", "art", "barcode", "color", "size", "flt", "mmin", "mmax", "by")
COEF_COL = "Коэффициент от базовой"
SCOPE_COL = "Где (код)"


# --- «где»: площадка целиком или один кабинет --------------------------------------

def scopes(db: Session) -> list[dict]:
    """Площадки с активными кабинетами, а у площадки с несколькими кабинетами
    (WB — три ИП) — ещё и каждый кабинет отдельно."""
    accs = _accounts(db)
    out = []
    for p in PLATFORMS:
        mine = [a for a in accs if a.platform == p]
        if not mine:
            continue
        out.append({"key": p, "platform": p, "accounts": mine, "account_id": None,
                    "label": PLATFORMS[p] + (f" — все кабинеты ({len(mine)})" if len(mine) > 1 else "")})
        if len(mine) > 1:
            for a in mine:
                out.append({"key": f"a{a.id}", "platform": p, "accounts": [a], "account_id": a.id,
                            "label": f"{PLATFORMS[p]}: {a.name}"})
    return out


def pick_scope(db: Session, key: str) -> dict | None:
    all_ = scopes(db)
    return next((s for s in all_ if s["key"] == key), all_[0] if all_ else None)


def _back(**params) -> RedirectResponse:
    q = urlencode({k: v for k, v in params.items() if v not in ("", None)})
    return RedirectResponse("/sku-prices" + (f"?{q}" if q else ""), status_code=303)


def _keep(src) -> dict:
    return {k: str(src.get(k) or "") for k in KEEP}


def _dec_or_none(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ".")) if text.strip() else None
    except InvalidOperation:
        return None


def _money(v) -> str:
    return f"{Decimal(str(v)):,.2f}".replace(",", " ").replace(".", ",")


def _coef(v) -> str:
    return f"{Decimal(str(v)):.2f}".replace(".", ",")


def span(values, money: bool = False) -> str:
    """«1299» или «1299–1499» — у размеров артикула числа бывают разные.
    Дробные (маржинальность) — всегда с двумя знаками: 1,90, а не 1,9."""
    vals = [v for v in values if v is not None]
    if not vals:
        return "—"
    lo, hi = min(vals), max(vals)
    f = _money if money else (_coef if isinstance(lo, Decimal) else str)
    return f(lo) if lo == hi else f"{f(lo)}–{f(hi)}"


# --- строки --------------------------------------------------------------------------

def sku_rows(db: Session, scope: dict) -> list[dict]:
    """По каждому SKU 1С, который есть хотя бы в одном кабинете «где»: базовая,
    коэффициент, и по каждому кабинету — новая цена и маржинальность против
    текущих. Считается ТЕМ ЖЕ `decide_for`, что и расчёт и отправка."""
    platform = scope["platform"]
    inp = load_inputs(db, platform)
    rule = inp.rule
    commission = rule.commission_percent
    rate = rates.current(db)
    usd = rate.usd_rub if rate else None
    per_acc = {a.id: mapping.account_items(db, a.id) for a in scope["accounts"]}
    item_ids = sorted({i for items in per_acc.values() for i in items})
    info: dict[str, OnecBarcode] = {}
    barcodes: dict[str, list[str]] = {}
    costs: dict[str, OnecCost] = {}
    for i in range(0, len(item_ids), 500):
        chunk = item_ids[i:i + 500]
        for b in db.query(OnecBarcode).filter(OnecBarcode.item_id.in_(chunk)):
            info.setdefault(b.item_id, b)
            barcodes.setdefault(b.item_id, []).append(b.barcode)
        for c in db.query(OnecCost).filter(OnecCost.item_id.in_(chunk)):
            costs[c.item_id] = c
    acc_ids = list(per_acc)
    pps = {(p.item_id, p.account_id): p
           for p in db.query(ProductPrice).filter(ProductPrice.account_id.in_(acc_ids))}
    out = []
    for item_id in item_ids:
        cost = costs.get(item_id)
        cost_usd = cost.cost_usd if cost else None
        base = base_price(cost_usd, usd)
        coef, src = coef_for(inp, item_id, scope["account_id"])
        art = article_of(inp.articles, item_id)
        cells = []
        for acc in scope["accounts"]:
            plat = per_acc[acc.id].get(item_id)
            if not plat:
                continue
            pp = pps.get((item_id, acc.id))
            d = decide_for(inp, item_id, acc.id, cost_usd, usd, pp)
            with_price = [p for p in plat if p.current_price]
            cur_row = max(with_price, key=lambda p: p.current_price) if with_price else None
            cur = cur_row.current_price if cur_row else None
            sale = (cur_row.current_sale_price or cur) if cur_row else None
            # Скидка продавца на площадке: покупатель платит цену со скидкой, и
            # маржинальность считается с неё — и по текущей, и по новой цене
            # (скидку мы не меняем, она остаётся той же долей).
            disc = (Decimal(1) - Decimal(sale) / Decimal(cur)) if cur and sale and sale < cur else Decimal(0)
            cur_margin = new_margin = None
            if base is not None and commission is not None:
                if sale:
                    cur_margin = markup(sale, base, commission)[1]
                if d.new_price:
                    new_margin = markup(Decimal(d.new_price) * (1 - disc), base, commission)[1]
            below_floor = (new_margin is not None and new_margin < _dec(rule.min_markup_coef)) or \
                d.block_reason == BLOCK_FLOOR
            # Баркод строки каталога ЭТОГО кабинета: отправка ищет позицию в его
            # каталоге, чужой баркод там не найдётся.
            cells.append({"account": acc, "barcode": plat[0].barcode, "decision": d, "price": d.new_price, "current": cur, "sale": sale,
                          "discount": int((disc * 100).quantize(Decimal("1"))) if disc else None,
                          "margin": new_margin, "cur_margin": cur_margin, "floor": below_floor,
                          "big_step": d.block_reason == BLOCK_MAX_CHANGE,
                          "last_sent": pp.last_sent_price if pp else None,
                          "own_cabinet": (art, acc.id) in inp.coefs})
        out.append({"item_id": item_id, "article": art, "sku": info.get(item_id),
                    "barcodes": barcodes.get(item_id, []), "plat_barcodes": [p.barcode for c in per_acc.values()
                                                                            for p in c.get(item_id, [])],
                    "cost_usd": cost_usd, "base": base, "coef": coef, "source": src, "cells": cells,
                    "note": next((c["decision"].note for c in cells if c["price"] is None), "")})
    return out


def _passes(r: dict, art: str, barcode: str, color: str, size: str) -> bool:
    s = r["sku"]
    if art and art not in r["article"].lower() and art not in (s.name.lower() if s else ""):
        return False
    if barcode and not any(barcode in b for b in (*r["barcodes"], *r["plat_barcodes"])):
        return False
    if color and color not in (s.color.lower() if s else ""):
        return False
    if size and size != (s.size.lower() if s else ""):
        return False
    return True


def summarize(rows: list[dict]) -> dict:
    """Сводка по набору SKU (артикул целиком или один размер)."""
    cells = [c for r in rows for c in r["cells"]]
    prices = [c["price"] for c in cells]
    cur = [c["current"] for c in cells]
    new_max = max((p for p in prices if p), default=None)
    cur_max = max((p for p in cur if p), default=None)
    sources = {r["source"] for r in rows}
    manual = any(c["decision"].source == SRC_MANUAL for c in cells)
    return {
        "base": span((r["base"] for r in rows), money=True),
        "cost": span((r["cost_usd"] for r in rows), money=True),
        "coef": rows[0]["coef"], "source": rows[0]["source"] if len(sources) == 1 else "",
        "price": span(prices), "current": span(cur),
        "margin": span(c["margin"] for c in cells), "cur_margin": span(c["cur_margin"] for c in cells),
        "min_margin": min((c["margin"] for c in cells if c["margin"] is not None), default=None),
        "discount": span(c["discount"] for c in cells),
        "pct": change_percent(cur_max, new_max) if new_max else None,
        "floor": any(c["floor"] for c in cells), "big_step": any(c["big_step"] for c in cells),
        "changes": any(c["price"] and c["price"] != c["current"] for c in cells),
        "no_cost": any(r["cost_usd"] is None for r in rows),
        "no_price": any(c["price"] is None for c in cells) or not cells,
        "manual": manual,
        "own_cabinets": sorted({c["account"].name for c in cells if c["own_cabinet"]}),
        "note": next((r["note"] for r in rows if r["note"]), ""),
    }


def _group_passes(g: dict, flt: str, lo, hi) -> bool:
    if flt == "changes" and not g["changes"]:
        return False
    if flt == "floor" and not g["floor"]:
        return False
    if flt == "big_step" and not g["big_step"]:
        return False
    if flt == "own" and g["source"] not in ("article", SRC_CABINET):
        return False
    if flt == "default" and g["source"] != "rule":
        return False
    if flt == "no_cost" and not g["no_cost"]:
        return False
    if flt == "no_price" and not g["no_price"]:
        return False
    m = g["min_margin"]
    if lo is not None and (m is None or m < lo):
        return False
    if hi is not None and (m is None or m > hi):
        return False
    return True


def build(db: Session, sc: dict, art: str = "", barcode: str = "", color: str = "", size: str = "",
          flt: str = "", mmin: str = "", mmax: str = "", by: str = "", scope: str = "") -> list[dict]:
    """Группы-артикулы в порядке артикула; у каждой — её SKU (размеры) и сводка.
    Отборы по тексту — по SKU, по состоянию — по артикулу целиком: коэффициент
    один на артикул, и отбор, разрезающий артикул, ставил бы его размерам
    разные решения в одной правке."""
    a_l, b_l, c_l, s_l = art.strip().lower(), barcode.strip(), color.strip().lower(), size.strip().lower()
    lo, hi = _dec_or_none(mmin), _dec_or_none(mmax)
    groups: dict[str, list[dict]] = {}
    for r in sku_rows(db, sc):
        if _passes(r, a_l, b_l, c_l, s_l):
            groups.setdefault(r["article"], []).append(r)
    out = []
    for article in sorted(groups):
        rows = sorted(groups[article], key=lambda r: ((r["sku"].color if r["sku"] else ""),
                                                      (r["sku"].size if r["sku"] else "")))
        g = summarize(rows)
        if not _group_passes(g, flt, lo, hi):
            continue
        s = rows[0]["sku"]
        g.update(article=article, name=s.name if s else "", rows=rows,
                 colors=sorted({r["sku"].color for r in rows if r["sku"] and r["sku"].color}),
                 sizes=[r["sku"].size for r in rows if r["sku"] and r["sku"].size],
                 sizes_detail=[dict(summarize([r]), row=r) for r in rows] if by == "sku" else [])
        out.append(g)
    return out


@router.get("/sku-prices")
def page(request: Request, scope: str = Query(""), art: str = Query(""), barcode: str = Query(""),
         color: str = Query(""), size: str = Query(""), flt: str = Query(""), mmin: str = Query(""),
         mmax: str = Query(""), by: str = Query(""), db: Session = Depends(get_db),
         user: User = Depends(get_current_user)):
    sc = pick_scope(db, scope)
    keep = {"scope": sc["key"] if sc else "", "art": art, "barcode": barcode, "color": color, "size": size,
            "flt": flt, "mmin": mmin, "mmax": mmax, "by": by}
    groups = build(db, sc, **keep) if sc else []
    rule = load_inputs(db, sc["platform"]).rule if sc else None
    db.commit()
    return render(request, "sku_prices.html", user, "sku_prices", groups=groups[:ROWS_LIMIT], total=len(groups),
                  skus=sum(len(g["rows"]) for g in groups), scopes=scopes(db), sc=sc, rule=rule,
                  filters=FILTERS, actions=ACTIONS, sources=SOURCE_LABELS, ru=_ru, rate=rates.current(db),
                  names=PLATFORMS, export_qs=urlencode(keep), **keep)


# --- коэффициенты --------------------------------------------------------------------

def set_coef(db: Session, user: User, article: str, platform: str, account_id: int,
             value: Decimal | None) -> bool:
    """Записать коэффициент артикула (account_id 0 — на всю площадку). None — снять."""
    row = (db.query(ArticleCoef).filter(ArticleCoef.article == article, ArticleCoef.platform == platform,
                                        ArticleCoef.account_id == account_id).first())
    if value is None:
        if row is None:
            return False
        db.delete(row)
        db.flush()
        return True
    if row is None:
        db.add(ArticleCoef(article=article, platform=platform, account_id=account_id, coef=value,
                           updated_by=user.username))
        db.flush()       # autoflush выключен: повтор артикула в файле иначе — второй INSERT
        return True
    if _dec(row.coef) == value:
        return False
    row.coef, row.updated_by = value, user.username
    return True


def _chosen(form, groups: list[dict]) -> list[dict]:
    if form.get("all_filtered") == "1":
        return groups
    wanted = set(str(v) for v in form.getlist("arts"))
    return [g for g in groups if g["article"] in wanted]


def _coef_value(raw) -> Decimal:
    return _num(raw, Decimal("0.1"), 100, False)


@router.post("/sku-prices/coef")
async def save_coef(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Коэффициент отмеченным артикулам (или всему отбору) в выбранном «где».
    Строка страницы — тот же путь с одним артикулом; пустое поле в строке —
    осознанное «снять»: человек стёр число."""
    form = await request.form()
    keep = _keep(form)
    sc = pick_scope(db, keep["scope"])
    if sc is None:
        flash(request, "Нет ни одного активного кабинета.", "warn")
        return _back(**keep)
    action = str(form.get("action") or "")
    row_art = str(form.get("row") or "")
    if row_art:
        action = "set" if _cell(form.get("value")) else "clear"
    if action not in ACTIONS:
        flash(request, "Выберите действие.", "warn")
        return _back(**keep)
    value = None
    if action != "clear":
        try:
            value = _coef_value(form.get("value"))
        except ValueError as e:
            flash(request, f"«{ACTIONS[action]}»: значение — {e}.", "warn")
            return _back(**keep)
    groups = build(db, sc, **keep)
    chosen = [g for g in groups if g["article"] == row_art] if row_art else _chosen(form, groups)
    if not chosen:
        flash(request, "Ничего не отмечено.", "warn")
        return _back(**keep)
    if len(chosen) > BULK_LIMIT:
        flash(request, f"В отборе {len(chosen)} артикулов — больше {BULK_LIMIT}. Сузьте отбор.", "warn")
        return _back(**keep)
    account_id = sc["account_id"] or 0
    changed, skipped = 0, 0
    for g in chosen:
        if action == "mult":
            if g["coef"] is None:
                skipped += 1
                continue
            new = (_dec(g["coef"]) * value).quantize(Decimal("0.001"))
        else:
            new = value
        changed += set_coef(db, user, g["article"], sc["platform"], account_id, new)
    audit.log(db, user.username, "article_coef", sc["label"][:100],
              f"{ACTIONS[action]} {value if value is not None else ''}; артикулов {len(chosen)}; "
              f"изменено {changed}; пропущено {skipped}")
    db.commit()
    msg = (f"{sc['label']}: «{ACTIONS[action]}»{' ' + _ru(value) if value is not None else ''} — "
           f"артикулов {len(chosen)}, изменено {changed}"
           + (f", пропущено {skipped} (коэффициент не задан — умножать нечего)" if skipped else "")
           + ". Новые цены и маржинальность — в таблице; на площадки ничего не отправлено.")
    flash(request, msg, "warn" if skipped else "ok")
    return _back(**keep)


# --- передача на площадки ------------------------------------------------------------

@router.post("/sku-prices/send")
async def send(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Передать новые цены отмеченных артикулов (или всего отбора) во все
    кабинеты «где». Это и есть подтверждение: цена встаёт в очередь отправки, как
    подтверждённая на «Ценах». Ниже пола — никогда; больше лимита шага — только с
    галочкой. Цена, которую площадка уже приняла от нас или которая там уже
    стоит, повторно не уходит."""
    form = await request.form()
    keep = _keep(form)
    sc = pick_scope(db, keep["scope"])
    if sc is None:
        return _back(**keep)
    if rates.current(db) is None:
        flash(request, "Курса доллара нет — базовую цену не посчитать. Страница «Курс $».", "error")
        return _back(**keep)
    confirm_large = form.get("confirm_large") == "1"
    chosen = _chosen(form, build(db, sc, **keep))
    if not chosen:
        flash(request, "Ничего не отмечено.", "warn")
        return _back(**keep)
    if len(chosen) > BULK_LIMIT:
        flash(request, f"В отборе {len(chosen)} артикулов — больше {BULK_LIMIT}. Сузьте отбор.", "warn")
        return _back(**keep)
    rate = rates.current(db)
    rule = load_inputs(db, sc["platform"]).rule
    now = now_utc()
    queued, same, floor, big, none = 0, 0, 0, 0, {}
    for g in chosen:
        for r in g["rows"]:
            for c in r["cells"]:
                d, acc = c["decision"], c["account"]
                if d.new_price is None:
                    none[d.note] = none.get(d.note, 0) + 1
                    continue
                if c["floor"]:
                    floor += 1
                    continue
                if d.block_reason == BLOCK_MAX_CHANGE and not confirm_large:
                    big += 1
                    continue
                if d.new_price == c["last_sent"] or (c["last_sent"] is None and d.new_price == c["current"]):
                    same += 1
                    continue
                for old in db.query(PriceChange).filter(
                        PriceChange.account_id == acc.id, PriceChange.item_id == r["item_id"],
                        PriceChange.is_test.is_(False),
                        PriceChange.status.in_(OPEN + (PriceChangeStatus.approved.value,))):
                    old.status = PriceChangeStatus.rejected.value
                    old.note = "вытеснено передачей со страницы «Цены товаров»"
                db.add(PriceChange(
                    item_id=r["item_id"], account_id=acc.id, barcode=c["barcode"],
                    cost_usd=r["cost_usd"], usd_rub=rate.usd_rub, cost_rub=d.cost_rub,
                    commission_percent=rule.commission_percent, old_price=c["last_sent"],
                    new_price=d.new_price, markup_rub=d.markup_rub, markup_coef=c["margin"], source=d.source,
                    status=PriceChangeStatus.approved.value, block_reason=d.block_reason,
                    note=(f"базовая {d.cost_rub} × {_ru(d.coef)}" if d.coef is not None else d.note)[:255] or None,
                    decided_by=user.username, decided_at=now))
                db.flush()
                queued += 1
    audit.log(db, user.username, "prices_sent_from_articles", sc["label"][:100],
              f"артикулов {len(chosen)}; в очередь {queued}; уже стоит {same}; ниже пола {floor}; "
              f"большой шаг без подтверждения {big}; не посчитано {none}")
    db.commit()
    parts = [f"{sc['label']}: передано на отправку {queued} цен по {len(chosen)} артикулам — уйдут в течение пары "
             "минут, итог — «Цены» → «Журнал»"]
    if same:
        parts.append(f"уже стоит на площадке — {same}")
    if floor:
        parts.append(f"ниже пола, не отправлено — {floor}")
    if big:
        parts.append(f"изменение больше лимита — {big} (отметьте «включая изменения больше лимита»)")
    if none:
        parts.append("цена не считается: " + "; ".join(f"{w} — {n}" for w, n in none.items()))
    flash(request, ". ".join(parts) + ".", "warn" if floor or big or none else "ok")
    return _back(**keep)


# --- Excel ---------------------------------------------------------------------------

HEADERS = [SCOPE_COL, "Где", "Артикул", "Наименование", "Цвета", "Размеры", "Себестоимость, $",
           "Базовая цена, ₽", COEF_COL, "Откуда коэффициент", "Новая цена, ₽", "Новая маржинальность",
           "Текущая цена, ₽", "Текущая маржинальность", "Скидка на площадке, %", "Изменение к текущей, %",
           "Ниже пола", "Больше лимита"]


@router.get("/sku-prices/export")
def export(scope: str = Query(""), art: str = Query(""), barcode: str = Query(""), color: str = Query(""),
           size: str = Query(""), flt: str = Query(""), mmin: str = Query(""), mmax: str = Query(""),
           by: str = Query(""), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    sc = pick_scope(db, scope)
    groups = build(db, sc, art=art, barcode=barcode, color=color, size=size, flt=flt, mmin=mmin,
                   mmax=mmax) if sc else []
    db.commit()
    data = []
    for g in groups:
        data.append([sc["key"], sc["label"], g["article"], g["name"], ", ".join(g["colors"]), ", ".join(g["sizes"]),
                     g["cost"], g["base"], _f(g["coef"]), SOURCE_LABELS.get(g["source"], ""),
                     g["price"], g["margin"], g["current"], g["cur_margin"], g["discount"],
                     round(g["pct"], 1) if g["pct"] is not None else None,
                     "да" if g["floor"] else "", "да" if g["big_step"] else ""])
    return xlsx_response(HEADERS, data, "коэффициенты_артикулов.xlsx")


@router.post("/sku-prices/import")
def import_file(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Читаются ТОЛЬКО «Где (код)», «Артикул» и «Коэффициент от базовой»;
    остальное справочное. Пустая ячейка ничего не меняет, «-» снимает."""
    rows = _read_upload(request, file, "Артикул")
    if rows is None:
        return _back()
    if COEF_COL not in rows[0] or SCOPE_COL not in rows[0]:
        flash(request, f"В файле нет колонок «{SCOPE_COL}» и «{COEF_COL}» — выгрузите файл с этой страницы.", "warn")
        return _back()
    by_key = {s["key"]: s for s in scopes(db)}
    known: dict[str, set[str]] = {}
    changed, errors = 0, []
    for i, row in enumerate(rows, start=2):
        art, key, raw = _cell(row.get("Артикул")), _cell(row.get(SCOPE_COL)), _cell(row.get(COEF_COL))
        if not art or raw == "":
            continue
        sc = by_key.get(key)
        if sc is None:
            errors.append(f"строка {i}: «{key}» — нет такой площадки или кабинета")
            continue
        if key not in known:
            known[key] = {g["article"] for g in build(db, sc)}
        if art not in known[key]:
            errors.append(f"строка {i}: артикула {art} нет в кабинетах «{sc['label']}»")
            continue
        if raw == CLEAR_CELL:
            value = None
        else:
            try:
                value = _coef_value(raw)
            except ValueError as e:
                errors.append(f"строка {i}: «{raw}» — {e}")
                continue
        changed += set_coef(db, user, art, sc["platform"], sc["account_id"] or 0, value)
    audit.log(db, user.username, "article_coef_import", f"{changed} коэфф.", f"ошибок {len(errors)}")
    db.commit()
    _import_flash(request, f"Коэффициентов изменено: {changed}. На площадки ничего не отправлено.", errors)
    return _back()

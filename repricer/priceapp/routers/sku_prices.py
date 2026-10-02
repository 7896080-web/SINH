"""Страница «Цены товаров»: наценка на АРТИКУЛ или на КАТЕГОРИЮ.

Работа идёт ПО ОДНОЙ ПЛОЩАДКЕ или ОДНОМУ КАБИНЕТУ («где»): выбрали, отобрали,
задали наценку — тут же видно новую цену и новую маржинальность рядом с
текущими, — и передали на площадку.

  * базовая цена = себестоимость 1С, $ × курс ЦБ — руками не правится;
  * наценка — коэффициентом от базовой (цена = базовая × k) или маржинальностью
    (цена такая, чтобы к получению / себестоимость = m при текущих комиссии и
    скидке продавца); задаётся на артикул (все его размеры) или на категорию
    площадки, в кабинете или на всю площадку (`pricing.target_for`);
  * вид страницы: по артикулам (размеры свёрнуты), с размерами, по категориям;
  * маржинальность — по цене, которую платит покупатель (со скидкой продавца),
    и комиссии товара (тариф площадки по категории + надбавка).

Передать — это и есть подтверждение: цены встают в очередь отправки так же,
как подтверждённые на «Ценах», с теми же ограничителями. Ниже пола не уходит
никогда; изменение больше лимита шага — только с отдельной галочкой.
Пустая ячейка в файле ничего не меняет, «-» снимает наценку.
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
from priceapp.models import (ArticleCoef, CategoryTarget, OnecBarcode, OnecCost, PriceChange, PriceChangeStatus,
                             ProductPrice, User)
from priceapp.pages import render
from priceapp.platforms import PLATFORMS
from priceapp.pricing import (BLOCK_FLOOR, BLOCK_MAX_CHANGE, KIND_COEF, KIND_LABELS, KIND_MARGIN, OPEN,
                              SOURCE_LABELS, SRC_CABINET, SRC_CAT_CABINET, SRC_CATEGORY, SRC_DEFAULT, SRC_MANUAL,
                              _dec, _ru, article_of, base_price, change_percent, decide_for, item_facts,
                              load_inputs, markup, target_for)
from priceapp.routers.prices import (BULK_LIMIT, CLEAR_CELL, ROWS_LIMIT, _accounts, _cell, _f, _import_flash,
                                     _num, _read_upload)
from priceapp.timeutils import now_utc

router = APIRouter()

FILTERS = {
    "": "все",
    "changes": "новая цена ≠ текущей",
    "floor": "ниже пола",
    "big_step": "изменение больше лимита",
    "own": "со своей наценкой артикула",
    "by_category": "наценка от категории",
    "default": "по умолчанию площадки",
    "no_cost": "нет себестоимости",
    "no_price": "цена не считается",
}
ACTIONS = {
    "set": "Задать",
    "mult": "Умножить текущую на",
    "clear": "Снять (взять уровнем выше)",
}
VIEWS = {"": "по артикулам", "sku": "с размерами", "category": "по категориям"}
KEEP = ("scope", "art", "barcode", "color", "size", "cat", "flt", "mmin", "mmax", "by")
NO_CATEGORY = "(без категории)"
SCOPE_COL = "Где (код)"
KIND_COL = "Вид наценки"
VALUE_COL = "Наценка"
KIND_BY_LABEL = {v: k for k, v in KIND_LABELS.items()} | {"коэффициент": KIND_COEF, "к": KIND_COEF,
                                                          "м": KIND_MARGIN, "coef": KIND_COEF,
                                                          "margin": KIND_MARGIN}


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
    Дробные (маржинальность, комиссия) — всегда с двумя знаками."""
    vals = [v for v in values if v is not None]
    if not vals:
        return "—"
    lo, hi = min(vals), max(vals)
    f = _money if money else (_coef if isinstance(lo, Decimal) else str)
    return f(lo) if lo == hi else f"{f(lo)}–{f(hi)}"


def target_text(kind: str | None, value) -> str:
    if value is None:
        return "—"
    return ("м " if kind == KIND_MARGIN else "× ") + _ru(value)


# --- строки --------------------------------------------------------------------------

def sku_rows(db: Session, scope: dict) -> list[dict]:
    """По каждому SKU 1С, который есть хотя бы в одном кабинете «где»: базовая,
    наценка, и по каждому кабинету — новая цена и маржинальность против
    текущих. Считается ТЕМ ЖЕ `decide_for`, что и расчёт и отправка."""
    platform = scope["platform"]
    inp = load_inputs(db, platform)
    rule = inp.rule
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
    pps = {(p.item_id, p.account_id): p
           for p in db.query(ProductPrice).filter(ProductPrice.account_id.in_(list(per_acc)))}
    # Последнее решение по паре товар+кабинет, которое что-то значит для площадки:
    # в очереди, ушло, не принято или заблокировано при отправке.
    last_change: dict[tuple, PriceChange] = {}
    for ch in (db.query(PriceChange)
               .filter(PriceChange.account_id.in_(list(per_acc)), PriceChange.is_test.is_(False),
                       PriceChange.status.in_(("approved", "sent", "error", "blocked")))
               .order_by(PriceChange.id.desc())):
        last_change.setdefault((ch.item_id, ch.account_id), ch)
    out = []
    for item_id in item_ids:
        cost = costs.get(item_id)
        cost_usd = cost.cost_usd if cost else None
        base = base_price(cost_usd, usd)
        all_rows = [p for items in per_acc.values() for p in items.get(item_id, [])]
        category = item_facts(rule, all_rows).category
        target = target_for(inp, item_id, scope["account_id"], category)
        art = article_of(inp.articles, item_id)
        cells = []
        for acc in scope["accounts"]:
            plat = per_acc[acc.id].get(item_id)
            if not plat:
                continue
            pp = pps.get((item_id, acc.id))
            facts = item_facts(rule, plat)
            d = decide_for(inp, item_id, acc.id, cost_usd, usd, pp, plat)
            cur_margin = None
            if base is not None and facts.commission is not None and facts.sale:
                cur_margin = markup(facts.sale, base, facts.commission)[1]
            # Баркод строки каталога ЭТОГО кабинета: отправка ищет позицию в его
            # каталоге, чужой баркод там не найдётся.
            cells.append({"account": acc, "barcode": plat[0].barcode, "decision": d, "price": d.new_price,
                          "current": facts.current, "sale": facts.sale,
                          "discount": int((facts.discount * 100).quantize(Decimal("1"))) if facts.discount else None,
                          "commission": facts.commission, "tariff": facts.tariff,
                          "margin": d.markup_coef, "cur_margin": cur_margin,
                          "floor": d.block_reason == BLOCK_FLOOR,
                          "big_step": d.block_reason == BLOCK_MAX_CHANGE,
                          "last_sent": pp.last_sent_price if pp else None,
                          "manual_price": pp.manual_price if pp else None,
                          "last_change": last_change.get((item_id, acc.id)),
                          "own_cabinet": (art, acc.id) in inp.coefs or (category, acc.id) in inp.categories})
        out.append({"item_id": item_id, "article": art, "sku": info.get(item_id), "category": category,
                    "barcodes": barcodes.get(item_id, []), "plat_barcodes": [p.barcode for p in all_rows],
                    "cost_usd": cost_usd, "base": base,
                    "kind": target.kind if target else None, "coef": target.value if target else None,
                    "source": target.source if target else SRC_DEFAULT, "cells": cells,
                    "note": next((c["decision"].note for c in cells if c["price"] is None), "")})
    return out


def _passes(r: dict, art: str, barcode: str, color: str, size: str, cat: str) -> bool:
    s = r["sku"]
    if art and art not in r["article"].lower() and art not in (s.name.lower() if s else ""):
        return False
    if barcode and not any(barcode in b for b in (*r["barcodes"], *r["plat_barcodes"])):
        return False
    if color and color not in (s.color.lower() if s else ""):
        return False
    if size and size != (s.size.lower() if s else ""):
        return False
    if cat and cat != (r["category"] or NO_CATEGORY):
        return False
    return True


def summarize(rows: list[dict]) -> dict:
    """Сводка по набору SKU (артикул, размер или категория целиком)."""
    cells = [c for r in rows for c in r["cells"]]
    prices = [c["price"] for c in cells]
    cur = [c["current"] for c in cells]
    new_max = max((p for p in prices if p), default=None)
    cur_max = max((p for p in cur if p), default=None)
    sources = {r["source"] for r in rows}
    return {
        "base": span((r["base"] for r in rows), money=True),
        "cost": span((r["cost_usd"] for r in rows), money=True),
        "coef": rows[0]["coef"], "kind": rows[0]["kind"],
        "source": rows[0]["source"] if len(sources) == 1 else "",
        "price": span(prices), "current": span(cur),
        "margin": span(c["margin"] for c in cells), "cur_margin": span(c["cur_margin"] for c in cells),
        "min_margin": min((c["margin"] for c in cells if c["margin"] is not None), default=None),
        "commission": span(c["commission"] for c in cells),
        "tariff_known": any(c["tariff"] is not None for c in cells),
        "discount": span(c["discount"] for c in cells),
        "pct": change_percent(cur_max, new_max) if new_max else None,
        "floor": any(c["floor"] for c in cells), "big_step": any(c["big_step"] for c in cells),
        "changes": any(c["price"] and c["price"] != c["current"] for c in cells),
        "no_cost": any(r["cost_usd"] is None for r in rows),
        "no_price": any(c["price"] is None for c in cells) or not cells,
        "manual": any(c["decision"].source == SRC_MANUAL for c in cells),
        "manual_prices": span(c["manual_price"] for c in cells),
        "last": last_state([c["last_change"] for c in cells if c["last_change"]]),
        "own_cabinets": sorted({c["account"].name for c in cells if c["own_cabinet"]}),
        "note": next((r["note"] for r in rows if r["note"]), ""),
    }


LAST_LABELS = {"approved": "в очереди", "sent": "ушло", "error": "не принято", "blocked": "не ушло: пол"}


def last_state(changes: list) -> dict | None:
    """Сводка последних решений по строке: худшее состояние, цены и время."""
    if not changes:
        return None
    order = ["error", "blocked", "approved", "sent"]
    worst = min(changes, key=lambda c: order.index(c.status))
    same = [c for c in changes if c.status == worst.status]
    when = max((c.sent_at or c.decided_at or c.created_at) for c in same)
    from datetime import timezone
    local = when.replace(tzinfo=timezone.utc).astimezone().strftime("%d.%m %H:%M") if when else ""
    return {"status": worst.status, "label": LAST_LABELS[worst.status],
            "price": span(c.new_price for c in same), "when": local,
            "detail": (worst.last_error or worst.note or "")[:200]}


def _group_passes(g: dict, flt: str, lo, hi) -> bool:
    if flt == "changes" and not g["changes"]:
        return False
    if flt == "floor" and not g["floor"]:
        return False
    if flt == "big_step" and not g["big_step"]:
        return False
    if flt == "own" and g["source"] not in ("article", SRC_CABINET):
        return False
    if flt == "by_category" and g["source"] not in (SRC_CATEGORY, SRC_CAT_CABINET):
        return False
    if flt == "default" and g["source"] != SRC_DEFAULT:
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


def category_target(db: Session, platform: str, account_id: int | None, category: str):
    """Наценка, заданная на САМУ категорию «где» (без наследования): (kind, value) или None."""
    q = db.query(CategoryTarget).filter(CategoryTarget.platform == platform, CategoryTarget.category == category,
                                        CategoryTarget.account_id == (account_id or 0))
    row = q.first()
    return (row.kind, _dec(row.value)) if row else None


def build(db: Session, sc: dict, art: str = "", barcode: str = "", color: str = "", size: str = "",
          cat: str = "", flt: str = "", mmin: str = "", mmax: str = "", by: str = "",
          scope: str = "") -> list[dict]:
    """Группы в порядке ключа: артикулы (с размерами внутри) или категории. Отборы
    по тексту — по SKU, по состоянию — по группе целиком: наценка одна на группу,
    и отбор, разрезающий её, ставил бы её строкам разные решения в одной правке."""
    a_l, b_l, c_l, s_l = art.strip().lower(), barcode.strip(), color.strip().lower(), size.strip().lower()
    lo, hi = _dec_or_none(mmin), _dec_or_none(mmax)
    by_category = by == "category"
    groups: dict[str, list[dict]] = {}
    for r in sku_rows(db, sc):
        if _passes(r, a_l, b_l, c_l, s_l, cat):
            key = (r["category"] or NO_CATEGORY) if by_category else r["article"]
            groups.setdefault(key, []).append(r)
    out = []
    for key in sorted(groups):
        rows = sorted(groups[key], key=lambda r: (r["article"], (r["sku"].color if r["sku"] else ""),
                                                  (r["sku"].size if r["sku"] else "")))
        g = summarize(rows)
        if not _group_passes(g, flt, lo, hi):
            continue
        s = rows[0]["sku"]
        g.update(key=key, article=key, rows=rows, level="category" if by_category else "article",
                 category=rows[0]["category"],
                 name=s.name if s and not by_category else "",
                 colors=sorted({r["sku"].color for r in rows if r["sku"] and r["sku"].color}),
                 sizes=[r["sku"].size for r in rows if r["sku"] and r["sku"].size],
                 articles=sorted({r["article"] for r in rows}),
                 sizes_detail=[dict(summarize([r]), row=r) for r in rows] if by == "sku" else [])
        if by_category:
            own = category_target(db, sc["platform"], sc["account_id"], key) if key != NO_CATEGORY else None
            g["own_kind"], g["own_value"] = own if own else (None, None)
        out.append(g)
    return out


def categories_in(db: Session, sc: dict) -> list[str]:
    """Категории, которые есть в каталогах кабинетов «где», — для отбора."""
    from priceapp.models import PlatformItem
    ids = [a.id for a in sc["accounts"]]
    got = {c for (c,) in db.query(PlatformItem.category).filter(PlatformItem.account_id.in_(ids)).distinct()}
    return sorted(c or NO_CATEGORY for c in got)


@router.get("/sku-prices")
def page(request: Request, scope: str = Query(""), art: str = Query(""), barcode: str = Query(""),
         color: str = Query(""), size: str = Query(""), cat: str = Query(""), flt: str = Query(""),
         mmin: str = Query(""), mmax: str = Query(""), by: str = Query(""), db: Session = Depends(get_db),
         user: User = Depends(get_current_user)):
    sc = pick_scope(db, scope)
    by = by if by in VIEWS else ""
    keep = {"scope": sc["key"] if sc else "", "art": art, "barcode": barcode, "color": color, "size": size,
            "cat": cat, "flt": flt, "mmin": mmin, "mmax": mmax, "by": by}
    groups = build(db, sc, **keep) if sc else []
    rule = load_inputs(db, sc["platform"]).rule if sc else None
    cats = categories_in(db, sc) if sc else []
    db.commit()
    return render(request, "sku_prices.html", user, "sku_prices", groups=groups[:ROWS_LIMIT], total=len(groups),
                  skus=sum(len(g["rows"]) for g in groups), scopes=scopes(db), sc=sc, rule=rule,
                  filters=FILTERS, actions=ACTIONS, views=VIEWS, kinds=KIND_LABELS, sources=SOURCE_LABELS,
                  categories=cats, ru=_ru, target_text=target_text, rate=rates.current(db),
                  names=PLATFORMS, export_qs=urlencode(keep), KIND_MARGIN=KIND_MARGIN, **keep)


# --- наценки -------------------------------------------------------------------------

def _upsert(db: Session, row, make, kind: str, value: Decimal | None, user: User, attr: str) -> bool:
    if value is None:
        if row is None:
            return False
        db.delete(row)
        db.flush()
        return True
    if row is None:
        db.add(make())
        db.flush()       # autoflush выключен: повтор в файле иначе — второй INSERT
        return True
    if _dec(getattr(row, attr)) == value and (row.kind or KIND_COEF) == kind:
        return False
    setattr(row, attr, value)
    row.kind, row.updated_by = kind, user.username
    return True


def set_coef(db: Session, user: User, article: str, platform: str, account_id: int,
             value: Decimal | None, kind: str = KIND_COEF) -> bool:
    """Наценка артикула (account_id 0 — на всю площадку). None — снять."""
    row = (db.query(ArticleCoef).filter(ArticleCoef.article == article, ArticleCoef.platform == platform,
                                        ArticleCoef.account_id == account_id).first())
    return _upsert(db, row, lambda: ArticleCoef(article=article, platform=platform, account_id=account_id,
                                                coef=value, kind=kind, updated_by=user.username),
                   kind, value, user, "coef")


def set_category(db: Session, user: User, category: str, platform: str, account_id: int,
                 value: Decimal | None, kind: str = KIND_MARGIN) -> bool:
    """Наценка категории (account_id 0 — на всю площадку). None — снять."""
    row = (db.query(CategoryTarget).filter(CategoryTarget.category == category, CategoryTarget.platform == platform,
                                           CategoryTarget.account_id == account_id).first())
    return _upsert(db, row, lambda: CategoryTarget(category=category, platform=platform, account_id=account_id,
                                                   value=value, kind=kind, updated_by=user.username),
                   kind, value, user, "value")


def _chosen(form, groups: list[dict]) -> list[dict]:
    if form.get("all_filtered") == "1":
        return groups
    wanted = set(str(v) for v in form.getlist("arts"))
    return [g for g in groups if g["key"] in wanted]


def _value(raw, kind: str) -> Decimal:
    # Маржинальность ниже 0,5 — почти наверняка опечатка (0,25 вместо 2,5): цена
    # вышла бы вдвое ниже себестоимости. Пол её всё равно не пустил бы, но лучше
    # отказать сразу, чем показать сотню «ниже пола».
    return _num(raw, Decimal("0.5") if kind == KIND_MARGIN else Decimal("0.1"), 100, False)


@router.post("/sku-prices/coef")
async def save_coef(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Наценка отмеченным артикулам или категориям (или всему отбору) в выбранном
    «где». Строка страницы — тот же путь с одной группой; пустое поле в строке —
    осознанное «снять»: человек стёр число."""
    form = await request.form()
    keep = _keep(form)
    sc = pick_scope(db, keep["scope"])
    if sc is None:
        flash(request, "Нет ни одного активного кабинета.", "warn")
        return _back(**keep)
    action = str(form.get("action") or "")
    kind = str(form.get("kind") or KIND_COEF)
    row_key = str(form.get("row") or "")
    if row_key:
        action = "set" if _cell(form.get("value")) else "clear"
    if action not in ACTIONS or kind not in KIND_LABELS:
        flash(request, "Выберите действие и вид наценки.", "warn")
        return _back(**keep)
    value = None
    if action != "clear":
        try:
            value = _num(form.get("value"), Decimal("0.1"), 100, False) if action == "mult" else _value(
                form.get("value"), kind)
        except ValueError as e:
            flash(request, f"«{ACTIONS[action]}»: значение — {e}.", "warn")
            return _back(**keep)
    groups = build(db, sc, **keep)
    chosen = [g for g in groups if g["key"] == row_key] if row_key else _chosen(form, groups)
    if not chosen:
        flash(request, "Ничего не отмечено.", "warn")
        return _back(**keep)
    if len(chosen) > BULK_LIMIT:
        flash(request, f"В отборе {len(chosen)} строк — больше {BULK_LIMIT}. Сузьте отбор.", "warn")
        return _back(**keep)
    account_id = sc["account_id"] or 0
    level_category = keep["by"] == "category"
    changed, skipped = 0, 0
    for g in chosen:
        if level_category and g["key"] == NO_CATEGORY:
            skipped += 1
            continue
        new_kind, new = kind, value
        if action == "mult":
            cur_kind, cur = (g.get("own_kind"), g.get("own_value")) if level_category else (g["kind"], g["coef"])
            if cur is None:
                skipped += 1
                continue
            new_kind, new = cur_kind, (_dec(cur) * value).quantize(Decimal("0.001"))
        if level_category:
            changed += set_category(db, user, g["key"], sc["platform"], account_id, new, new_kind)
        else:
            changed += set_coef(db, user, g["key"], sc["platform"], account_id, new, new_kind)
    what = "категорий" if level_category else "артикулов"
    shown = f" {KIND_LABELS[kind]} {_ru(value)}" if value is not None and action == "set" else (
        f" × {_ru(value)}" if value is not None else "")
    audit.log(db, user.username, "category_target" if level_category else "article_coef", sc["label"][:100],
              f"{ACTIONS[action]}{shown}; {what} {len(chosen)}; изменено {changed}; пропущено {skipped}")
    db.commit()
    msg = (f"{sc['label']}: «{ACTIONS[action]}»{shown} — {what} {len(chosen)}, изменено {changed}"
           + (f", пропущено {skipped} (наценка не задана — умножать нечего, или строка без категории)"
              if skipped else "")
           + ". Новые цены и маржинальность — в таблице; на площадки ничего не отправлено.")
    flash(request, msg, "warn" if skipped else "ok")
    return _back(**keep)


@router.post("/sku-prices/manual-clear")
async def manual_clear(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Снять ручные цены кабинетов у строки — тогда действует наценка. Ручная цена
    задаётся на «Правила и журнал» → «Текущие и ручные цены», а побеждает наценку:
    без этой кнопки оператор задавал наценку и не понимал, почему уходит другое."""
    form = await request.form()
    keep = _keep(form)
    sc = pick_scope(db, keep["scope"])
    key = str(form.get("row") or "")
    groups = [g for g in build(db, sc, **keep) if g["key"] == key] if sc else []
    n = 0
    for g in groups:
        for r in g["rows"]:
            for c in r["cells"]:
                pp = db.query(ProductPrice).filter(ProductPrice.item_id == r["item_id"],
                                                   ProductPrice.account_id == c["account"].id).first()
                if pp is not None and pp.manual_price is not None:
                    pp.manual_price = None
                    n += 1
    audit.log(db, user.username, "manual_price_cleared", key[:100], f"{sc['label'] if sc else ''}: снято {n}")
    db.commit()
    flash(request, f"«{key}»: ручных цен снято — {n}. Теперь действует наценка; на площадки ничего не отправлено.",
          "ok" if n else "warn")
    return _back(**keep)


# --- передача на площадки ------------------------------------------------------------

@router.post("/sku-prices/send")
async def send(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Передать новые цены отмеченных строк (или всего отбора) во все кабинеты
    «где». Это и есть подтверждение: цена встаёт в очередь отправки, как
    подтверждённая на «Ценах». Ниже пола — никогда; больше лимита шага — только с
    галочкой. Цена, которую площадка уже приняла от нас или которая там уже
    стоит, повторно не уходит."""
    form = await request.form()
    keep = _keep(form)
    sc = pick_scope(db, keep["scope"])
    if sc is None:
        return _back(**keep)
    rate = rates.current(db)
    if rate is None:
        flash(request, "Курса доллара нет — базовую цену не посчитать. Страница «Курс $».", "error")
        return _back(**keep)
    confirm_large = form.get("confirm_large") == "1"
    chosen = _chosen(form, build(db, sc, **keep))
    if not chosen:
        flash(request, "Ничего не отмечено.", "warn")
        return _back(**keep)
    if len(chosen) > BULK_LIMIT:
        flash(request, f"В отборе {len(chosen)} строк — больше {BULK_LIMIT}. Сузьте отбор.", "warn")
        return _back(**keep)
    now = now_utc()
    queued, same, floor, big, none, superseded = 0, 0, 0, 0, {}, 0
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
                    superseded += 1
                how = (f"маржинальность {_ru(d.coef)}" if d.kind == KIND_MARGIN else f"базовая {d.cost_rub} × "
                       f"{_ru(d.coef)}") if d.coef is not None else d.note
                db.add(PriceChange(
                    item_id=r["item_id"], account_id=acc.id, barcode=c["barcode"],
                    cost_usd=r["cost_usd"], usd_rub=rate.usd_rub, cost_rub=d.cost_rub,
                    commission_percent=d.commission, old_price=c["last_sent"],
                    new_price=d.new_price, markup_rub=d.markup_rub, markup_coef=d.markup_coef, source=d.source,
                    status=PriceChangeStatus.approved.value, block_reason=d.block_reason,
                    note=(how or "")[:255] or None, decided_by=user.username, decided_at=now))
                db.flush()
                queued += 1
    audit.log(db, user.username, "prices_sent_from_articles", sc["label"][:100],
              f"строк {len(chosen)}; в очередь {queued}; уже стоит {same}; ниже пола {floor}; "
              f"большой шаг без подтверждения {big}; не посчитано {none}")
    db.commit()
    what = "категориям" if keep["by"] == "category" else "артикулам"
    parts = [f"{sc['label']}: передано на отправку {queued} цен по {len(chosen)} {what} — уйдут в течение пары "
             "минут, итог — «Цены» → «Журнал»"]
    if same:
        parts.append(f"уже стоит на площадке — {same}")
    if superseded:
        parts.append(f"прежних предложений и подтверждений по этим товарам снято — {superseded} (ушло одно решение, это)")
    if floor:
        parts.append(f"ниже пола, не отправлено — {floor}")
    if big:
        parts.append(f"изменение больше лимита — {big} (отметьте «включая изменения больше лимита»)")
    if none:
        parts.append("цена не считается: " + "; ".join(f"{w} — {n}" for w, n in none.items()))
    flash(request, ". ".join(parts) + ".", "warn" if floor or big or none else "ok")
    return _back(**keep)


# --- Excel ---------------------------------------------------------------------------
#
# Один файл на вид страницы: по артикулам (с размерами — размеры отдельными
# строками, справочно) или по категориям. Читаются ТОЛЬКО «Где (код)», ключ
# («Артикул» или «Категория»), «Вид наценки» и «Наценка»; остальное справочное.
# Пустая «Наценка» ничего не меняет, «-» снимает.

TAIL = ["Новая цена, ₽", "Новая маржинальность", "Текущая цена, ₽", "Текущая маржинальность",
        "Комиссия, %", "Скидка на площадке, %", "Изменение к текущей, %", "Ниже пола", "Больше лимита"]


def _tail(g: dict) -> list:
    return [g["price"], g["margin"], g["current"], g["cur_margin"], g["commission"], g["discount"],
            round(g["pct"], 1) if g["pct"] is not None else None, "да" if g["floor"] else "",
            "да" if g["big_step"] else ""]


@router.get("/sku-prices/export")
def export(scope: str = Query(""), art: str = Query(""), barcode: str = Query(""), color: str = Query(""),
           size: str = Query(""), cat: str = Query(""), flt: str = Query(""), mmin: str = Query(""),
           mmax: str = Query(""), by: str = Query(""), db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    """Выгружается РОВНО то, что отобрано на странице: те же «где», отборы и вид."""
    sc = pick_scope(db, scope)
    keep = dict(art=art, barcode=barcode, color=color, size=size, cat=cat, flt=flt, mmin=mmin, mmax=mmax, by=by)
    groups = build(db, sc, **keep) if sc else []
    db.commit()
    data = []
    if by == "category":
        headers = [SCOPE_COL, "Где", "Категория", "Артикулов", "Размеров", KIND_COL, VALUE_COL,
                   "Действует (с учётом наследования)", *TAIL]
        for g in groups:
            data.append([sc["key"], sc["label"], g["key"], len(g["articles"]), len(g["rows"]),
                         KIND_LABELS.get(g["own_kind"], ""), _f(g["own_value"]),
                         f"{target_text(g['kind'], g['coef'])} ({SOURCE_LABELS.get(g['source'], 'разные')})",
                         *_tail(g)])
        return xlsx_response(headers, data, "наценки_категорий.xlsx")
    headers = [SCOPE_COL, "Где", "Артикул", "Размер", "Наименование", "Категория", "Цвета", "Размеры",
               "Себестоимость, $", "Базовая цена, ₽", KIND_COL, VALUE_COL, "Откуда наценка", *TAIL]
    for g in groups:
        own = g["source"] in ("article", SRC_CABINET)
        data.append([sc["key"], sc["label"], g["key"], "", g["name"], g["category"], ", ".join(g["colors"]),
                     ", ".join(g["sizes"]), g["cost"], g["base"],
                     KIND_LABELS.get(g["kind"], "") if own else "", _f(g["coef"]) if own else None,
                     f"{target_text(g['kind'], g['coef'])} ({SOURCE_LABELS.get(g['source'], 'разные')})",
                     *_tail(g)])
        for d in g["sizes_detail"]:
            s = d["row"]["sku"]
            data.append([sc["key"], sc["label"], g["key"], f"{s.color} {s.size}".strip() if s else d["row"]["item_id"],
                         "", "", "", "", d["cost"], d["base"], "", None, "", *_tail(d)])
    return xlsx_response(headers, data, "наценки_артикулов.xlsx")


@router.post("/sku-prices/import")
def import_file(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    rows = _read_upload(request, file, SCOPE_COL)
    if rows is None:
        return _back()
    head = rows[0]
    level_category = "Категория" in head and "Артикул" not in head
    key_col = "Категория" if level_category else "Артикул"
    if key_col not in head or VALUE_COL not in head:
        flash(request, f"В файле нет колонок «{key_col}» и «{VALUE_COL}» — выгрузите файл с этой страницы.", "warn")
        return _back()
    by_key = {s["key"]: s for s in scopes(db)}
    known: dict[str, set[str]] = {}
    changed, errors = 0, []
    for i, row in enumerate(rows, start=2):
        key, scope_key, raw = _cell(row.get(key_col)), _cell(row.get(SCOPE_COL)), _cell(row.get(VALUE_COL))
        if not key or raw == "" or (not level_category and _cell(row.get("Размер"))):
            continue            # пустая наценка ничего не меняет; строки размеров — справочные
        sc = by_key.get(scope_key)
        if sc is None:
            errors.append(f"строка {i}: «{scope_key}» — нет такой площадки или кабинета")
            continue
        if scope_key not in known:
            known[scope_key] = {g["key"] for g in build(db, sc, by="category" if level_category else "")}
        if key not in known[scope_key] or key == NO_CATEGORY:
            errors.append(f"строка {i}: {'категории' if level_category else 'артикула'} «{key}» нет в кабинетах "
                          f"«{sc['label']}»")
            continue
        kind_raw = _cell(row.get(KIND_COL)).lower()
        kind = KIND_BY_LABEL.get(kind_raw, KIND_MARGIN if level_category else KIND_COEF) if kind_raw else (
            KIND_MARGIN if level_category else KIND_COEF)
        if kind_raw and kind_raw not in KIND_BY_LABEL:
            errors.append(f"строка {i}: вид наценки «{kind_raw}» — нужно «коэффициент от базовой» или «маржинальность»")
            continue
        if raw == CLEAR_CELL:
            value = None
        else:
            try:
                value = _value(raw, kind)
            except ValueError as e:
                errors.append(f"строка {i}: «{raw}» — {e}")
                continue
        setter = set_category if level_category else set_coef
        changed += setter(db, user, key, sc["platform"], sc["account_id"] or 0, value, kind)
    audit.log(db, user.username, "category_target_import" if level_category else "article_coef_import",
              f"{changed} наценок", f"ошибок {len(errors)}")
    db.commit()
    _import_flash(request, f"Наценок изменено: {changed}. На площадки ничего не отправлено.", errors)
    return _back(by="category" if level_category else "")

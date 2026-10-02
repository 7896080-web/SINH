"""Страница «Цены».

Вкладки:
  * «Правила» — ПО ПЛОЩАДКЕ, одно на все её кабинеты (у WB три ИП, условия у них
    одни): комиссия, коэффициент наценки (2 = +100%), минимальный коэффициент,
    округление, лимит шага — или цена от другой площадки с коэффициентом;
  * «Предложения» — результат расчёта: подтвердить / отклонить;
  * «Товары» — экономика по SKU кабинета: себестоимость, ТЕКУЩАЯ цена площадки и
    наценка по ней, расчётная цена, ручная цена; отбор и массовая правка;
  * «Журнал» — что ушло на площадки и что нет.

На каждой вкладке — выгрузка в Excel; импорт — там, где файлом есть что
поменять (правила, решения по предложениям, ручные цены). Журнал — история, его
не импортируют.

Ничего не уходит на площадку без подтверждения оператора. Пустая ячейка в файле
НИЧЕГО НЕ МЕНЯЕТ; снять значение — «-» в ячейке.
"""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import accounts as acc_mod, audit, mapping, overview, platforms, rates, settings
from priceapp.database import get_db
from priceapp.deps import get_current_user
from priceapp.excel import ExcelReadError, read_xlsx_rows, xlsx_response
from priceapp.flash import flash
from priceapp.models import (Account, ApiCredential, OnecBarcode, OnecCost, PlatformRule,
                             PriceChange, PriceChangeStatus, ProductPrice, SavedFilter, User)
from priceapp.pages import render
from priceapp.platforms import PLATFORMS
from priceapp.pricing import (BLOCK_FLOOR, BLOCK_LABELS, BLOCK_MAX_CHANGE, OPEN, _ru, approve,
                              change_percent, decide, get_rule, markup, payout, preview_rule,
                              propose_price, recalculate_account, round_price, rules_for)
from priceapp.timeutils import now_utc

router = APIRouter()

ROWS_LIMIT = 500
BULK_LIMIT = 20000
VIEWS = ("rules", "proposals", "products", "compare", "log")
CLEAR_CELL = "-"
STATUS_LABELS = {
    "proposed": "ждёт решения", "blocked": "заблокировано", "approved": "подтверждено, ждёт отправки",
    "sent": "отправлено", "error": "площадка не приняла", "rejected": "отклонено",
}
SOURCE_LABELS = {"rule": "по правилу", "manual": "ручная", "base": "от базовой площадки",
                 "rollback": "возврат прежней цены"}
PRICE_STATUS_LABELS = {"QUARANTINE": "на карантине у площадки", "ERROR": "площадка: ошибка цены",
                       "PROCESSING": "площадка обновляет цену"}
# (поле, подпись, мин, макс)
RULE_FIELDS = [
    ("markup_coef", "Коэффициент наценки (2 = +100%)", 1, 100),
    ("min_markup_coef", "Мин. коэффициент (пол)", 0, 100),
    ("round_step", "Округлять вверх до, ₽", 1, 10000),
    ("round_minus", "Окончание: минус, ₽", 0, 9999),
    ("max_change_percent", "Макс. изменение за раз, %", 0, 1000),
]
INT_FIELDS = ("round_step", "round_minus")
RULE_KEYS = ["commission_percent", *(n for n, *_ in RULE_FIELDS), "base_platform", "base_coef"]

# Отборы на «Товарах»: ключ -> подпись.
PRODUCT_FILTERS = {
    "": "все",
    "below_floor_now": "текущая цена ниже пола",
    "differs": "текущая ≠ расчётной",
    "no_current": "нет текущей цены",
    "no_cost": "нет себестоимости",
    "manual": "с ручной ценой",
    "platform_status": "карантин или ошибка у площадки",
    "below_platform_min": "расчётная ниже минимальной площадки",
}
BULK_ACTIONS = {
    "set_manual": "Ручная цена = число",
    "manual_from_current": "Ручная цена = текущая × коэффициент",
    "manual_from_calc": "Ручная цена = расчётная × коэффициент",
    "clear_manual": "Снять ручную цену",
}
KEEP = ("q", "flt", "coef_min", "coef_max")


# --- общее ---------------------------------------------------------------------------

def _accounts(db: Session) -> list[Account]:
    return list(db.query(Account).filter(Account.is_active.is_(True))
                .order_by(Account.platform, Account.name))


def label(a: Account) -> str:
    return f"{a.name} ({PLATFORMS.get(a.platform, a.platform)})"


def _platforms(accs) -> list[str]:
    present = {a.platform for a in accs}
    return [p for p in PLATFORMS if p in present]


def _back(view: str, **params) -> RedirectResponse:
    q = urlencode({"view": view, **{k: v for k, v in params.items() if v not in ("", None)}})
    return RedirectResponse(f"/prices?{q}", status_code=303)


def _pick(accounts, account_id: str):
    if (account_id or "").isdigit():
        for a in accounts:
            if a.id == int(account_id):
                return a
    return accounts[0] if accounts else None


def _sku_info(db: Session, item_ids) -> dict:
    out = {}
    ids = list(item_ids)
    for i in range(0, len(ids), 500):
        for b in db.query(OnecBarcode).filter(OnecBarcode.item_id.in_(ids[i:i + 500])):
            out.setdefault(b.item_id, b)
    return out


def _cell(v) -> str:
    return "" if v is None else str(v).strip()


def _f(v):
    return float(v) if v is not None else None


def _num(raw, lo, hi, integer: bool):
    text = str(raw if raw is not None else "").strip().replace(",", ".").replace(" ", "")
    if text == "":
        raise ValueError("пусто")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("не число")
    if not value.is_finite():
        raise ValueError("не число")
    if integer:
        if value != value.to_integral_value():
            raise ValueError("нужно целое")
        value = int(value)
    if value < lo or value > hi:
        raise ValueError(f"допустимо от {lo} до {hi}")
    return value


def _import_flash(request: Request, msg: str, errors: list[str]) -> None:
    if errors:
        more = f" и ещё {len(errors) - 3}" if len(errors) > 3 else ""
        flash(request, msg + f" Ошибок: {len(errors)} ({'; '.join(errors[:3])}{more}).", "warn")
    else:
        flash(request, msg, "ok")


def _read_upload(request: Request, file: UploadFile, need: str) -> list[dict] | None:
    try:
        rows = read_xlsx_rows(file.file.read())
    except ExcelReadError as e:
        flash(request, str(e), "warn")
        return None
    if not rows or need not in rows[0]:
        flash(request, f"В файле нет колонки «{need}» — выгрузите файл с этой же вкладки.", "warn")
        return None
    return rows


# --- правила площадок ----------------------------------------------------------------

def validate_rule(db: Session, platform: str, raw: dict) -> tuple[dict, list[str]]:
    """Проверка правила площадки. `raw` — строки из формы или файла. Одна
    функция на оба пути: разойдись они, файл принимал бы то, что форма отвергает."""
    values, errors = {}, []
    try:
        values["commission_percent"] = _num(raw.get("commission_percent"), 0, Decimal("99.99"), False)
    except ValueError as e:
        errors.append(f"«Комиссия площадки»: {e}")
    for name, title, lo, hi in RULE_FIELDS:
        try:
            values[name] = _num(raw.get(name), lo, hi, name in INT_FIELDS)
        except ValueError as e:
            errors.append(f"«{title}»: {e}")
    base = _cell(raw.get("base_platform"))
    values["base_platform"], values["base_coef"] = base or None, None
    if base:
        if base not in PLATFORMS:
            errors.append(f"неизвестная базовая площадка «{base}»")
        elif base == platform:
            errors.append("площадка не может брать цену сама от себя")
        else:
            other = db.query(PlatformRule).filter(PlatformRule.platform == base).first()
            if other is not None and other.base_platform:
                errors.append(f"{PLATFORMS[base]} сама берёт цену от {PLATFORMS.get(other.base_platform)} — "
                              "цепочки не поддерживаются")
            users = [r.platform for r in db.query(PlatformRule).filter(PlatformRule.base_platform == platform)]
            if users:
                errors.append(f"от {PLATFORMS[platform]} берут цену: "
                              f"{', '.join(PLATFORMS.get(u, u) for u in users)} — цепочки не поддерживаются")
        try:
            values["base_coef"] = _num(raw.get("base_coef"), Decimal("0.1"), 10, False)
        except ValueError as e:
            errors.append(f"«Коэффициент к базовой»: {e}")
    if "round_step" in values and "round_minus" in values and values["round_minus"] >= values["round_step"]:
        errors.append("«Окончание: минус» должно быть меньше шага округления")
    if (not base and "min_markup_coef" in values and "markup_coef" in values
            and values["min_markup_coef"] > values["markup_coef"]):
        errors.append("минимальный коэффициент больше основного — все цены окажутся ниже пола")
    return values, errors


def _apply_rule(db: Session, user: User, platform: str, values: dict) -> bool:
    rule = get_rule(db, platform)
    before = {n: str(getattr(rule, n)) for n in RULE_KEYS}
    for n in RULE_KEYS:
        setattr(rule, n, values[n])
    after = {n: str(getattr(rule, n)) for n in RULE_KEYS}
    if before == after:
        return False
    audit.log(db, user.username, "price_rule_saved", PLATFORMS.get(platform, platform),
              f"было {before}, стало {after}")
    return True


def _rule_raw(rule: PlatformRule) -> dict:
    raw = {n: (str(getattr(rule, n)) if getattr(rule, n) is not None else "") for n in RULE_KEYS}
    raw["base_platform"] = rule.base_platform or ""
    return raw


@router.post("/prices/rules/{platform}")
async def save_rule(platform: str, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    if platform not in PLATFORMS:
        flash(request, "Площадка не найдена.", "warn")
        return _back("rules")
    form = await request.form()
    values, errors = validate_rule(db, platform, {k: form.get(k) for k in RULE_KEYS})
    if errors:
        flash(request, f"{PLATFORMS[platform]}: не сохранено — " + "; ".join(errors), "warn")
        return _back("rules")
    _apply_rule(db, user, platform, values)
    db.commit()
    flash(request, f"Правило {PLATFORMS[platform]} сохранено для всех её кабинетов. "
                   "Цены не изменились — нажмите «Рассчитать».", "ok")
    return _back("rules")


RULE_HEADERS = ["Площадка (код)", "Площадка", "Кабинеты", "Комиссия, %",
                *(t for _, t, *_ in RULE_FIELDS), "Цена от площадки (код)", "Коэффициент к базовой"]
RULE_COLS = {"Комиссия, %": "commission_percent", **{t: n for n, t, *_ in RULE_FIELDS},
             "Цена от площадки (код)": "base_platform", "Коэффициент к базовой": "base_coef"}


@router.get("/prices/rules-export")
def export_rules(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    accs = _accounts(db)
    data = []
    for p in _platforms(accs):
        r = get_rule(db, p)
        data.append([p, PLATFORMS[p], ", ".join(a.name for a in accs if a.platform == p),
                     _f(r.commission_percent), *(_f(getattr(r, n)) for n, *_ in RULE_FIELDS),
                     r.base_platform or "", _f(r.base_coef)])
    db.commit()
    return xlsx_response(RULE_HEADERS, data, "правила_цен.xlsx")


@router.post("/prices/rules-import")
def import_rules(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    rows = _read_upload(request, file, "Площадка (код)")
    if rows is None:
        return _back("rules")
    changed, errors = 0, []
    for i, row in enumerate(rows, start=2):
        p = _cell(row.get("Площадка (код)"))
        if not p:
            continue
        if p not in PLATFORMS:
            errors.append(f"строка {i}: неизвестная площадка «{p}»")
            continue
        raw = _rule_raw(get_rule(db, p))
        for col, name in RULE_COLS.items():
            v = _cell(row.get(col))
            if v == "":
                continue            # пустая ячейка ничего не меняет
            raw[name] = "" if v == CLEAR_CELL else v
        if not raw["base_platform"]:
            raw["base_coef"] = ""
        values, errs = validate_rule(db, p, raw)
        if errs:
            errors.append(f"строка {i} ({PLATFORMS[p]}): " + "; ".join(errs))
            continue
        if _apply_rule(db, user, p, values):
            changed += 1
        db.flush()   # следующая строка проверяет цепочки по уже применённым
    db.commit()
    _import_flash(request, f"Правил изменено: {changed}. Цены не пересчитаны — нажмите «Рассчитать».", errors)
    return _back("rules")


# --- товары: экономика по SKU кабинета ----------------------------------------------

def product_rows(db: Session, account: Account, q: str = "", flt: str = "",
                 coef_min: str = "", coef_max: str = "") -> list[dict]:
    """Экономика по каждому SKU 1С, сопоставленному с каталогом кабинета:
    по ТЕКУЩЕЙ цене площадки и по расчётной."""
    items = mapping.account_items(db, account.id)
    info = _sku_info(db, items)
    costs = {c.item_id: c for c in db.query(OnecCost).filter(OnecCost.item_id.in_(list(items)))}
    prices = {p.item_id: p for p in db.query(ProductPrice).filter(ProductPrice.account_id == account.id)}
    rule, base_rule = rules_for(db, account.platform)
    commission = rule.commission_percent
    rate = rates.current(db)
    ql = q.strip().lower()
    try:
        lo = Decimal(coef_min.replace(",", ".")) if coef_min.strip() else None
        hi = Decimal(coef_max.replace(",", ".")) if coef_max.strip() else None
    except InvalidOperation:
        lo = hi = None
    rows = []
    for item_id, plat in items.items():
        sku = info.get(item_id)
        if ql and not any(ql in (s or "").lower() for s in
                          (sku.article if sku else "", sku.name if sku else "", item_id,
                           *(p.article for p in plat), *(p.barcode for p in plat))):
            continue
        pp = prices.get(item_id)
        cost = costs.get(item_id)
        d = decide(cost.cost_usd if cost else None, rate.usd_rub if rate else None,
                   commission, rule, pp.manual_price if pp else None,
                   pp.last_sent_price if pp else None, base_rule)
        with_price = [p for p in plat if p.current_price]
        cur = max(with_price, key=lambda p: p.current_price) if with_price else None
        cur_sale = (cur.current_sale_price or cur.current_price) if cur else None
        cur_rub = cur_coef = None
        if cur_sale and d.cost_rub is not None and commission is not None:
            cur_rub, cur_coef = markup(cur_sale, d.cost_rub, commission)
        below_floor_now = cur_coef is not None and cur_coef < Decimal(str(rule.min_markup_coef))
        statuses = [p.price_status for p in plat if p.price_status]
        price_status = max(statuses, key=lambda x: platforms._STATUS_RANK.get(x, 0)) if statuses else None
        min_price = max((p.min_price for p in plat if p.min_price), default=None)
        r = {
            "item_id": item_id, "sku": sku, "platform": plat[0], "cost_usd": cost.cost_usd if cost else None,
            "cost_rub": d.cost_rub, "price": d.new_price,
            "payout": payout(d.new_price, commission) if d.new_price and commission is not None else None,
            "markup_rub": d.markup_rub, "markup_coef": d.markup_coef,
            "block_reason": d.block_reason, "note": d.note, "source": d.source,
            "manual": pp.manual_price if pp else None, "last_sent": pp.last_sent_price if pp else None,
            "current": cur.current_price if cur else None, "current_sale": cur_sale,
            "current_payout": payout(cur_sale, commission) if cur_sale and commission is not None else None,
            "current_markup_rub": cur_rub, "current_coef": cur_coef, "below_floor_now": below_floor_now,
            "price_status": price_status, "min_price": min_price,
        }
        if flt == "below_floor_now" and not below_floor_now:
            continue
        if flt == "differs" and not (r["current"] and r["price"] and r["current"] != r["price"]):
            continue
        if flt == "no_current" and r["current"]:
            continue
        if flt == "no_cost" and r["cost_usd"] is not None:
            continue
        if flt == "manual" and r["manual"] is None:
            continue
        if flt == "platform_status" and price_status not in ("QUARANTINE", "ERROR"):
            continue
        if flt == "below_platform_min" and not (min_price and r["price"] and r["price"] < min_price):
            continue
        if lo is not None and (cur_coef is None or cur_coef < lo):
            continue
        if hi is not None and (cur_coef is None or cur_coef > hi):
            continue
        rows.append(r)
    rows.sort(key=lambda r: ((r["sku"].article if r["sku"] else ""), (r["sku"].size if r["sku"] else "")))
    return rows


def parse_manual(raw) -> tuple[str, int | None]:
    """('skip'|'clear'|'set'|'bad', значение). Пустая ячейка ничего не меняет,
    «-» снимает ручную цену."""
    text = str(raw if raw is not None else "").strip().replace(" ", "").replace(",", ".")
    if text == "":
        return "skip", None
    if text == CLEAR_CELL:
        return "clear", None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return "bad", None
    if not value.is_finite() or value <= 0 or value != value.to_integral_value() or value > 10_000_000:
        return "bad", None
    return "set", int(value)


def _set_manual(db: Session, item_id: str, account_id: int, value: int | None) -> bool:
    pp = db.query(ProductPrice).filter(ProductPrice.item_id == item_id,
                                       ProductPrice.account_id == account_id).first()
    if pp is None:
        if value is None:
            return False
        pp = ProductPrice(item_id=item_id, account_id=account_id)
        db.add(pp)
        db.flush()   # autoflush выключен: повтор пары в файле иначе завёл бы дубль
    if pp.manual_price == value:
        return False
    pp.manual_price = value
    return True


def _keep(form) -> dict:
    return {k: str(form.get(k) or "") for k in KEEP}


@router.post("/prices/manual/{account_id}")
async def set_manual(account_id: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    form = await request.form()
    item_id, value, keep = str(form.get("item_id") or ""), form.get("value"), _keep(form)
    a = db.get(Account, account_id)
    if a is None or not item_id:
        flash(request, "Кабинет или товар не найден.", "warn")
        return _back("products", account_id=account_id, **keep)
    # В строке страницы пустое поле — осознанное «снять»: человек стёр число.
    kind, parsed = parse_manual(value if str(value or "").strip() else CLEAR_CELL)
    if kind == "bad":
        flash(request, f"Ручная цена «{value}» не принята: целое число рублей больше нуля (пусто — снять).", "warn")
        return _back("products", account_id=account_id, **keep)
    if _set_manual(db, item_id, account_id, parsed):
        audit.log(db, user.username, "price_manual_set", label(a), f"{item_id}: {parsed or 'снята'}")
    db.commit()
    flash(request, "Ручная цена сохранена. На площадку уйдёт после расчёта и подтверждения.", "ok")
    return _back("products", account_id=account_id, **keep)


@router.post("/prices/bulk/{account_id}")
async def bulk_edit(account_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Массовая правка ручных цен: по отмеченным строкам или по ВСЕМУ отбору.
    «Во всех кабинетах площадки» — то же в каждом её кабинете, где товар
    сопоставлен (у WB три ИП); «от текущей» берёт текущую цену КАЖДОГО
    кабинета. Ничего не отправляет: дальше расчёт и подтверждение."""
    form = await request.form()
    keep = _keep(form)
    a = db.get(Account, account_id)
    action = str(form.get("action") or "")
    if a is None or action not in BULK_ACTIONS:
        flash(request, "Выберите действие.", "warn")
        return _back("products", account_id=account_id, **keep)
    value = None
    try:
        if action == "set_manual":
            value = _num(form.get("value"), 1, 10_000_000, True)
        elif action in ("manual_from_current", "manual_from_calc"):
            value = _num(form.get("value"), Decimal("0.1"), 10, False)
    except ValueError as e:
        flash(request, f"«{BULK_ACTIONS[action]}»: значение — {e}.", "warn")
        return _back("products", account_id=account_id, **keep)
    if form.get("all_filtered") == "1":
        item_ids = [r["item_id"] for r in product_rows(db, a, **keep)]
    else:
        item_ids = list(dict.fromkeys(str(v) for v in form.getlist("ids") if str(v)))
    if not item_ids:
        flash(request, "Ничего не отмечено.", "warn")
        return _back("products", account_id=account_id, **keep)
    if len(item_ids) > BULK_LIMIT:
        flash(request, f"В отборе {len(item_ids)} строк — больше {BULK_LIMIT}. Сузьте отбор.", "warn")
        return _back("products", account_id=account_id, **keep)
    targets = [a]
    if form.get("scope") == "platform":
        targets = [x for x in _accounts(db) if x.platform == a.platform]
    wanted = set(item_ids)
    changed = skipped = 0
    for acc in targets:
        rows = {r["item_id"]: r for r in product_rows(db, acc) if r["item_id"] in wanted}
        rule = get_rule(db, acc.platform)
        skipped += len(wanted - set(rows))
        for item_id, r in rows.items():
            if action == "set_manual":
                new = value
            elif action == "clear_manual":
                new = None
            else:
                base = r["current"] if action == "manual_from_current" else r["price"]
                if not base:
                    skipped += 1
                    continue
                new = round_price(Decimal(base) * value, rule.round_step, rule.round_minus)
            if _set_manual(db, item_id, acc.id, new):
                changed += 1
    audit.log(db, user.username, "price_bulk_edit", ", ".join(x.name for x in targets)[:100],
              f"{BULK_ACTIONS[action]} {value if value is not None else ''}; строк {len(item_ids)}; "
              f"изменено {changed}, пропущено {skipped}")
    db.commit()
    msg = (f"«{BULK_ACTIONS[action]}» по {len(item_ids)} строкам в {len(targets)} кабинет(ах): изменено {changed}"
           + (f", пропущено {skipped} (нет текущей/расчётной цены или товар не сопоставлен в кабинете)"
              if skipped else "")
           + ". На площадки ничего не отправлено — нажмите «Рассчитать».")
    flash(request, msg, "warn" if skipped else "ok")
    return _back("products", account_id=account_id, **keep)


@router.post("/prices/load-current")
def load_current(request: Request, account_id: str = Form(""), back: str = Form("products"),
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Текущие цены со всех активных кабинетов — для наценки по текущим. Только
    читает площадки, ничего не меняет на них."""
    from priceapp.routers import accounts as acc_router
    accs = _accounts(db)
    done, problems = [], []
    for a in accs:
        if a.platform not in platforms.READS_PRICES:
            problems.append(f"{a.name}: {PLATFORMS[a.platform]} — чтение цен не подключено")
            continue
        if db.query(ApiCredential.id).filter(ApiCredential.account_id == a.id).first() is None:
            problems.append(f"{a.name}: нет ключей")
            continue
        try:
            st = acc_mod.load_prices(db, a, acc_mod.client_for(db, a, acc_router.CLIENT_FACTORY))
        except Exception as e:
            db.rollback()
            problems.append(f"{a.name}: {e}"[:200])
            continue
        done.append(f"{a.name} — {st['updated']}" + (" (НЕПОЛНАЯ выгрузка)" if st["truncated"] else ""))
    audit.log(db, user.username, "prices_loaded", f"{len(done)} кабинетов", "; ".join(done + problems)[:2000])
    db.commit()
    msg = "Текущие цены загружены: " + ("; ".join(done) if done else "ни по одному кабинету") + "."
    if problems:
        msg += " Не загружены: " + "; ".join(problems) + "."
    flash(request, msg, "warn" if problems else "ok")
    return _back(back if back in VIEWS else "products", account_id=account_id)


PRODUCT_HEADERS = ["ID_1С", "Артикул 1С", "Наименование", "Размер", "Цвет", "Баркод площадки",
                   "Артикул площадки", "Себестоимость, $", "Себестоимость, ₽", "Комиссия, %",
                   "Текущая цена, ₽", "Текущая цена продажи, ₽", "К получению по текущей, ₽",
                   "Наценка по текущей, ₽", "Коэффициент по текущей",
                   "Расчётная цена, ₽", "Как посчитана", "К получению по расчётной, ₽",
                   "Наценка по расчётной, ₽", "Коэффициент по расчётной",
                   "Отправлено нами, ₽", "Ручная цена, ₽"]
MANUAL_COL = "Ручная цена, ₽"


@router.get("/prices/export/{account_id}")
def export(account_id: int, q: str = Query(""), flt: str = Query(""), coef_min: str = Query(""),
           coef_max: str = Query(""), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    if a is None:
        return _back("products")
    rows = product_rows(db, a, q, flt, coef_min, coef_max)
    commission = get_rule(db, a.platform).commission_percent
    db.commit()
    data = [[r["item_id"], r["sku"].article if r["sku"] else "", r["sku"].name if r["sku"] else "",
             r["sku"].size if r["sku"] else "", r["sku"].color if r["sku"] else "",
             r["platform"].barcode, r["platform"].article, _f(r["cost_usd"]), _f(r["cost_rub"]),
             _f(commission), r["current"], r["current_sale"], _f(r["current_payout"]),
             _f(r["current_markup_rub"]), _f(r["current_coef"]),
             r["price"], SOURCE_LABELS.get(r["source"], "") if r["price"] else r["note"],
             _f(r["payout"]), _f(r["markup_rub"]), _f(r["markup_coef"]), r["last_sent"], r["manual"]]
            for r in rows]
    return xlsx_response(PRODUCT_HEADERS, data, f"цены_{a.name}.xlsx")


@router.post("/prices/import/{account_id}")
def import_manual(account_id: int, request: Request, file: UploadFile = File(...),
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Импорт ручных цен из файла вкладки «Товары». Читается ТОЛЬКО колонка
    «Ручная цена, ₽»: остальное справочное. Пустая ячейка ничего не меняет, «-»
    снимает ручную цену. На площадки ничего не уходит."""
    a = db.get(Account, account_id)
    if a is None:
        return _back("products")
    rows = _read_upload(request, file, MANUAL_COL)
    if rows is None:
        return _back("products", account_id=account_id)
    known = set(mapping.account_items(db, a.id))
    changed, errors = 0, []
    for i, row in enumerate(rows, start=2):
        item_id = _cell(row.get("ID_1С"))
        if not item_id:
            continue
        kind, value = parse_manual(row.get(MANUAL_COL))
        if kind == "skip":
            continue
        if item_id not in known:
            errors.append(f"строка {i}: {item_id} не сопоставлен с каталогом кабинета")
            continue
        if kind == "bad":
            errors.append(f"строка {i}: «{row.get(MANUAL_COL)}» — не целое число рублей")
            continue
        if _set_manual(db, item_id, a.id, value):
            changed += 1
    audit.log(db, user.username, "price_import", label(a), f"изменено {changed}, ошибок {len(errors)}")
    db.commit()
    _import_flash(request, f"Ручных цен изменено: {changed}. На площадки ничего не отправлено.", errors)
    return _back("products", account_id=account_id)


# --- предложения и журнал -------------------------------------------------------------

def _statuses(view: str) -> tuple:
    return OPEN if view == "proposals" else ("approved", "sent", "error", "rejected")


def _changes_query(db: Session, view: str, account_id: str, status: str = "", q: str = ""):
    statuses = _statuses(view)
    if status in statuses:
        statuses = (status,)
    query = db.query(PriceChange).filter(PriceChange.status.in_(statuses), PriceChange.is_test.is_(False))
    if (account_id or "").isdigit():
        query = query.filter(PriceChange.account_id == int(account_id))
    if q.strip():
        like = f"%{q.strip()}%"
        ids = [r[0] for r in db.query(OnecBarcode.item_id).filter(
            OnecBarcode.article.ilike(like) | OnecBarcode.name.ilike(like) |
            OnecBarcode.barcode.ilike(like)).distinct().limit(BULK_LIMIT)]
        query = query.filter(PriceChange.item_id.in_(ids) | PriceChange.barcode.ilike(like))
    return query.order_by(PriceChange.id.asc() if view == "proposals" else PriceChange.id.desc())


CHANGE_HEADERS = ["ID предложения", "Кабинет", "ID_1С", "Артикул 1С", "Наименование", "Размер",
                  "Баркод", "Себестоимость, $", "Курс", "Себестоимость, ₽", "Комиссия, %",
                  "Было, ₽", "Станет, ₽", "Изменение, %", "Наценка, ₽", "Коэффициент",
                  "Как посчитана", "Статус", "Причина / примечание"]
DECISION_COL = "Решение (Да / Нет)"
LARGE_COL = "Подтверждаю большой шаг (Да)"


@router.get("/prices/changes-export")
def export_changes(view: str = Query("proposals"), account_id: str = Query(""), status: str = Query(""),
                   q: str = Query(""), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    view = "log" if view == "log" else "proposals"
    changes = _changes_query(db, view, account_id, status, q).limit(BULK_LIMIT).all()
    skus = _sku_info(db, {c.item_id for c in changes})
    data = []
    for c in changes:
        s = skus.get(c.item_id)
        pct = change_percent(c.old_price, c.new_price)
        row = [c.id, label(c.account), c.item_id, s.article if s else "", s.name if s else "",
               s.size if s else "", c.barcode, _f(c.cost_usd), _f(c.usd_rub), _f(c.cost_rub),
               _f(c.commission_percent), c.old_price, c.new_price,
               round(pct, 1) if pct is not None else None, _f(c.markup_rub), _f(c.markup_coef),
               SOURCE_LABELS.get(c.source, c.source), STATUS_LABELS.get(c.status, c.status),
               "; ".join(x for x in (BLOCK_LABELS.get(c.block_reason or "", ""), c.note or "",
                                     c.last_error or "") if x)]
        if view == "proposals":
            row += ["", ""]
        data.append(row)
    headers = CHANGE_HEADERS + ([DECISION_COL, LARGE_COL] if view == "proposals" else [])
    return xlsx_response(headers, data, "предложения_цен.xlsx" if view == "proposals" else "журнал_цен.xlsx")


@router.post("/prices/changes-import")
def import_changes(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Решения по предложениям файлом: «Да» — подтвердить через тот же
    `approve`, что и кнопка (пол не обходится никогда, большой шаг — только с
    «Да» в соседней колонке), «Нет» — отклонить, пусто — ничего."""
    rows = _read_upload(request, file, DECISION_COL)
    if rows is None:
        return _back("proposals")
    approved = rejected = 0
    errors = []
    for i, row in enumerate(rows, start=2):
        decision = _cell(row.get(DECISION_COL)).lower()
        if decision == "":
            continue
        raw_id = _cell(row.get("ID предложения"))
        if raw_id.endswith(".0"):
            raw_id = raw_id[:-2]
        ch = db.get(PriceChange, int(raw_id)) if raw_id.isdigit() else None
        if ch is None or ch.is_test:
            errors.append(f"строка {i}: предложение «{raw_id}» не найдено")
            continue
        if decision == "да":
            reason = approve(ch, user.username, _cell(row.get(LARGE_COL)).lower() == "да")
            if reason:
                errors.append(f"строка {i} (#{ch.id}): {reason}")
            else:
                approved += 1
        elif decision == "нет":
            if ch.status not in OPEN + ("approved",):
                errors.append(f"строка {i} (#{ch.id}): уже решено")
                continue
            ch.status = PriceChangeStatus.rejected.value
            ch.decided_by, ch.decided_at = user.username, now_utc()
            rejected += 1
        else:
            errors.append(f"строка {i}: «{row.get(DECISION_COL)}» — нужно «Да», «Нет» или пусто")
    audit.log(db, user.username, "price_decisions_import", f"{approved}+{rejected}",
              f"подтверждено {approved}, отклонено {rejected}, ошибок {len(errors)}")
    db.commit()
    _import_flash(request, f"Из файла: подтверждено {approved}, отклонено {rejected}. "
                           "Подтверждённые уйдут на площадки в течение пары минут.", errors)
    return _back("proposals")


@router.get("/prices")
def page(request: Request, view: str = Query("proposals"), account_id: str = Query(""),
         q: str = Query(""), status: str = Query(""), flt: str = Query(""), coef_min: str = Query(""),
         coef_max: str = Query(""), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    view = view if view in VIEWS else "proposals"
    accs = _accounts(db)
    ctx = dict(view=view, accounts=accs, label=label, account_id=account_id, q=q, status=status,
               status_labels=STATUS_LABELS, block_labels=BLOCK_LABELS, BLOCK_FLOOR=BLOCK_FLOOR,
               BLOCK_MAX_CHANGE=BLOCK_MAX_CHANGE, rule_fields=RULE_FIELDS, platform_names=PLATFORMS,
               source_labels=SOURCE_LABELS, change_percent=change_percent, rate=rates.current(db),
               rows_limit=ROWS_LIMIT, ru=_ru, price_status_labels=PRICE_STATUS_LABELS,
               saved_filters=db.query(SavedFilter).order_by(SavedFilter.name).all(),
               current_url=str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
               rate_shift=overview.rate_shift(db),
               open_count=db.query(PriceChange).filter(PriceChange.status.in_(OPEN),
                                                       PriceChange.is_test.is_(False)).count())
    if view == "rules":
        plats = _platforms(accs)
        ctx.update(plats=plats, rules={p: get_rule(db, p) for p in plats},
                   cabinets={p: [a for a in accs if a.platform == p] for p in plats})
        db.commit()
    elif view == "products":
        account = _pick(accs, account_id)
        rows = product_rows(db, account, q, flt, coef_min, coef_max) if account else []
        rule = get_rule(db, account.platform) if account else None
        db.commit()
        ctx.update(account=account, account_id=str(account.id) if account else "", rows=rows[:ROWS_LIMIT],
                   total=len(rows), rule=rule, flt=flt, coef_min=coef_min, coef_max=coef_max,
                   product_filters=PRODUCT_FILTERS, bulk_actions=BULK_ACTIONS,
                   reads_prices=platforms.READS_PRICES,
                   siblings=[a for a in accs if account and a.platform == account.platform],
                   export_qs=urlencode({"q": q, "flt": flt, "coef_min": coef_min, "coef_max": coef_max}))
    elif view == "compare":
        rows, cols = compare_rows(db, accs, q, flt)
        ctx.update(rows=rows[:ROWS_LIMIT], total=len(rows), cols=cols, flt=flt,
                   compare_filters=COMPARE_FILTERS, export_qs=urlencode({"q": q, "flt": flt}))
    else:
        query = _changes_query(db, view, account_id, status, q)
        changes = query.limit(ROWS_LIMIT).all()
        if view == "proposals":
            every = query.limit(BULK_LIMIT).all()
            ctx.update(summary=overview.proposals_summary(db, every),
                       info=overview.change_context(db, changes))
        else:
            ctx.update(info={})
        ctx.update(total=query.count(), changes=changes, status_choices=_statuses(view),
                   skus=_sku_info(db, {c.item_id for c in changes}),
                   export_qs=urlencode({"view": view, "account_id": account_id, "status": status, "q": q}))
    return render(request, "prices.html", user, "prices", **ctx)


@router.post("/prices/recalculate")
def recalculate(request: Request, account_id: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    accs = _accounts(db)
    if account_id.isdigit():
        accs = [a for a in accs if a.id == int(account_id)]
    if rates.current(db) is None:
        flash(request, "Курса доллара нет — считать не по чему. Страница «Курс $».", "error")
        return _back("proposals", account_id=account_id)
    total = {"proposed": 0, "blocked": 0, "unchanged": 0, "skipped": 0}
    reasons: dict[str, int] = {}
    for a in accs:
        st = recalculate_account(db, a)
        for k in total:
            total[k] += getattr(st, k)
        for r, n in st.reasons.items():
            reasons[r] = reasons.get(r, 0) + n
    audit.log(db, user.username, "price_recalculated", ", ".join(label(a) for a in accs)[:100],
              f"{total}; не посчитано: {reasons}")
    db.commit()
    msg = (f"Расчёт по курсу {rates.current(db).usd_rub} ₽: предложено {total['proposed']}, "
           f"заблокировано {total['blocked']}, без изменений {total['unchanged']}")
    if reasons:
        msg += ", не посчитано: " + "; ".join(f"{r} — {n}" for r, n in reasons.items())
    flash(request, msg + ". На площадки ничего не отправлено.", "ok")
    return _back("proposals", account_id=account_id)


def _ids(form) -> list[int]:
    return [int(v) for v in form.getlist("ids") if str(v).isdigit()]


@router.post("/prices/approve")
async def approve_changes(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    form = await request.form()
    ids, account_id = _ids(form), str(form.get("account_id") or "")
    confirm_large = form.get("confirm_large") == "true"
    if form.get("all_filtered") == "1":
        # Весь отбор, а не 500 видимых строк: тот же запрос, что рисует страницу.
        ids = [c.id for c in _changes_query(db, "proposals", account_id, str(form.get("status") or ""),
                                            str(form.get("q") or "")).limit(BULK_LIMIT)]
    if not ids:
        flash(request, "Ничего не выбрано.", "warn")
        return _back("proposals", account_id=account_id)
    approved, refused = 0, {}
    for ch in db.query(PriceChange).filter(PriceChange.id.in_(ids), PriceChange.is_test.is_(False)):
        reason = approve(ch, user.username, confirm_large)
        if reason is None:
            approved += 1
        else:
            refused[reason] = refused.get(reason, 0) + 1
    audit.log(db, user.username, "price_approved", f"{approved} шт",
              f"крупные изменения: {'да' if confirm_large else 'нет'}; отказано {refused}; id={ids[:50]}")
    db.commit()
    msg = f"Подтверждено: {approved}. Отправка на площадки — в течение пары минут."
    if refused:
        msg += " Не подтверждено: " + "; ".join(f"{n} — {r}" for r, n in refused.items()) + "."
    flash(request, msg, "warn" if refused else "ok")
    return _back("proposals", account_id=account_id)


@router.post("/prices/reject")
async def reject_changes(request: Request, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    form = await request.form()
    ids, account_id = _ids(form), str(form.get("account_id") or "")
    if form.get("all_filtered") == "1":
        ids = [c.id for c in _changes_query(db, "proposals", account_id, str(form.get("status") or ""),
                                            str(form.get("q") or "")).limit(BULK_LIMIT)]
    n = 0
    for ch in db.query(PriceChange).filter(PriceChange.id.in_(ids), PriceChange.is_test.is_(False),
                                           PriceChange.status.in_(OPEN + ("approved",))):
        ch.status = PriceChangeStatus.rejected.value
        ch.decided_by, ch.decided_at = user.username, now_utc()
        n += 1
    audit.log(db, user.username, "price_rejected", f"{n} шт", f"id={ids[:50]}")
    db.commit()
    flash(request, f"Отклонено: {n}.", "ok" if n else "warn")
    return _back("proposals", account_id=account_id)



# --- сравнение площадок: товар и все кабинеты в одной строке -------------------------

COMPARE_FILTERS = {"": "все", "spread": "цены расходятся больше чем на 15%",
                   "below_floor_now": "где-то ниже пола по текущей"}


def compare_rows(db: Session, accs: list[Account], q: str = "", flt: str = "") -> tuple[list[dict], list[Account]]:
    """SKU 1С -> по каждому кабинету: текущая и расчётная цена, коэффициент по
    текущей. Считается ТЕМ ЖЕ `product_rows`, что и «Товары»: разойдись они,
    сравнение показывало бы не те числа, что вкладка кабинета."""
    per = {a.id: {r["item_id"]: r for r in product_rows(db, a, q)} for a in accs}
    cols = [a for a in accs if per[a.id]]
    items = sorted({i for a in cols for i in per[a.id]})
    out = []
    for item_id in items:
        cells = {a.id: per[a.id].get(item_id) for a in cols}
        present = [c for c in cells.values() if c]
        sku = present[0]["sku"]
        current = [c["current"] for c in present if c["current"]]
        spread = (max(current) - min(current)) * 100.0 / min(current) if len(current) > 1 else 0.0
        if flt == "spread" and spread <= 15:
            continue
        if flt == "below_floor_now" and not any(c["below_floor_now"] for c in present):
            continue
        out.append({"item_id": item_id, "sku": sku, "cells": cells, "spread": spread})
    out.sort(key=lambda r: ((r["sku"].article if r["sku"] else ""), (r["sku"].size if r["sku"] else "")))
    return out, cols


@router.get("/prices/compare-export")
def export_compare(q: str = Query(""), flt: str = Query(""), db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    rows, cols = compare_rows(db, _accounts(db), q, flt)
    db.commit()
    headers = ["ID_1С", "Артикул 1С", "Наименование", "Размер", "Цвет", "Разброс текущих, %"]
    for a in cols:
        headers += [f"{a.name}: текущая, ₽", f"{a.name}: коэфф. по текущей", f"{a.name}: расчётная, ₽"]
    data = []
    for r in rows:
        s = r["sku"]
        line = [r["item_id"], s.article if s else "", s.name if s else "", s.size if s else "",
                s.color if s else "", round(r["spread"], 1)]
        for a in cols:
            c = r["cells"][a.id]
            line += [c["current"], _f(c["current_coef"]), c["price"]] if c else [None, None, None]
        data.append(line)
    return xlsx_response(headers, data, "сравнение_площадок.xlsx")


# --- история цены товара и возврат прежней ------------------------------------------

@router.get("/prices/history/{account_id}/{item_id}")
def history(account_id: int, item_id: str, request: Request, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    if a is None:
        return _back("products")
    changes = (db.query(PriceChange).filter(PriceChange.account_id == a.id, PriceChange.item_id == item_id,
                                            PriceChange.is_test.is_(False))
               .order_by(PriceChange.id.desc()).limit(200).all())
    pp = db.query(ProductPrice).filter(ProductPrice.item_id == item_id, ProductPrice.account_id == a.id).first()
    others = [x for x in _accounts(db) if x.platform == a.platform and x.id != a.id]
    return render(request, "price_history.html", user, "prices", account=a, label=label, item_id=item_id,
                  sku=_sku_info(db, [item_id]).get(item_id), changes=changes, pp=pp,
                  status_labels=STATUS_LABELS, source_labels=SOURCE_LABELS, block_labels=BLOCK_LABELS,
                  change_percent=change_percent, others=others)


@router.post("/prices/rollback/{change_id}")
def rollback(change_id: int, request: Request, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """Вернуть цену, которую когда-то приняла площадка. Создаёт ОБЫЧНОЕ
    предложение (пол, лимит шага, подтверждение) — сама ничего не отправляет."""
    ch = db.get(PriceChange, change_id)
    if ch is None or ch.is_test or ch.status != PriceChangeStatus.sent.value:
        flash(request, "Вернуть можно только цену, которую площадка приняла.", "warn")
        return _back("proposals")
    a = db.get(Account, ch.account_id)
    new = propose_price(db, a, ch.item_id, ch.new_price, "rollback",
                        f"возврат цены от {ch.sent_at.strftime('%d.%m.%Y') if ch.sent_at else '—'}")
    if new is None:
        db.rollback()
        flash(request, "Не получилось: товар больше не сопоставлен с каталогом кабинета или нет комиссии.", "warn")
        return RedirectResponse(f"/prices/history/{ch.account_id}/{ch.item_id}", status_code=303)
    audit.log(db, user.username, "price_rollback", label(a), f"{ch.item_id}: {ch.new_price} (из #{ch.id})")
    db.commit()
    flash(request, f"Предложение вернуть {ch.new_price} ₽ создано"
                   + (f" — но оно заблокировано: {BLOCK_LABELS[new.block_reason]}" if new.block_reason else "")
                   + ". На площадку уйдёт после подтверждения.", "warn" if new.block_reason else "ok")
    return _back("proposals", account_id=ch.account_id)


# --- «что будет, если» для правила --------------------------------------------------

@router.post("/prices/rules/{platform}/preview")
async def preview(platform: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    if platform not in PLATFORMS:
        return _back("rules")
    form = await request.form()
    values, errors = validate_rule(db, platform, {k: form.get(k) for k in RULE_KEYS})
    if errors:
        flash(request, f"{PLATFORMS[platform]}: проверить нельзя — " + "; ".join(errors), "warn")
        return _back("rules")
    result = preview_rule(db, platform, values)
    db.rollback()        # проверка ничего не пишет, даже заведённые по ходу правила
    parts = []
    for pv in result:
        avg = f", в среднем {pv.avg_change:+.1f}%" if pv.avg_change is not None else ""
        parts.append(f"{PLATFORMS[pv.platform]} ({pv.accounts} каб.): посчитается {pv.priced_after} цен "
                     f"(сейчас {pv.priced_before}), изменится {pv.changed} — выше {pv.up}, ниже {pv.down}{avg}; "
                     f"упрётся в пол {pv.floor}, большой шаг {pv.big_step}")
    flash(request, "Если сохранить: " + " | ".join(parts) + ". Ничего не сохранено.", "info")
    return _back("rules")


# --- сохранённые отборы -------------------------------------------------------------

@router.post("/filters/save")
def save_filter(request: Request, name: str = Form(""), url: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    name = name.strip()[:100]
    # Только свои страницы: адрес из формы не должен уводить куда угодно.
    if not name or not (url.startswith("/prices") or url.startswith("/mapping")) or "//" in url:
        flash(request, "Дайте отбору имя.", "warn")
        return RedirectResponse(url if url.startswith("/") and "//" not in url else "/prices", status_code=303)
    db.add(SavedFilter(name=name, url=url[:1000], created_by=user.username))
    db.commit()
    flash(request, f"Отбор «{name}» сохранён — он над вкладками.", "ok")
    return RedirectResponse(url, status_code=303)


@router.post("/filters/{filter_id}/delete")
def delete_filter(filter_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    f = db.get(SavedFilter, filter_id)
    if f is not None:
        db.delete(f)
        db.commit()
        flash(request, f"Отбор «{f.name}» удалён.", "ok")
    return _back("proposals")

"""Страница «Цены».

Вкладки:
  * «Правила» — по кабинету: комиссия площадки, коэффициент наценки (2 = +100%),
    минимальный коэффициент,
    округление, лимит шага;
  * «Предложения» — результат расчёта: подтвердить / отклонить;
  * «Товары» — вся экономика по SKU кабинета (себестоимость $, курс, себестоимость ₽,
    цена, к получению, наценка ₽ и коэффициентом), ручная цена, Excel;
  * «Журнал» — что ушло на площадки и что нет.

Ничего не уходит на площадку без подтверждения оператора.
"""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import audit, mapping, rates
from priceapp.database import get_db
from priceapp.deps import get_current_user
from priceapp.excel import ExcelReadError, read_xlsx_rows, xlsx_response
from priceapp.flash import flash
from priceapp.models import (Account, OnecBarcode, OnecCost, PriceChange, PriceChangeStatus,
                             ProductPrice, User)
from priceapp.pages import render
from priceapp.platforms import PLATFORMS
from priceapp.pricing import (BLOCK_FLOOR, BLOCK_LABELS, BLOCK_MAX_CHANGE, OPEN, approve,
                              change_percent, decide, get_or_create_rule, payout,
                              recalculate_account)
from priceapp.timeutils import now_utc

router = APIRouter()

ROWS_LIMIT = 500
VIEWS = ("rules", "proposals", "products", "log")
STATUS_LABELS = {
    "proposed": "ждёт решения", "blocked": "заблокировано", "approved": "подтверждено, ждёт отправки",
    "sent": "отправлено", "error": "площадка не приняла", "rejected": "отклонено",
}
# (поле, подпись, мин, макс). Комиссия — поле КАБИНЕТА, остальное — правила.
RULE_FIELDS = [
    ("markup_coef", "Коэффициент наценки (2 = +100%)", 1, 100),
    ("min_markup_coef", "Мин. коэффициент (пол)", 0, 100),
    ("round_step", "Округлять вверх до, ₽", 1, 10000),
    ("round_minus", "Окончание: минус, ₽", 0, 9999),
    ("max_change_percent", "Макс. изменение за раз, %", 0, 1000),
]


def _accounts(db: Session) -> list[Account]:
    return list(db.query(Account).filter(Account.is_active.is_(True))
                .order_by(Account.platform, Account.name))


def label(a: Account) -> str:
    return f"{a.name} ({PLATFORMS.get(a.platform, a.platform)})"


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


def product_rows(db: Session, account: Account, q: str = "") -> list[dict]:
    """Экономика по каждому SKU 1С, сопоставленному с каталогом кабинета."""
    items = mapping.account_items(db, account.id)
    info = _sku_info(db, items)
    costs = {c.item_id: c for c in db.query(OnecCost).filter(OnecCost.item_id.in_(list(items)))}
    prices = {p.item_id: p for p in db.query(ProductPrice).filter(ProductPrice.account_id == account.id)}
    rule = get_or_create_rule(db, account.id)
    rate = rates.current(db)
    ql = q.strip().lower()
    rows = []
    for item_id, plat in items.items():
        sku = info.get(item_id)
        if ql and not any(ql in (s or "").lower() for s in
                          (sku.article if sku else "", sku.name if sku else "", item_id,
                           plat[0].article, plat[0].barcode)):
            continue
        pp = prices.get(item_id)
        cost = costs.get(item_id)
        d = decide(cost.cost_usd if cost else None, rate.usd_rub if rate else None,
                   account.commission_percent, rule, pp.manual_price if pp else None,
                   pp.last_sent_price if pp else None)
        rows.append({
            "item_id": item_id, "sku": sku, "platform": plat[0], "cost_usd": cost.cost_usd if cost else None,
            "cost_rub": d.cost_rub, "price": d.new_price,
            "payout": payout(d.new_price, account.commission_percent) if d.new_price else None,
            "markup_rub": d.markup_rub, "markup_coef": d.markup_coef,
            "block_reason": d.block_reason, "note": d.note, "source": d.source,
            "manual": pp.manual_price if pp else None, "last_sent": pp.last_sent_price if pp else None,
        })
    rows.sort(key=lambda r: ((r["sku"].article if r["sku"] else ""), (r["sku"].size if r["sku"] else "")))
    return rows


def _changes_query(db: Session, view: str, account_id: str):
    statuses = OPEN if view == "proposals" else ("approved", "sent", "error", "rejected")
    query = db.query(PriceChange).filter(PriceChange.status.in_(statuses), PriceChange.is_test.is_(False))
    if (account_id or "").isdigit():
        query = query.filter(PriceChange.account_id == int(account_id))
    return query.order_by(PriceChange.id.asc() if view == "proposals" else PriceChange.id.desc())


@router.get("/prices")
def page(request: Request, view: str = Query("proposals"), account_id: str = Query(""),
         q: str = Query(""), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    view = view if view in VIEWS else "proposals"
    accs = _accounts(db)
    ctx = dict(view=view, accounts=accs, label=label, account_id=account_id, q=q,
               status_labels=STATUS_LABELS, block_labels=BLOCK_LABELS, BLOCK_FLOOR=BLOCK_FLOOR,
               BLOCK_MAX_CHANGE=BLOCK_MAX_CHANGE, rule_fields=RULE_FIELDS,
               change_percent=change_percent, rate=rates.current(db), rows_limit=ROWS_LIMIT,
               open_count=db.query(PriceChange).filter(PriceChange.status.in_(OPEN),
                                                       PriceChange.is_test.is_(False)).count())
    if view == "rules":
        ctx["rules"] = {a.id: get_or_create_rule(db, a.id) for a in accs}
        db.commit()
    elif view == "products":
        account = _pick(accs, account_id)
        rows = product_rows(db, account, q) if account else []
        db.commit()
        ctx.update(account=account, account_id=str(account.id) if account else "",
                   rows=rows[:ROWS_LIMIT], total=len(rows))
    else:
        query = _changes_query(db, view, account_id)
        changes = query.limit(ROWS_LIMIT).all()
        ctx.update(total=query.count(), changes=changes,
                   skus=_sku_info(db, {c.item_id for c in changes}))
    return render(request, "prices.html", user, "prices", **ctx)


def _num(raw, lo, hi, integer: bool):
    text = str(raw or "").strip().replace(",", ".").replace(" ", "")
    if text == "":
        raise ValueError("пусто")
    try:
        value = int(text) if integer else Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("не число")
    if not integer and not value.is_finite():
        raise ValueError("не число")
    if value < lo or value > hi:
        raise ValueError(f"допустимо от {lo} до {hi}")
    return value


@router.post("/prices/rules/{account_id}")
async def save_rule(account_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    if a is None:
        flash(request, "Кабинет не найден.", "warn")
        return _back("rules")
    form = await request.form()
    values, errors = {}, []
    try:
        commission = _num(form.get("commission_percent"), 0, Decimal("99.99"), False)
    except ValueError as e:
        errors.append(f"«Комиссия площадки»: {e}")
    for name, title, lo, hi in RULE_FIELDS:
        try:
            values[name] = _num(form.get(name), lo, hi, name in ("round_step", "round_minus"))
        except ValueError as e:
            errors.append(f"«{title}»: {e}")
    if not errors and values["round_minus"] >= values["round_step"]:
        errors.append("«Окончание: минус» должно быть меньше шага округления")
    if not errors and values["min_markup_coef"] > values["markup_coef"]:
        errors.append("минимальный коэффициент больше основного — все цены окажутся ниже пола")
    if errors:
        flash(request, f"{label(a)}: не сохранено — " + "; ".join(errors), "warn")
        return _back("rules")
    rule = get_or_create_rule(db, a.id)
    before = {"commission": str(a.commission_percent), **{n: str(getattr(rule, n)) for n, *_ in RULE_FIELDS}}
    a.commission_percent = commission
    for n, v in values.items():
        setattr(rule, n, v)
    audit.log(db, user.username, "price_rule_saved", label(a),
              f"было {before}, стало комиссия {commission}, {dict((k, str(v)) for k, v in values.items())}")
    db.commit()
    flash(request, f"Правило {label(a)} сохранено. Цены не изменились — нажмите «Рассчитать».", "ok")
    return _back("rules")


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


def parse_manual(raw) -> tuple[bool, int | None]:
    """(корректно, значение). Пусто — сброс ручной цены."""
    text = str(raw if raw is not None else "").strip().replace(" ", "").replace(",", ".")
    if text == "":
        return True, None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return False, None
    if not value.is_finite() or value <= 0 or value != value.to_integral_value() or value > 10_000_000:
        return False, None
    return True, int(value)


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


@router.post("/prices/manual/{account_id}")
async def set_manual(account_id: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    form = await request.form()
    item_id, value, q = str(form.get("item_id") or ""), form.get("value"), str(form.get("q") or "")
    a = db.get(Account, account_id)
    if a is None or not item_id:
        flash(request, "Кабинет или товар не найден.", "warn")
        return _back("products", account_id=account_id, q=q)
    ok, parsed = parse_manual(value)
    if not ok:
        flash(request, f"Ручная цена «{value}» не принята: целое число рублей больше нуля (пусто — сбросить).", "warn")
        return _back("products", account_id=account_id, q=q)
    if _set_manual(db, item_id, account_id, parsed):
        audit.log(db, user.username, "price_manual_set", label(a), f"{item_id}: {parsed or 'сброшена'}")
    db.commit()
    flash(request, "Ручная цена сохранена. На площадку уйдёт после расчёта и подтверждения.", "ok")
    return _back("products", account_id=account_id, q=q)


EXPORT_HEADERS = ["ID_1С", "Артикул 1С", "Наименование", "Размер", "Цвет", "Баркод площадки",
                  "Артикул площадки", "Себестоимость, $", "Себестоимость, ₽", "Комиссия, %",
                  "Расчётная цена, ₽", "К получению, ₽", "Наценка, ₽", "Коэффициент",
                  "На площадке (отправлено), ₽", "Ручная цена, ₽"]
MANUAL_COL = "Ручная цена, ₽"


@router.get("/prices/export/{account_id}")
def export(account_id: int, q: str = Query(""), db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    if a is None:
        return _back("products")
    rows = product_rows(db, a, q)
    db.commit()
    f = lambda v: float(v) if v is not None else None  # noqa: E731
    data = [[r["item_id"], r["sku"].article if r["sku"] else "", r["sku"].name if r["sku"] else "",
             r["sku"].size if r["sku"] else "", r["sku"].color if r["sku"] else "",
             r["platform"].barcode, r["platform"].article, f(r["cost_usd"]), f(r["cost_rub"]),
             f(a.commission_percent), r["price"], f(r["payout"]), f(r["markup_rub"]),
             f(r["markup_coef"]), r["last_sent"], r["manual"]] for r in rows]
    return xlsx_response(EXPORT_HEADERS, data, f"цены_{a.name}.xlsx")


@router.post("/prices/import/{account_id}")
def import_manual(account_id: int, request: Request, file: UploadFile = File(...),
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Импорт ручных цен из файла, выгруженного с вкладки «Товары». Читается
    ТОЛЬКО колонка «Ручная цена, ₽»: остальное справочное. Пустая ячейка
    сбрасывает ручную цену. На площадки ничего не уходит."""
    a = db.get(Account, account_id)
    if a is None:
        return _back("products")
    try:
        rows = read_xlsx_rows(file.file.read())
    except ExcelReadError as e:
        flash(request, str(e), "warn")
        return _back("products", account_id=account_id)
    if not rows or MANUAL_COL not in rows[0]:
        flash(request, f"В файле нет колонки «{MANUAL_COL}» — выгрузите файл с вкладки «Товары».", "warn")
        return _back("products", account_id=account_id)
    known = set(mapping.account_items(db, a.id))
    changed, errors = 0, []
    for i, row in enumerate(rows, start=2):
        item_id = str(row.get("ID_1С") or "").strip()
        if not item_id:
            continue
        if item_id not in known:
            errors.append(f"строка {i}: {item_id} не сопоставлен с каталогом кабинета")
            continue
        ok, value = parse_manual(row.get(MANUAL_COL))
        if not ok:
            errors.append(f"строка {i}: «{row.get(MANUAL_COL)}» — не целое число рублей")
            continue
        if _set_manual(db, item_id, a.id, value):
            changed += 1
    audit.log(db, user.username, "price_import", label(a), f"изменено {changed}, ошибок {len(errors)}")
    db.commit()
    msg = f"Ручных цен изменено: {changed}. На площадки ничего не отправлено."
    if errors:
        more = f" и ещё {len(errors) - 3}" if len(errors) > 3 else ""
        flash(request, msg + f" Ошибок: {len(errors)} ({'; '.join(errors[:3])}{more}).", "warn")
    else:
        flash(request, msg, "ok")
    return _back("products", account_id=account_id)

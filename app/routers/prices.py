"""Страница «Цены» (/prices) — репрайсер.

Цена на площадке = себестоимость из 1С × наценка, по правилу КАБИНЕТА: у каждой
площадки (и каждого кабинета WB) свои комиссии и логистика, поэтому и условия
свои. Логика расчёта и ограничителей — app/pricing.py, отправка —
app/workers/price_dispatch.py.

Вкладки, как на «Мэппинге»:
  * «Правила» — условия по кабинетам;
  * «Цены товаров» — себестоимость, ручная и последняя отправленная цена по
    каждому кабинету; правка через Excel (экспорт → правка → импорт);
  * «Предложения» — результат расчёта, подтверждение и отклонение;
  * «Журнал» — что ушло на площадки и что нет.

Ничего не уходит на площадку без подтверждения оператора.
"""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request, Depends, Form, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.audit import log_action
from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.excel_utils import build_xlsx_response, read_xlsx_rows, format_dt, ExcelReadError
from app.flash import set_flash, pop_flash
from app.models import (Platform, PlatformAccount, PriceChange, PriceChangeStatus, PriceRule, Product,
                        ProductPrice, SyncSetting, User)
from app.pricing import (BLOCK_FLOOR, BLOCK_LABELS, BLOCK_MAX_CHANGE, approve, change_percent,
                         decide_price, get_or_create_rule, recalculate_account)
from app.timeutils import now_utc

router = APIRouter()
templates = shared_templates

ROWS_LIMIT = 300
VIEWS = ("rules", "products", "proposals", "log")

STATUS_LABELS = {
    PriceChangeStatus.proposed: "ждёт решения",
    PriceChangeStatus.blocked: "заблокировано",
    PriceChangeStatus.approved: "подтверждено, ждёт отправки",
    PriceChangeStatus.sent: "отправлено",
    PriceChangeStatus.error: "площадка не приняла",
    PriceChangeStatus.rejected: "отклонено",
}

# Поля правила: (имя, подпись, тип, мин, макс)
RULE_FIELDS = [
    ("markup_percent", "Наценка, %", "decimal", 0, 10000),
    ("fixed_add", "Надбавка, ₽", "int", 0, 1000000),
    ("round_step", "Округлять вверх до, ₽", "int", 1, 10000),
    ("round_minus", "Окончание: минус, ₽", "int", 0, 9999),
    ("min_margin_percent", "Мин. наценка (пол), %", "decimal", 0, 10000),
    ("max_change_percent", "Макс. изменение за раз, %", "decimal", 0, 1000),
]


def _accounts(db: Session) -> list[PlatformAccount]:
    return list(db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True))
                .order_by(PlatformAccount.platform, PlatformAccount.name).all())


def _account_label(a: PlatformAccount) -> str:
    return f"{a.name} ({a.platform.value.upper()})"


def _back(view: str, **params) -> RedirectResponse:
    from urllib.parse import urlencode
    query = urlencode({"view": view, **{k: v for k, v in params.items() if v not in ("", None)}})
    return RedirectResponse(f"/prices?{query}", status_code=303)


def _products_query(db: Session, q: str):
    """Товары, которые ведутся хотя бы в одном кабинете (синхронизация включена) —
    цены остальных на площадках нам не принадлежат."""
    enabled = db.query(SyncSetting.uid_1c).filter(SyncSetting.enabled.is_(True))
    query = db.query(Product).filter(Product.uid_1c.in_(enabled))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Product.article.ilike(like), Product.name.ilike(like),
                                 Product.uid_1c.ilike(like)))
    return query.order_by(Product.article.asc(), Product.size.asc())


def _product_rows(db: Session, products: list[Product], accounts: list[PlatformAccount]) -> list[dict]:
    uids = [p.uid_1c for p in products]
    prices = {(pp.uid_1c, pp.account_id): pp
              for pp in db.query(ProductPrice).filter(ProductPrice.uid_1c.in_(uids))}
    enabled = {(s.uid_1c, s.account_id) for s in db.query(SyncSetting).filter(
        SyncSetting.uid_1c.in_(uids), SyncSetting.enabled.is_(True))}
    rules = {a.id: get_or_create_rule(db, a.id) for a in accounts}
    rows = []
    for p in products:
        cells = []
        for a in accounts:
            pp = prices.get((p.uid_1c, a.id))
            decision = decide_price(p.cost_price, rules[a.id], pp.manual_price if pp else None,
                                    pp.last_sent_price if pp else None)
            cells.append({
                "account": a, "enabled": (p.uid_1c, a.id) in enabled,
                "manual": pp.manual_price if pp else None,
                "last_sent": pp.last_sent_price if pp else None,
                "target": decision.new_price, "block_reason": decision.block_reason,
                "note": decision.note,
            })
        rows.append({"product": p, "cells": cells})
    return rows


def _changes_query(db: Session, view: str, account_id: str):
    statuses = ([PriceChangeStatus.proposed, PriceChangeStatus.blocked] if view == "proposals" else
                [PriceChangeStatus.approved, PriceChangeStatus.sent, PriceChangeStatus.error,
                 PriceChangeStatus.rejected])
    query = db.query(PriceChange).filter(PriceChange.status.in_(statuses),
                                         PriceChange.is_test.is_(False))
    if (account_id or "").isdigit():
        query = query.filter(PriceChange.account_id == int(account_id))
    order = PriceChange.id.asc() if view == "proposals" else PriceChange.id.desc()
    return query.order_by(order)


def _render(request: Request, db: Session, user: User, template: str, view: str, q: str, account_id: str):
    view = view if view in VIEWS else "proposals"
    accounts = _accounts(db)
    ctx = {
        "request": request, "current_user": user, "active_page": "prices",
        "view": view, "q": q, "account_id": account_id, "accounts": accounts,
        "account_label": _account_label, "status_labels": STATUS_LABELS,
        "block_labels": BLOCK_LABELS, "BLOCK_FLOOR": BLOCK_FLOOR, "BLOCK_MAX_CHANGE": BLOCK_MAX_CHANGE,
        "rule_fields": RULE_FIELDS, "change_percent": change_percent,
        "open_count": db.query(PriceChange).filter(
            PriceChange.status.in_([PriceChangeStatus.proposed, PriceChangeStatus.blocked]),
            PriceChange.is_test.is_(False)).count(),
        "flash": pop_flash(request) if template == "prices.html" else None,
    }
    if view == "rules":
        ctx["rules"] = {a.id: get_or_create_rule(db, a.id) for a in accounts}
        db.commit()
        ctx["platforms"] = [p for p in Platform if any(a.platform == p for a in accounts)]
    elif view == "products":
        query = _products_query(db, q)
        ctx["total"] = query.count()
        products = query.limit(ROWS_LIMIT).all()
        ctx["rows"] = _product_rows(db, products, accounts)
        ctx["with_cost"] = sum(1 for p in products if p.cost_price is not None)
        db.commit()
    else:
        query = _changes_query(db, view, account_id)
        ctx["total"] = query.count()
        ctx["changes"] = query.limit(ROWS_LIMIT).all()
    ctx["rows_limit"] = ROWS_LIMIT
    return templates.TemplateResponse(request, template, ctx)


@router.get("/prices", response_class=HTMLResponse)
def prices_page(request: Request, view: str = Query("proposals"), q: str = Query(""),
                account_id: str = Query(""),
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _render(request, db, user, "prices.html", view, q, account_id)


@router.get("/prices/rows", response_class=HTMLResponse)
def prices_rows(request: Request, view: str = Query("products"), q: str = Query(""),
                account_id: str = Query(""),
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _render(request, db, user, "prices_rows.html", view, q, account_id)


def _parse_rule_value(raw: str, kind: str, lo, hi):
    raw = (raw or "").strip().replace(",", ".").replace(" ", "")
    if raw == "":
        raise ValueError("пусто")
    try:
        value = Decimal(raw) if kind == "decimal" else int(raw)
    except (InvalidOperation, ValueError):
        raise ValueError("не число")
    if kind == "decimal" and not value.is_finite():
        raise ValueError("не число")
    if value < lo or value > hi:
        raise ValueError(f"допустимо от {lo} до {hi}")
    return value


@router.post("/prices/rules/{account_id}")
async def save_rule(account_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        set_flash(request, "Кабинет не найден.", "warn")
        return _back("rules")
    form = await request.form()
    values, errors = {}, []
    for name, label, kind, lo, hi in RULE_FIELDS:
        try:
            values[name] = _parse_rule_value(form.get(name, ""), kind, lo, hi)
        except ValueError as e:
            errors.append(f"«{label}»: {e}")
    if not errors and values["round_minus"] >= values["round_step"]:
        errors.append("«Окончание: минус» должно быть меньше шага округления")
    if not errors and values["min_margin_percent"] > values["markup_percent"]:
        errors.append("минимальная наценка больше основной — все цены окажутся ниже пола")
    if errors:
        set_flash(request, f"{_account_label(account)}: правило не сохранено — " + "; ".join(errors), "warn")
        return _back("rules")

    rule = get_or_create_rule(db, account_id)
    before = {n: str(getattr(rule, n)) for n, *_ in RULE_FIELDS}
    for name, value in values.items():
        setattr(rule, name, value)
    log_action(db, user.username, "price_rule_saved",
               f"{_account_label(account)}: было {before}, стало { {n: str(v) for n, v in values.items()} }")
    db.commit()
    set_flash(request, f"Правило для {_account_label(account)} сохранено. Цены не изменились — "
                       f"нажмите «Рассчитать» на вкладке «Предложения».", "good")
    return _back("rules")


@router.post("/prices/recalculate")
def recalculate(request: Request, account_id: str = Form(""),
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    accounts = _accounts(db)
    if account_id.isdigit():
        accounts = [a for a in accounts if a.id == int(account_id)]
    totals = {"proposed": 0, "blocked": 0, "unchanged": 0, "no_cost": 0}
    for account in accounts:
        for k, v in recalculate_account(db, account).items():
            totals[k] += v
    log_action(db, user.username, "price_recalculated",
               f"{', '.join(_account_label(a) for a in accounts) or 'нет кабинетов'}: {totals}")
    db.commit()
    set_flash(request, f"Расчёт готов: предложено {totals['proposed']}, заблокировано ограничителями "
                       f"{totals['blocked']}, без изменений {totals['unchanged']}, "
                       f"не посчитано (нет себестоимости или правило не настроено) {totals['no_cost']}. На площадки ничего не отправлено.", "good")
    return _back("proposals", account_id=account_id)


def _selected_ids(form) -> list[int]:
    return [int(v) for v in form.getlist("ids") if str(v).isdigit()]


@router.post("/prices/approve")
async def approve_changes(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    form = await request.form()
    ids = _selected_ids(form)
    confirm_large = form.get("confirm_large") == "true"
    account_id = str(form.get("account_id") or "")
    if not ids:
        set_flash(request, "Ничего не выбрано.", "warn")
        return _back("proposals", account_id=account_id)

    approved, refused = 0, {}
    for change in db.query(PriceChange).filter(PriceChange.id.in_(ids), PriceChange.is_test.is_(False)):
        reason = approve(change, user.username, confirm_large=confirm_large)
        if reason is None:
            approved += 1
        else:
            refused[reason] = refused.get(reason, 0) + 1
    log_action(db, user.username, "price_approved",
               f"подтверждено {approved} (крупные изменения: {'да' if confirm_large else 'нет'}), "
               f"отказано {sum(refused.values())}, id={ids[:50]}")
    db.commit()

    message = f"Подтверждено: {approved}. Отправка на площадки — в течение пары минут."
    if refused:
        message += " Не подтверждено: " + "; ".join(f"{n} — {r}" for r, n in refused.items()) + "."
    set_flash(request, message, "warn" if refused else "good")
    return _back("proposals", account_id=account_id)


@router.post("/prices/reject")
async def reject_changes(request: Request, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    form = await request.form()
    ids = _selected_ids(form)
    account_id = str(form.get("account_id") or "")
    rejected = 0
    for change in db.query(PriceChange).filter(
            PriceChange.id.in_(ids), PriceChange.is_test.is_(False),
            PriceChange.status.in_([PriceChangeStatus.proposed, PriceChangeStatus.blocked,
                                    PriceChangeStatus.approved])):
        change.status = PriceChangeStatus.rejected
        change.decided_by = user.username
        change.decided_at = now_utc()
        rejected += 1
    log_action(db, user.username, "price_rejected", f"отклонено {rejected}, id={ids[:50]}")
    db.commit()
    set_flash(request, f"Отклонено: {rejected}.", "good" if rejected else "warn")
    return _back("proposals", account_id=account_id)


def _parse_manual(raw) -> tuple[bool, int | None]:
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


def _set_manual(db: Session, uid_1c: str, account_id: int, value: int | None) -> bool:
    """True, если значение изменилось."""
    pp = db.query(ProductPrice).filter(ProductPrice.uid_1c == uid_1c,
                                       ProductPrice.account_id == account_id).first()
    if pp is None:
        if value is None:
            return False
        pp = ProductPrice(uid_1c=uid_1c, account_id=account_id)
        db.add(pp)
        # autoflush выключен: без этого вторая строка файла с той же парой не
        # увидела бы первую и завела бы дубль (uq_price_product_account).
        db.flush()
    if pp.manual_price == value:
        return False
    pp.manual_price = value
    return True


@router.post("/prices/{uid_1c}/{account_id}/manual")
def set_manual_price(uid_1c: str, account_id: int, request: Request, value: str = Form(""),
                     q: str = Form(""), db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if product is None or account is None:
        set_flash(request, "Товар или кабинет не найден.", "warn")
        return _back("products", q=q)
    ok, parsed = _parse_manual(value)
    if not ok:
        set_flash(request, f"Ручная цена «{value}» не принята: нужно целое число рублей больше нуля "
                           f"(пусто — сбросить).", "warn")
        return _back("products", q=q)
    if _set_manual(db, uid_1c, account_id, parsed):
        log_action(db, user.username, "price_manual_set",
                   f"{product.article} {product.size or ''} / {_account_label(account)}: "
                   f"{parsed if parsed is not None else 'сброшена'}")
    db.commit()
    set_flash(request, "Ручная цена сохранена. На площадку она уйдёт после расчёта и подтверждения.", "good")
    return _back("products", q=q)


def _manual_header(a: PlatformAccount) -> str:
    return f"Ручная цена: {a.name} [{a.id}]"


@router.get("/prices/export")
def prices_export(view: str = Query("products"), q: str = Query(""), account_id: str = Query(""),
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    accounts = _accounts(db)
    if view in ("proposals", "log"):
        headers = ["ID_1С", "Артикул", "Наименование", "Размер", "Цвет", "Кабинет", "Себестоимость",
                   "Было", "Станет", "Изменение, %", "Статус", "Примечание", "Создано", "Отправлено"]
        data = []
        for c in _changes_query(db, view, account_id).all():
            pct = change_percent(c.old_price, c.new_price)
            data.append([c.uid_1c, c.product.article, c.product.name, c.product.size, c.product.color,
                         _account_label(c.account), float(c.cost_price) if c.cost_price is not None else None,
                         c.old_price, c.new_price, round(pct, 1) if pct is not None else None,
                         STATUS_LABELS[c.status], c.note or c.last_error or "",
                         format_dt(c.created_at), format_dt(c.sent_at)])
        return build_xlsx_response(headers, data, "предложения_цен.xlsx" if view == "proposals"
                                   else "журнал_цен.xlsx")

    headers = ["ID_1С", "Артикул", "Наименование", "Размер", "Цвет", "Себестоимость"]
    for a in accounts:
        headers += [_manual_header(a), f"Отправлено: {a.name} [{a.id}]", f"Расчёт: {a.name} [{a.id}]"]
    data = []
    for row in _product_rows(db, _products_query(db, q).all(), accounts):
        p = row["product"]
        line = [p.uid_1c, p.article, p.name, p.size, p.color,
                float(p.cost_price) if p.cost_price is not None else None]
        for cell in row["cells"]:
            line += [cell["manual"], cell["last_sent"], cell["target"]]
        data.append(line)
    db.commit()
    return build_xlsx_response(headers, data, "цены_товаров.xlsx")


@router.post("/prices/import")
def prices_import(request: Request, file: UploadFile = File(...),
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Импорт ручных цен из файла, выгруженного с вкладки «Цены товаров».
    Читаются ТОЛЬКО колонки «Ручная цена: <кабинет> [id]» — остальные
    (себестоимость, расчёт, отправлено) справочные и игнорируются. Пустая ячейка
    сбрасывает ручную цену; кабинет, чьей колонки в файле нет, не трогается.
    На площадки ничего не уходит — только после расчёта и подтверждения."""
    try:
        rows = read_xlsx_rows(file.file.read())
    except ExcelReadError as e:
        set_flash(request, str(e), "warn")
        return _back("products")

    accounts = _accounts(db)
    columns = {}
    if rows:
        for a in accounts:
            if _manual_header(a) in rows[0]:
                columns[a.id] = _manual_header(a)
    if not columns:
        set_flash(request, "В файле нет колонок «Ручная цена: …» — выгрузите файл с вкладки "
                           "«Цены товаров» и правьте его.", "warn")
        return _back("products")

    changed, errors = 0, []
    for i, row in enumerate(rows, start=2):
        uid_1c = str(row.get("ID_1С") or "").strip()
        if not uid_1c:
            continue
        if db.query(Product).filter(Product.uid_1c == uid_1c).first() is None:
            errors.append(f"строка {i}: товар {uid_1c} не найден")
            continue
        for account_id, header in columns.items():
            ok, value = _parse_manual(row.get(header))
            if not ok:
                errors.append(f"строка {i}, «{header}»: «{row.get(header)}» — не целое число рублей")
                continue
            if _set_manual(db, uid_1c, account_id, value):
                changed += 1

    log_action(db, user.username, "price_import", f"изменено ручных цен {changed}, ошибок {len(errors)}")
    db.commit()
    message = f"Ручных цен изменено: {changed}. На площадки ничего не отправлено — нужен расчёт и подтверждение."
    if errors:
        more = f" и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        set_flash(request, message + f" Ошибок: {len(errors)} ({'; '.join(errors[:5])}{more}).", "warn")
    else:
        set_flash(request, message, "good")
    return _back("products")

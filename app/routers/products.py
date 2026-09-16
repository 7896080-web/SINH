"""Единая страница «Товары и остатки» (/products).

Заменила две прежние — «Синхронизируемые товары» (отбор по кабинетам) и
«Управление остатками» (сколько передавать). Разделение было источником ошибок:
выключателей три и живут они на разных уровнях, а оператор видел их порознь и
считал, что товар транслируется, хотя на площадки уходили нули.

Здесь всё в одной строке, и главное — по каждому кабинету показано ЧИСЛО,
которое реально уйдёт, а если ноль, то прямая причина и что нажать. Расчёт
берётся из `app/transmit.py` — того же модуля, которым считает рассылка.
"""

from datetime import date, datetime

from fastapi import APIRouter, Request, Depends, Form, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Product, PlatformAccount, Platform, SyncSetting, User
from app.flash import set_flash, pop_flash
from app.audit import log_action
from app.timeutils import now_utc
from app.excel_utils import build_xlsx_response, read_xlsx_rows, parse_bool_ru
from app.transmit import explain, sku_quantity, enqueue_full_resend

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _active_accounts(db: Session) -> list[PlatformAccount]:
    return db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)) \
        .order_by(PlatformAccount.platform, PlatformAccount.name).all()


def _account_label(account: PlatformAccount) -> str:
    return f"{account.name} ({account.platform.value.upper()})"


def _get_product(db: Session, uid_1c: str) -> Product | None:
    return db.query(Product).options(
        joinedload(Product.sync_settings), joinedload(Product.barcodes),
    ).filter(Product.uid_1c == uid_1c).first()


def _repropagate(db: Session, product: Product, reason: str = "manual_enable"):
    """После правки резерва/порога/трансляции — переотправить актуальное значение
    на отмеченные кабинеты, не дожидаясь следующего изменения остатка."""
    for setting in product.sync_settings:
        if setting.enabled:
            enqueue_full_resend(db, product.uid_1c, setting.account_id, reason=reason)


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _row(product: Product, accounts: list[PlatformAccount]) -> dict:
    """Строка таблицы. По каждому кабинету — реальное число и причина нуля."""
    settings_map = {s.account_id: s for s in product.sync_settings}

    sku = explain(product, None, None)          # уровень SKU: только выключатель трансляции
    per_account = {}
    for account in accounts:
        setting = settings_map.get(account.id)
        result = explain(product, setting, account)
        per_account[account.id] = {
            "enabled": setting.enabled if setting else False,
            "has_proposal": setting.has_proposal if setting else False,
            "proposal_date": setting.proposal_date if setting else None,
            "min_threshold": setting.min_threshold if setting else 0,
            "quantity": result.quantity,
            "blocked": result.blocked,
            "reason": result.reason,
            "fix_hint": result.fix_hint,
        }

    return {
        "uid_1c": product.uid_1c, "article": product.article, "name": product.name,
        "size": product.size, "color": product.color,
        "stock_on_hand": product.stock_on_hand or 0, "reserve": product.reserve or 0,
        "broadcast_offset": product.broadcast_offset,
        "transmit_override": product.transmit_override,   # legacy: только предупреждение
        "broadcast_enabled": product.broadcast_enabled,
        "active_since": product.broadcast_active_since,
        "has_barcode": len(product.barcodes) > 0,
        "sku_quantity": sku_quantity(product),
        "sku_blocked": sku.blocked,
        "sku_reason": sku.reason,
        "accounts": per_account,
    }


def _load_products(db: Session, q: str, only_proposals: bool, only_blocked: bool,
                   accounts: list[PlatformAccount]) -> list[Product]:
    query = db.query(Product).options(joinedload(Product.sync_settings), joinedload(Product.barcodes))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.article.ilike(like), Product.name.ilike(like)))
    products = query.order_by(Product.name).limit(300).all()

    if only_proposals:
        products = [p for p in products if any(s.has_proposal for s in p.sync_settings)]
    if only_blocked:
        # «Ничего не уходит»: ни один отмеченный кабинет не получает положительное число.
        # Товары, не отмеченные нигде, сюда не попадают — это не проблема, а выбор оператора.
        kept = []
        for p in products:
            settings_map = {s.account_id: s for s in p.sync_settings}
            marked = [a for a in accounts if (settings_map.get(a.id) and settings_map[a.id].enabled)]
            if marked and all(explain(p, settings_map.get(a.id), a).quantity == 0 for a in marked):
                kept.append(p)
        products = kept
    return products


def _dispatch_summary(db: Session) -> dict:
    """Для мастер-переключателей: по каждой площадке — сколько кабинетов транслируют."""
    by_platform: dict[str, dict] = {}
    for a in db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all():
        d = by_platform.setdefault(a.platform.value, {"platform": a.platform.value, "total": 0, "on": 0})
        d["total"] += 1
        if a.dispatch_enabled:
            d["on"] += 1
    return {
        "platforms": list(by_platform.values()),
        "all_total": sum(d["total"] for d in by_platform.values()),
        "all_on": sum(d["on"] for d in by_platform.values()),
    }


def _render(request: Request, db: Session, user: User, q: str, only_proposals: bool,
            only_blocked: bool, template: str):
    accounts = _active_accounts(db)
    rows = [_row(p, accounts) for p in _load_products(db, q, only_proposals, only_blocked, accounts)]
    return templates.TemplateResponse(request, template, {
        "request": request, "current_user": user, "active_page": "products",
        "rows": rows, "q": q, "only_proposals": only_proposals, "only_blocked": only_blocked,
        "accounts": accounts, "account_label": _account_label,
        "dispatch": _dispatch_summary(db),
        "flash": pop_flash(request) if template == "products.html" else None,
    })


def _row_response(request: Request, db: Session, uid_1c: str):
    """Ответ HTMX: перерисованная строка (все числа пересчитываются заново)."""
    accounts = _active_accounts(db)
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "products_row.html", {
        "request": request, "row": _row(product, accounts), "accounts": accounts,
    })


def _back(q: str) -> RedirectResponse:
    return RedirectResponse(f"/products{'?q=' + q if q else ''}", status_code=303)


# --------------------------------------------------------------------------- страница

@router.get("/products", response_class=HTMLResponse)
def products_page(
    request: Request, q: str = Query(""), only_proposals: bool = Query(False),
    only_blocked: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    if not _active_accounts(db):
        set_flash(request, "Пока нет ни одного активного кабинета — добавьте его на странице «API-ключи».", "warn")
    return _render(request, db, user, q, only_proposals, only_blocked, "products.html")


@router.get("/products/rows", response_class=HTMLResponse)
def products_rows(
    request: Request, q: str = Query(""), only_proposals: bool = Query(False),
    only_blocked: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, q, only_proposals, only_blocked, "products_rows.html")


# Старые адреса — на новую страницу (в закладках и в переписке они ещё живут).
@router.get("/sync-products")
@router.get("/stock-control")
def legacy_redirect(q: str = Query("")):
    return RedirectResponse(f"/products{'?q=' + q if q else ''}", status_code=301)


# --------------------------------------------------------------------------- правки строки

@router.post("/products/{uid_1c}/reserve", response_class=HTMLResponse)
def set_reserve(
    request: Request, uid_1c: str, reserve: int = Form(0),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Резерв: столько штук держим у себя. Применяется только в автоматическом
    режиме — заданный порог трансляции считает от остатка ЦС напрямую."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    reserve = max(0, reserve)
    if product.reserve != reserve:
        product.reserve = reserve
        log_action(db, user.username, "reserve_changed", f"{uid_1c} -> {reserve}")
        _repropagate(db, product, reason="reserve_changed")
        db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/offset", response_class=HTMLResponse)
def set_offset(
    request: Request, uid_1c: str, value: str = Form(""),
    available: str = Form(""), stock_at_date: str = Form(""), clear: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Порог трансляции (`Product.broadcast_offset`) — постоянное расхождение между
    учётом 1С и реальностью склада. На площадки уходит max(0, остаток ЦС − порог).

    Задаётся двумя способами: числом напрямую (`value`) или расчётом от пересчёта
    (`stock_at_date` − `available`, может быть отрицательным). Порог фиксирован:
    заказы и сверка двигают только остаток, поэтому цифра не дрейфует — в отличие
    от прежнего ручного остатка, который приходилось поправлять руками."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)

    if clear or (not value.strip() and not available.strip() and not stock_at_date.strip()):
        product.broadcast_offset = None
        detail = "сброшен (автоматический расчёт от остатка и резерва)"
    else:
        try:
            if value.strip():
                offset = int(value.strip())
            else:
                offset = int(stock_at_date.strip()) - int(available.strip())
        except ValueError:
            set_flash(request, "Порог: введите целое число либо оба числа пересчёта.", "warn")
            return _row_response(request, db, uid_1c)
        product.broadcast_offset = offset
        product.transmit_override = None  # порог заменяет устаревшую ручную цифру
        detail = f"{offset}"

    log_action(db, user.username, "broadcast_offset_changed", f"{uid_1c} -> {detail}")
    _repropagate(db, product, reason="offset_changed")
    db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/clear-override", response_class=HTMLResponse)
def clear_override(
    request: Request, uid_1c: str,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Сброс устаревшего ручного остатка. Задать его из интерфейса больше нельзя —
    остался только сброс для товаров, где он проставлен с прошлых версий."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    if product.transmit_override is not None:
        product.transmit_override = None
        log_action(db, user.username, "transmit_override_cleared", uid_1c)
        _repropagate(db, product, reason="override_cleared")
        db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/broadcast", response_class=HTMLResponse)
def toggle_broadcast(
    request: Request, uid_1c: str, enabled: bool = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Трансляция SKU — главный выключатель товара. Выключено → на площадки уходит 0
    независимо от порогов и остатка. По умолчанию выключена у всех товаров."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    if product.broadcast_enabled != enabled:
        product.broadcast_enabled = enabled
        log_action(db, user.username, "broadcast_toggled", f"{uid_1c} -> {enabled}")
        _repropagate(db, product, reason="broadcast_toggled")
        db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/active-since", response_class=HTMLResponse)
def set_active_since(
    request: Request, uid_1c: str, value: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Дата старта задним числом — от неё стартует бэкфилл на «Тестировании»."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    try:
        product.broadcast_active_since = _parse_date(value)
    except ValueError:
        set_flash(request, "Дата должна быть в формате ГГГГ-ММ-ДД.", "warn")
        return _row_response(request, db, uid_1c)
    log_action(db, user.username, "active_since_changed", f"{uid_1c} -> {product.broadcast_active_since}")
    db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/{account_id}/toggle", response_class=HTMLResponse)
def toggle_sync(
    request: Request, uid_1c: str, account_id: int, enabled: bool = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Отметка кабинета: передавать ли этот товар в этот кабинет. Живой опрос
    заказов работает только по отмеченным парам товар+кабинет."""
    setting = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account_id,
    ).first()
    if setting is None:
        setting = SyncSetting(uid_1c=uid_1c, account_id=account_id)
        db.add(setting)

    was_enabled = setting.enabled
    setting.enabled = enabled
    if enabled and not was_enabled:
        setting.enabled_at = now_utc()
        setting.has_proposal = False
        enqueue_full_resend(db, uid_1c, account_id)
        log_action(db, user.username, "sync_enabled", f"{uid_1c} / кабинет #{account_id}")
    elif not enabled and was_enabled:
        log_action(db, user.username, "sync_disabled", f"{uid_1c} / кабинет #{account_id}")
    db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/{account_id}/threshold", response_class=HTMLResponse)
def set_threshold(
    request: Request, uid_1c: str, account_id: int, min_threshold: int = Form(0),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Порог кабинета — страховой буфер: если доступное количество не больше него,
    на эту площадку уходит 0. 0 = выключен. Не применяется, когда задан порог
    трансляции или ручной остаток: их оператор задал явно."""
    min_threshold = max(0, min_threshold)
    setting = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account_id,
    ).first()
    if setting is None:
        setting = SyncSetting(uid_1c=uid_1c, account_id=account_id)
        db.add(setting)
    setting.min_threshold = min_threshold
    log_action(db, user.username, "threshold_changed", f"{uid_1c} / кабинет #{account_id} -> {min_threshold}")
    db.commit()
    return _row_response(request, db, uid_1c)


# --------------------------------------------------------------------------- массовые действия

@router.post("/products/bulk")
def bulk_edit(
    request: Request, action: str = Form(...), uids: list[str] = Form(default=[]),
    int_value: str = Form(""), date_value: str = Form(""), q: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Массовая правка отмеченных строк: set_reserve | set_offset | clear_offset |
    broadcast_on | broadcast_off | set_active_since."""
    if not uids:
        set_flash(request, "Не выбрано ни одной строки.", "warn")
        return _back(q)

    n = d = None
    if action in ("set_reserve", "set_offset"):
        try:
            n = int((int_value or "").strip())
        except ValueError:
            set_flash(request, "Введите число для массовой правки.", "warn")
            return _back(q)
        if action == "set_reserve":
            n = max(0, n)          # резерв отрицательным не бывает, порог — бывает
    if action == "set_active_since":
        try:
            d = _parse_date(date_value)
        except ValueError:
            set_flash(request, "Дата должна быть в формате ГГГГ-ММ-ДД.", "warn")
            return _back(q)

    products = db.query(Product).options(joinedload(Product.sync_settings)) \
        .filter(Product.uid_1c.in_(uids)).all()

    changed = 0
    for p in products:
        if action == "set_reserve":
            p.reserve = n
        elif action == "set_offset":
            p.broadcast_offset = n
            p.transmit_override = None
        elif action == "clear_offset":
            p.broadcast_offset = None
        elif action == "broadcast_on":
            p.broadcast_enabled = True
        elif action == "broadcast_off":
            p.broadcast_enabled = False
        elif action == "set_active_since":
            p.broadcast_active_since = d
        else:
            set_flash(request, "Неизвестное действие.", "warn")
            return _back(q)
        if action != "set_active_since":
            _repropagate(db, p, reason="bulk_edit")
        changed += 1

    log_action(db, user.username, "products_bulk", f"{action} x{changed}")
    db.commit()
    set_flash(request, f"Массовая правка: изменено строк — {changed}.", "good")
    return _back(q)


@router.post("/products/dispatch-toggle")
def dispatch_toggle(
    request: Request, scope: str = Form(...), enabled: bool = Form(...), q: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Пауза рассылки на кабинеты: по площадке или по всем сразу. Пауза не трогает
    настройки товаров — очередь копится и уйдёт при включении."""
    query = db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True))
    if scope != "all":
        try:
            query = query.filter(PlatformAccount.platform == Platform(scope))
        except ValueError:
            set_flash(request, "Неизвестная площадка.", "warn")
            return _back(q)

    accounts = query.all()
    for a in accounts:
        a.dispatch_enabled = enabled
    log_action(db, user.username, "dispatch_toggle", f"scope={scope} enabled={enabled} x{len(accounts)}")
    db.commit()
    label = "всех площадок" if scope == "all" else scope.upper()
    state = "включена" if enabled else "выключена (пауза)"
    set_flash(request, f"Рассылка {label}: {state} — кабинетов затронуто {len(accounts)}.", "good")
    return _back(q)


# --------------------------------------------------------------------------- Excel

@router.get("/products/export")
def products_export(
    q: str = Query(""), only_proposals: bool = Query(False), only_blocked: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    accounts = _active_accounts(db)
    products = _load_products(db, q, only_proposals, only_blocked, accounts)

    headers = ["ID_1С", "Артикул", "Размер", "Цвет", "Наименование", "Остаток ЦС",
               "Резерв", "Порог трансляции", "Трансляция", "Уходит на площадки"]
    for account in accounts:
        headers.append(f"{_account_label(account)} — Синхронизировать")
        headers.append(f"{_account_label(account)} — Порог")

    data = []
    for product in products:
        settings_map = {s.account_id: s for s in product.sync_settings}
        row = [product.uid_1c, product.article, product.size, product.color, product.name,
               product.stock_on_hand, product.reserve,
               product.broadcast_offset if product.broadcast_offset is not None else "",
               "Да" if product.broadcast_enabled else "Нет",
               explain(product, None, None).quantity]
        for account in accounts:
            setting = settings_map.get(account.id)
            row.append("Да" if setting and setting.enabled else "Нет")
            row.append(setting.min_threshold if setting else 0)
        data.append(row)

    return build_xlsx_response(headers, data, "товары_и_остатки.xlsx")


@router.post("/products/import")
def products_import(
    request: Request, file: UploadFile = File(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Массовая правка по отредактированному файлу экспорта. Ключ — колонка ID_1С.
    Заголовки колонок кабинетов менять нельзя: по ним определяется кабинет.
    Колонка «Уходит на площадки» справочная, при импорте игнорируется."""
    accounts = _active_accounts(db)
    label_to_account = {_account_label(a): a for a in accounts}
    rows = read_xlsx_rows(file.file.read())

    updated, unchanged, errors = 0, 0, []

    for i, row in enumerate(rows, start=2):
        uid_1c = str(row.get("ID_1С") or "").strip()
        if not uid_1c:
            continue
        product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
        if product is None:
            errors.append(f"строка {i}: товар с ID {uid_1c} не найден")
            continue

        touched = False

        if "Резерв" in row:
            try:
                desired = max(0, int(row.get("Резерв") or 0))
            except (TypeError, ValueError):
                errors.append(f"строка {i}: некорректный резерв")
            else:
                if product.reserve != desired:
                    product.reserve = desired
                    touched = True

        if "Порог трансляции" in row:
            raw = row.get("Порог трансляции")
            raw = "" if raw is None else str(raw).strip()
            try:
                desired_offset = int(raw) if raw else None
            except ValueError:
                errors.append(f"строка {i}: некорректный порог трансляции")
            else:
                if product.broadcast_offset != desired_offset:
                    product.broadcast_offset = desired_offset
                    if desired_offset is not None:
                        product.transmit_override = None
                    touched = True

        if "Трансляция" in row:
            desired_broadcast = parse_bool_ru(row.get("Трансляция"))
            if product.broadcast_enabled != desired_broadcast:
                product.broadcast_enabled = desired_broadcast
                touched = True

        for label, account in label_to_account.items():
            sync_col = f"{label} — Синхронизировать"
            threshold_col = f"{label} — Порог"
            if sync_col not in row and threshold_col not in row:
                continue

            setting = db.query(SyncSetting).filter(
                SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account.id,
            ).first()
            current_enabled = setting.enabled if setting else False
            current_threshold = setting.min_threshold if setting else 0

            desired_enabled = parse_bool_ru(row.get(sync_col)) if sync_col in row else current_enabled
            try:
                desired_threshold = int(row.get(threshold_col) or 0) if threshold_col in row else current_threshold
            except (TypeError, ValueError):
                errors.append(f"строка {i}: некорректный порог в колонке «{threshold_col}»")
                continue

            if desired_enabled == current_enabled and desired_threshold == current_threshold:
                continue

            if setting is None:
                setting = SyncSetting(uid_1c=uid_1c, account_id=account.id)
                db.add(setting)
            if desired_enabled and not current_enabled:
                setting.enabled_at = now_utc()
                setting.has_proposal = False
                enqueue_full_resend(db, uid_1c, account.id)
            setting.enabled = desired_enabled
            setting.min_threshold = max(0, desired_threshold)
            touched = True

        if touched:
            updated += 1
            _repropagate(db, product, reason="excel_import")
        else:
            unchanged += 1

    log_action(db, user.username, "products_bulk_import_excel", f"updated={updated}")
    db.commit()

    message = f"Изменено строк: {updated}. Без изменений: {unchanged}."
    if errors:
        shown = "; ".join(errors[:5])
        more = f" и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        set_flash(request, f"{message} Ошибок: {len(errors)} ({shown}{more}).", "warn")
    else:
        set_flash(request, message, "good")
    return RedirectResponse("/products", status_code=303)

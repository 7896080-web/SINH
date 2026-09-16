from datetime import datetime
from app.timeutils import now_utc

from fastapi import APIRouter, Request, Depends, Query, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Product, SyncSetting, PlatformAccount, User, DispatchQueueItem
from app.excel_utils import build_xlsx_response, read_xlsx_rows, parse_bool_ru
from app.flash import set_flash, pop_flash
from app.audit import log_action

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def enqueue_full_resend(db: Session, uid_1c: str, account_id: int):
    """Разовая доотправка полного текущего остатка при включении синхронизации
    (раздел 10 спецификации). Кладёт запись в очередь рассылки — реальную
    отправку на площадку делает фоновый воркер app/workers/dispatch.py."""
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    quantity = product.stock_on_hand if product else 0

    db.add(DispatchQueueItem(
        uid_1c=uid_1c, account_id=account_id, quantity=quantity, reason="manual_enable",
    ))


def _active_accounts(db: Session) -> list[PlatformAccount]:
    return db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)) \
        .order_by(PlatformAccount.platform, PlatformAccount.name).all()


def _account_label(account: PlatformAccount) -> str:
    return f"{account.name} ({account.platform.value.upper()})"


def _get_settings_map(product: Product) -> dict:
    return {s.account_id: s for s in product.sync_settings}


def _load_products(db: Session, q: str, only_proposals: bool):
    query = db.query(Product).options(joinedload(Product.sync_settings), joinedload(Product.barcodes))

    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.article.ilike(like), Product.name.ilike(like)))

    products = query.order_by(Product.name).limit(300).all()

    if only_proposals:
        products = [p for p in products if any(s.has_proposal for s in p.sync_settings)]

    return products


def _row_for_product(product: Product, accounts: list[PlatformAccount]) -> dict:
    settings_map = _get_settings_map(product)
    account_states = {}
    for account in accounts:
        setting = settings_map.get(account.id)
        account_states[account.id] = {
            "enabled": setting.enabled if setting else False,
            "has_proposal": setting.has_proposal if setting else False,
            "proposal_date": setting.proposal_date if setting else None,
            "min_threshold": setting.min_threshold if setting else 0,
        }
    return {
        "uid_1c": product.uid_1c, "article": product.article, "name": product.name,
        "size": product.size, "color": product.color,
        "stock_on_hand": product.stock_on_hand, "reserve": product.reserve,
        "has_barcode": len(product.barcodes) > 0,
        "active_since": product.broadcast_active_since,
        "accounts": account_states,
    }


def _render(request: Request, db: Session, user: User, q: str, only_proposals: bool, template: str):
    accounts = _active_accounts(db)
    products = _load_products(db, q, only_proposals)
    rows = [_row_for_product(p, accounts) for p in products]

    return templates.TemplateResponse(request, template, {
        "request": request, "current_user": user, "active_page": "sync-products",
        "rows": rows, "q": q, "only_proposals": only_proposals, "accounts": accounts,
        "account_label": _account_label,
        "flash": pop_flash(request) if template == "sync_products.html" else None,
    })


@router.get("/sync-products", response_class=HTMLResponse)
def sync_products_page(
    request: Request, q: str = Query(""), only_proposals: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    if not _active_accounts(db):
        set_flash(request, "Пока нет ни одного активного кабинета — добавьте его на странице «API-ключи».", "warn")
    return _render(request, db, user, q, only_proposals, "sync_products.html")


@router.get("/sync-products/rows", response_class=HTMLResponse)
def sync_products_rows(
    request: Request, q: str = Query(""), only_proposals: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, q, only_proposals, "sync_products_rows.html")


@router.post("/sync-products/{uid_1c}/{account_id}/toggle", response_class=HTMLResponse)
def toggle_sync(
    request: Request, uid_1c: str, account_id: int,
    enabled: bool = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
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

    accounts = _active_accounts(db)
    product = db.query(Product).options(
        joinedload(Product.sync_settings), joinedload(Product.barcodes)
    ).filter(Product.uid_1c == uid_1c).first()
    row = _row_for_product(product, accounts)

    return templates.TemplateResponse(request, "sync_products_row.html", {
        "request": request, "row": row, "accounts": accounts,
    })


@router.post("/sync-products/{uid_1c}/active-since", response_class=HTMLResponse)
def sync_active_since(
    request: Request, uid_1c: str, value: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Единая ручная дата старта задним числом (Product.broadcast_active_since) —
    общая для всех площадок; та же, что на «Управлении остатками» и /testing."""
    product = db.query(Product).options(
        joinedload(Product.sync_settings), joinedload(Product.barcodes)
    ).filter(Product.uid_1c == uid_1c).first()
    if product is None:
        return HTMLResponse("", status_code=404)
    v = (value or "").strip()
    if v:
        try:
            product.broadcast_active_since = datetime.strptime(v, "%Y-%m-%d").date()
        except ValueError:
            pass
    else:
        product.broadcast_active_since = None
    log_action(db, user.username, "active_since_changed", f"{uid_1c} -> {product.broadcast_active_since}")
    db.commit()

    accounts = _active_accounts(db)
    row = _row_for_product(product, accounts)
    return templates.TemplateResponse(request, "sync_products_row.html", {
        "request": request, "row": row, "accounts": accounts,
    })


@router.post("/sync-products/{uid_1c}/{account_id}/threshold", response_class=HTMLResponse)
def update_threshold(
    request: Request, uid_1c: str, account_id: int,
    min_threshold: int = Form(0),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    if min_threshold < 0:
        min_threshold = 0

    setting = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account_id,
    ).first()

    if setting is None:
        setting = SyncSetting(uid_1c=uid_1c, account_id=account_id)
        db.add(setting)

    setting.min_threshold = min_threshold
    log_action(db, user.username, "threshold_changed", f"{uid_1c} / кабинет #{account_id} -> {min_threshold}")
    db.commit()

    accounts = _active_accounts(db)
    product = db.query(Product).options(
        joinedload(Product.sync_settings), joinedload(Product.barcodes)
    ).filter(Product.uid_1c == uid_1c).first()
    row = _row_for_product(product, accounts)

    return templates.TemplateResponse(request, "sync_products_row.html", {
        "request": request, "row": row, "accounts": accounts,
    })


@router.post("/sync-products/{uid_1c}/reserve", response_class=HTMLResponse)
def update_reserve(
    request: Request, uid_1c: str,
    reserve: int = Form(0),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Резерв на товар (общий на все каналы): столько штук держим у себя и не
    отдаём на площадки. На площадку уходит max(0, остаток − резерв)."""
    if reserve < 0:
        reserve = 0

    product = db.query(Product).options(
        joinedload(Product.sync_settings), joinedload(Product.barcodes)
    ).filter(Product.uid_1c == uid_1c).first()
    if product is None:
        return HTMLResponse("", status_code=404)

    if product.reserve != reserve:
        product.reserve = reserve
        log_action(db, user.username, "reserve_changed", f"{uid_1c} -> {reserve}")
        db.commit()
        # Переотправляем актуальный остаток за вычетом нового резерва на все
        # кабинеты, где синхронизация включена — иначе площадки узнают о новом
        # резерве только со следующего изменения остатка.
        for setting in product.sync_settings:
            if setting.enabled:
                enqueue_full_resend(db, uid_1c, setting.account_id)
        db.commit()

    accounts = _active_accounts(db)
    row = _row_for_product(product, accounts)
    return templates.TemplateResponse(request, "sync_products_row.html", {
        "request": request, "row": row, "accounts": accounts,
    })


@router.get("/sync-products/export")
def sync_products_export(
    q: str = Query(""), only_proposals: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    accounts = _active_accounts(db)
    products = _load_products(db, q, only_proposals)

    headers = ["ID_1С", "Артикул", "Наименование", "Остаток", "Резерв"]
    for account in accounts:
        headers.append(f"{_account_label(account)} — Синхронизировать")
        headers.append(f"{_account_label(account)} — Порог")

    data = []
    for product in products:
        settings_map = _get_settings_map(product)
        row = [product.uid_1c, product.article, product.name, product.stock_on_hand, product.reserve]
        for account in accounts:
            setting = settings_map.get(account.id)
            row.append("Да" if setting and setting.enabled else "Нет")
            row.append(setting.min_threshold if setting else 0)
        data.append(row)

    return build_xlsx_response(headers, data, "синхронизируемые_товары.xlsx")


@router.post("/sync-products/import")
def sync_products_import(
    request: Request, file: UploadFile = File(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Массовое включение/выключение синхронизации и изменение порога по
    отредактированному файлу. Колонка ID_1С — ключ сопоставления. Колонки
    вида "<кабинет> — Синхронизировать" (Да/Нет) и "<кабинет> — Порог" (число)
    должны сохранить свои заголовки такими, какими их отдал экспорт —
    именно по заголовку определяется, какому кабинету принадлежит колонка."""

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

        if "Резерв" in row:
            try:
                desired_reserve = max(0, int(row.get("Резерв") or 0))
            except (TypeError, ValueError):
                errors.append(f"строка {i}: некорректный резерв в колонке «Резерв»")
            else:
                if product.reserve != desired_reserve:
                    product.reserve = desired_reserve
                    updated += 1
                    for setting in product.sync_settings:
                        if setting.enabled:
                            enqueue_full_resend(db, uid_1c, setting.account_id)

        for label, account in label_to_account.items():
            sync_col = f"{label} — Синхронизировать"
            threshold_col = f"{label} — Порог"
            if sync_col not in row and threshold_col not in row:
                continue

            setting = db.query(SyncSetting).filter(
                SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account.id,
            ).first()

            desired_enabled = parse_bool_ru(row.get(sync_col)) if sync_col in row else (setting.enabled if setting else False)
            try:
                desired_threshold = int(row.get(threshold_col) or 0) if threshold_col in row else (setting.min_threshold if setting else 0)
            except (TypeError, ValueError):
                errors.append(f"строка {i}: некорректный порог в колонке «{threshold_col}»")
                continue

            current_enabled = setting.enabled if setting else False
            current_threshold = setting.min_threshold if setting else 0

            if desired_enabled == current_enabled and desired_threshold == current_threshold:
                unchanged += 1
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
            updated += 1

    log_action(db, user.username, "sync_products_bulk_import_excel", f"updated={updated}")
    db.commit()

    message = f"Изменено строк: {updated}. Без изменений: {unchanged}."
    if errors:
        shown = "; ".join(errors[:5])
        more = f" и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        message += f" Ошибок: {len(errors)} ({shown}{more})."
        set_flash(request, message, "warn")
    else:
        set_flash(request, message, "good")

    return RedirectResponse("/sync-products", status_code=303)

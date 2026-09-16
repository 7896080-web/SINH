"""Страница «Управление остатками» (Фаза 1).

По каждому размер-цвет SKU: Остаток ЦС (из 1С, только чтение), Резерв,
Передаваемый остаток (override — залипающий ручной, NULL = расчёт), Трансляция
(вкл/выкл на SKU: выкл → на площадки уходит 0), Активно с (дата — аудит/backfill).

Построчная и массовая правка. Плюс мастер-переключатели трансляции на уровне
кабинетов: по площадке (все кабинеты площадки) и по всем сразу — это пауза
рассылки (`PlatformAccount.dispatch_enabled`), отдельная от per-SKU-трансляции.
"""
from datetime import date

from fastapi import APIRouter, Request, Depends, Query, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Product, PlatformAccount, Platform, User
from app.flash import set_flash, pop_flash
from app.audit import log_action
from app.routers.sync_products import enqueue_full_resend

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def sku_transmit(product: Product) -> int:
    """Передаваемый остаток на уровне SKU (без учёта порога кабинета — он
    применяется отдельно на странице «Синхронизируемые товары»)."""
    if not product.broadcast_enabled:
        return 0
    # Порог трансляции — «ЦС − порог» (порог может быть ±), приоритетнее override.
    if product.broadcast_offset is not None:
        return max(0, (product.stock_on_hand or 0) - product.broadcast_offset)
    if product.transmit_override is not None:
        return max(0, product.transmit_override)
    return max(0, (product.stock_on_hand or 0) - (product.reserve or 0))


def _repropagate(db: Session, product: Product):
    """После изменения резерва/override/трансляции — переотправить актуальное
    значение на все кабинеты, где синхронизация SKU включена, чтобы площадки
    узнали новое значение не дожидаясь следующего изменения остатка."""
    for setting in product.sync_settings:
        if setting.enabled:
            enqueue_full_resend(db, product.uid_1c, setting.account_id)


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return date.fromisoformat(raw)  # ГГГГ-ММ-ДД; исключение → 422/обработка выше


def _load_products(db: Session, q: str) -> list[Product]:
    query = db.query(Product).options(
        joinedload(Product.sync_settings), joinedload(Product.barcodes),
    )
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.article.ilike(like), Product.name.ilike(like)))
    return query.order_by(Product.name).limit(300).all()


def _dispatch_summary(db: Session) -> dict:
    """Для мастер-переключателей: по каждой площадке — сколько кабинетов
    транслируют из активных, и общий итог."""
    accounts = db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all()
    by_platform: dict[str, dict] = {}
    for a in accounts:
        p = a.platform.value
        d = by_platform.setdefault(p, {"platform": p, "total": 0, "on": 0})
        d["total"] += 1
        if a.dispatch_enabled:
            d["on"] += 1
    total = sum(d["total"] for d in by_platform.values())
    on = sum(d["on"] for d in by_platform.values())
    return {"platforms": list(by_platform.values()), "all_total": total, "all_on": on}


def _render(request: Request, db: Session, user: User, q: str):
    products = _load_products(db, q)
    rows = [{
        "uid_1c": p.uid_1c, "article": p.article, "name": p.name,
        "size": p.size, "color": p.color,
        "stock_on_hand": p.stock_on_hand or 0, "reserve": p.reserve or 0,
        "transmit_override": p.transmit_override,
        "transmit": sku_transmit(p),
        "broadcast_enabled": p.broadcast_enabled,
        "active_since": p.broadcast_active_since,
        "has_barcode": len(p.barcodes) > 0,
    } for p in products]

    return templates.TemplateResponse(request, "stock_control.html", {
        "request": request, "current_user": user, "active_page": "stock-control",
        "rows": rows, "q": q, "dispatch": _dispatch_summary(db),
        "flash": pop_flash(request),
    })


@router.get("/stock-control", response_class=HTMLResponse)
def stock_control_page(
    request: Request, q: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, q)


def _back(q: str) -> RedirectResponse:
    suffix = f"?q={q}" if q else ""
    return RedirectResponse(f"/stock-control{suffix}", status_code=303)


def _get_product(db: Session, uid_1c: str) -> Product | None:
    return db.query(Product).options(
        joinedload(Product.sync_settings), joinedload(Product.barcodes),
    ).filter(Product.uid_1c == uid_1c).first()


@router.post("/stock-control/row/{uid_1c}/reserve")
def row_reserve(
    request: Request, uid_1c: str, reserve: int = Form(0), q: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    reserve = max(0, reserve)
    if product.reserve != reserve:
        product.reserve = reserve
        log_action(db, user.username, "reserve_changed", f"{uid_1c} -> {reserve}")
        _repropagate(db, product)
        db.commit()
    return _back(q)


@router.post("/stock-control/row/{uid_1c}/override")
def row_override(
    request: Request, uid_1c: str, value: str = Form(""), q: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Пустое значение → сбросить override (снова автоматический расчёт)."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)

    raw = (value or "").strip()
    if raw == "":
        new_override = None
    else:
        try:
            new_override = max(0, int(raw))
        except ValueError:
            set_flash(request, f"«{raw}» — не число, передаваемый остаток не изменён.", "warn")
            return _back(q)

    if product.transmit_override != new_override:
        product.transmit_override = new_override
        log_action(db, user.username, "transmit_override_changed", f"{uid_1c} -> {new_override}")
        _repropagate(db, product)
        db.commit()
    return _back(q)


@router.post("/stock-control/row/{uid_1c}/broadcast")
def row_broadcast(
    request: Request, uid_1c: str, enabled: bool = Form(...), q: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    if product.broadcast_enabled != enabled:
        product.broadcast_enabled = enabled
        log_action(db, user.username, "broadcast_toggled", f"{uid_1c} -> {enabled}")
        _repropagate(db, product)
        db.commit()
    return _back(q)


@router.post("/stock-control/row/{uid_1c}/active-since")
def row_active_since(
    request: Request, uid_1c: str, value: str = Form(""), q: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    try:
        new_date = _parse_date(value)
    except ValueError:
        set_flash(request, "Дата должна быть в формате ГГГГ-ММ-ДД.", "warn")
        return _back(q)
    product.broadcast_active_since = new_date
    log_action(db, user.username, "active_since_changed", f"{uid_1c} -> {new_date}")
    db.commit()
    return _back(q)


@router.post("/stock-control/bulk")
def bulk_edit(
    request: Request,
    action: str = Form(...),
    uids: list[str] = Form(default=[]),
    int_value: str = Form(""),
    date_value: str = Form(""),
    q: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Массовая правка выделенных строк. action:
    set_reserve | set_override | clear_override | broadcast_on | broadcast_off | set_active_since."""
    if not uids:
        set_flash(request, "Не выбрано ни одной строки.", "warn")
        return _back(q)

    products = db.query(Product).options(joinedload(Product.sync_settings)) \
        .filter(Product.uid_1c.in_(uids)).all()

    changed = 0
    if action in ("set_reserve", "set_override"):
        try:
            n = max(0, int((int_value or "").strip()))
        except ValueError:
            set_flash(request, "Введите число для массовой правки.", "warn")
            return _back(q)

    if action == "set_active_since":
        try:
            d = _parse_date(date_value)
        except ValueError:
            set_flash(request, "Дата должна быть в формате ГГГГ-ММ-ДД.", "warn")
            return _back(q)

    for p in products:
        if action == "set_reserve":
            p.reserve = n
        elif action == "set_override":
            p.transmit_override = n
        elif action == "clear_override":
            p.transmit_override = None
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
            _repropagate(db, p)
        changed += 1

    log_action(db, user.username, "stock_control_bulk", f"{action} x{changed}")
    db.commit()
    set_flash(request, f"Массовая правка: изменено строк — {changed}.", "good")
    return _back(q)


@router.post("/stock-control/dispatch-toggle")
def dispatch_toggle(
    request: Request,
    scope: str = Form(...),        # "all" | значение площадки (wb/ozon/kit)
    enabled: bool = Form(...),
    q: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Мастер-пауза трансляции на кабинеты: по площадке или по всем сразу."""
    query = db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True))
    if scope != "all":
        try:
            platform = Platform(scope)
        except ValueError:
            set_flash(request, "Неизвестная площадка.", "warn")
            return _back(q)
        query = query.filter(PlatformAccount.platform == platform)

    accounts = query.all()
    for a in accounts:
        a.dispatch_enabled = enabled
    log_action(db, user.username, "dispatch_toggle", f"scope={scope} enabled={enabled} x{len(accounts)}")
    db.commit()

    label = "всех площадок" if scope == "all" else scope.upper()
    state = "включена" if enabled else "выключена (пауза)"
    set_flash(request, f"Трансляция {label}: {state} — кабинетов затронуто {len(accounts)}.", "good")
    return _back(q)

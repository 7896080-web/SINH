from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.models import ApiCredential, PlatformAccount, Platform, User
from app.crypto import encrypt_value, decrypt_value, mask_value
from app.audit import log_action
from app.flash import set_flash, pop_flash
from app.workers.scheduler import PENDING_WAREHOUSE_NAME, SOLD_WAREHOUSE_NAME, SOURCE_WAREHOUSE_NAME

router = APIRouter()
templates = shared_templates

# Поля, которые нужны любому кабинету данной площадки.
PLATFORM_FIELDS = {
    Platform.wb: [("token", "API-токен (категория Marketplace)")],
    Platform.ozon: [
        ("client_id", "Client-Id"),
        ("api_key", "Api-Key"),
    ],
    Platform.kit: [("token", "API-токен")],
}

PLATFORM_LABELS = {
    Platform.wb: "Wildberries",
    Platform.ozon: "Ozon",
    Platform.kit: "Яндекс KIT",
}


def _required_1c_warehouses(db: Session) -> list[dict]:
    """Список складов, которые нужно завести (или переименовать в
    точности так) в старой базе 1С, чтобы код мог их найти по имени —
    раздел 2 спецификации. «ЦС» нужен всегда, «<Площадка>.Ожидает» — по
    одному на площадку, для которой сейчас есть хотя бы один активный
    кабинет (общий на все кабинеты этой площадки, не по одному на кабинет —
    см. примечание в scheduler.py)."""

    warehouses = [{
        "name": SOURCE_WAREHOUSE_NAME, "purpose": "Основной физический склад — источник истины для всех площадок.",
        "used_by": "все кабинеты",
    }]

    active_accounts = db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all()
    platforms_in_use = sorted({a.platform for a in active_accounts}, key=lambda p: p.value)

    for platform in platforms_in_use:
        account_names = [a.name for a in active_accounts if a.platform == platform]
        used_by = ", ".join(account_names)
        warehouses.append({
            "name": PENDING_WAREHOUSE_NAME.get(platform, f"{platform.value}.Ожидает"),
            "purpose": f"Резерв под заказы «ожидает подтверждения» для {PLATFORM_LABELS[platform]}.",
            "used_by": used_by,
        })
        warehouses.append({
            "name": SOLD_WAREHOUSE_NAME.get(platform, f"Склад {platform.value.upper()}"),
            "purpose": f"Склад продаж {PLATFORM_LABELS[platform]} — товар при подтверждении/отгрузке.",
            "used_by": used_by,
        })

    return warehouses


def _ensure_credential_rows(db: Session, account: PlatformAccount):
    """Создаёт пустые записи под все обязательные поля этого кабинета,
    если их ещё нет — чтобы форма всегда рисовала полный набор полей."""
    existing = {c.field_name for c in account.credentials}
    changed = False
    for field_name, field_label in PLATFORM_FIELDS.get(account.platform, []):
        if field_name not in existing:
            db.add(ApiCredential(account_id=account.id, field_name=field_name, field_label=field_label))
            changed = True
    if changed:
        db.commit()
        db.refresh(account)


@router.get("/api-keys", response_class=HTMLResponse)
def api_keys_page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    accounts = db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name).all()
    for account in accounts:
        _ensure_credential_rows(db, account)

    account_cards = []
    for account in accounts:
        fields = []
        for cred in sorted(account.credentials, key=lambda c: c.field_name):
            plain = decrypt_value(cred.encrypted_value) if cred.encrypted_value else ""
            fields.append({
                "field_name": cred.field_name, "field_label": cred.field_label,
                "masked": mask_value(plain), "is_set": bool(plain), "updated_at": cred.updated_at,
            })
        account_cards.append({
            "id": account.id, "name": account.name, "platform": account.platform.value,
            "platform_label": PLATFORM_LABELS[account.platform],
            "warehouse_id": account.warehouse_id or "", "is_active": account.is_active,
            "publish_hidden_on_stock": account.publish_hidden_on_stock,
            "supports_publish": account.platform == Platform.kit,
            "fields": fields,
        })

    return templates.TemplateResponse(request, "api_keys.html", {
        "request": request, "current_user": user, "active_page": "api-keys",
        "accounts": account_cards, "platforms": list(Platform), "platform_labels": PLATFORM_LABELS,
        "required_warehouses": _required_1c_warehouses(db),
        "flash": pop_flash(request),
    })


@router.post("/api-keys/accounts/create")
def create_account(
    request: Request,
    platform: str = Form(...), name: str = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    name = name.strip()
    try:
        platform_enum = Platform(platform)
    except ValueError:
        set_flash(request, f"Неизвестная площадка «{platform}».", "warn")
        return RedirectResponse("/api-keys", status_code=303)

    if not name:
        set_flash(request, "Название кабинета не может быть пустым.", "warn")
        return RedirectResponse("/api-keys", status_code=303)

    account = PlatformAccount(platform=platform_enum, name=name)
    db.add(account)
    db.commit()
    db.refresh(account)
    _ensure_credential_rows(db, account)

    log_action(db, user.username, "account_created", f"{platform}: {name}")
    db.commit()

    set_flash(request, f"Кабинет «{name}» ({PLATFORM_LABELS[platform_enum]}) добавлен — заполните ключи ниже.", "good")
    return RedirectResponse("/api-keys", status_code=303)


@router.post("/api-keys/accounts/{account_id}/deactivate")
def deactivate_account(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Не удаляем — деактивируем: история (заказы, аномалии, лог) должна
    остаться читаемой, а воркеры просто перестают опрашивать этот кабинет."""
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/api-keys", status_code=303)

    account.is_active = False
    log_action(db, user.username, "account_deactivated", account.name)
    db.commit()

    set_flash(request, f"Кабинет «{account.name}» отключён — задания синхронизации по нему больше не создаются.", "good")
    return RedirectResponse("/api-keys", status_code=303)


@router.post("/api-keys/accounts/{account_id}/activate")
def activate_account(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/api-keys", status_code=303)

    account.is_active = True
    log_action(db, user.username, "account_activated", account.name)
    db.commit()

    set_flash(request, f"Кабинет «{account.name}» снова активен.", "good")
    return RedirectResponse("/api-keys", status_code=303)


@router.post("/api-keys/accounts/{account_id}/warehouse")
def update_warehouse(
    request: Request, account_id: int,
    warehouse_id: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/api-keys", status_code=303)

    was = account.warehouse_id
    account.warehouse_id = warehouse_id.strip() or None
    if account.warehouse_id != was:
        # Склад кабинета определяет, куда уходит остаток и какое перемещение
        # создаётся в 1С, — менять его молча нельзя.
        log_action(db, user.username, "warehouse_changed",
                   f"{account.name}: «{was or '—'}» -> «{account.warehouse_id or '—'}»")
    db.commit()

    set_flash(request, f"Склад для «{account.name}» обновлён.", "good")
    return RedirectResponse("/api-keys", status_code=303)


@router.post("/api-keys/accounts/{account_id}/publish-hidden")
def update_publish_hidden(
    request: Request, account_id: int,
    publish_hidden_on_stock: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Возвращать ли на витрину карточки, спрятанные площадкой за нулевой остаток.

    Пишется в журнал: это действие наружу — после включения мы сами меняем
    статус чужих карточек, и человек должен потом видеть, кто и когда разрешил.
    """
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/api-keys", status_code=303)

    was = account.publish_hidden_on_stock
    account.publish_hidden_on_stock = publish_hidden_on_stock == "on"
    if account.publish_hidden_on_stock != was:
        log_action(db, user.username, "publish_hidden_changed",
                   f"{account.name}: автопубликация скрытых карточек "
                   f"{'включена' if account.publish_hidden_on_stock else 'выключена'}")
    db.commit()

    set_flash(request, (
        f"Скрытые карточки «{account.name}» будут возвращаться на витрину при ненулевом остатке."
        if account.publish_hidden_on_stock else
        f"Автопубликация скрытых карточек «{account.name}» выключена."
    ), "good")
    return RedirectResponse("/api-keys", status_code=303)


@router.post("/api-keys/accounts/{account_id}/{field_name}")
def update_credential(
    request: Request, account_id: int, field_name: str,
    value: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/api-keys", status_code=303)

    valid_field_names = {name for name, _ in PLATFORM_FIELDS.get(account.platform, [])}
    if field_name not in valid_field_names:
        return RedirectResponse("/api-keys", status_code=303)

    cred = db.query(ApiCredential).filter(
        ApiCredential.account_id == account_id, ApiCredential.field_name == field_name,
    ).first()
    if cred is None:
        field_label = dict(PLATFORM_FIELDS[account.platform])[field_name]
        cred = ApiCredential(account_id=account_id, field_name=field_name, field_label=field_label)
        db.add(cred)

    had_value = cred.encrypted_value is not None
    cred.encrypted_value = encrypt_value(value) if value else None
    # В журнал — только ФАКТ и имя поля. Само значение (боевой токен площадки) в
    # журнале действий не место: он открыт любому пользователю админки.
    log_action(db, user.username,
               "credential_changed" if value else "credential_cleared",
               f"{account.name} / {cred.field_label}" + ("" if value or had_value else " (было пусто)"))
    db.commit()

    return RedirectResponse("/api-keys", status_code=303)

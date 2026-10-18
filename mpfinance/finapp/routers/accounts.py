"""Страница «API-ключи»: кабинеты площадок, их ключи и связь с кабинетом Sync Admin.

Ключи хранятся ЗАШИФРОВАННЫМИ своим ключом программы и на страницу не
выводятся — только маска. Пустое поле при сохранении ключ не стирает.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from finapp import accounts as acc, syncdb
from finapp.audit import log_action
from finapp.finance import fetch
from finapp.crypto import mask_value
from finapp.database import get_db
from finapp.deps import get_current_user, posted_form
from finapp.flash import pop_flash, set_flash
from finapp.models import PLATFORMS, PlatformAccount, User
from finapp.security import verify_password
from finapp.templating import templates

router = APIRouter()


def _back():
    return RedirectResponse("/api-keys", status_code=303)


@router.get("/api-keys", response_class=HTMLResponse)
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        sync_accounts, sync_error = syncdb.accounts(), ""
    except syncdb.SyncDbError as e:
        sync_accounts, sync_error = [], str(e)
    rows = []
    for a in db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name):
        creds = acc.credentials(db, a)
        bad = acc.unreadable_fields(db, a)
        rows.append({
            "account": a,
            "fields": [(f, label, acc.UNREADABLE if f in bad else mask_value(creds.get(f, "")))
                       for f, label in acc.CREDENTIAL_FIELDS[a.platform]],
            "sync_choices": [s for s in sync_accounts if s.platform == a.platform],
        })
    return templates.TemplateResponse(request, "accounts.html", {
        "current_user": user, "active_page": "accounts", "flash": pop_flash(request),
        "rows": rows, "platforms": PLATFORMS, "sync_error": sync_error,
        "sync_names": {s.id: s.name for s in sync_accounts},
    })


@router.post("/api-keys/new")
def create(request: Request, platform: str = Form(""), name: str = Form(""),
           db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    name = name.strip()[:128]
    if platform not in PLATFORMS or not name:
        set_flash(request, "Выберите площадку и введите название кабинета.", "warn")
        return _back()
    account = PlatformAccount(platform=platform, name=name)
    db.add(account)
    db.flush()
    log_action(db, user.username, "account_created", f"{name} ({platform})")
    db.commit()
    set_flash(request, f"Кабинет «{name}» заведён.", "good")
    return _back()


@router.post("/api-keys/{account_id}")
def save(account_id: int, request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
         user: User = Depends(get_current_user)):
    account = db.get(PlatformAccount, account_id)
    if account is None:
        set_flash(request, "Кабинет не найден.", "warn")
        return _back()
    name = str(form.get("name") or "").strip()[:128]
    if name:
        account.name = name
    account.is_active = form.get("is_active") == "1"
    sync_raw = str(form.get("sync_account_id") or "").strip()
    account.sync_account_id = int(sync_raw) if sync_raw.isdigit() else None
    # Браузер подставляет сохранённый пароль входа в поле ключа сам (форма с
    # полем-паролем похожа на форму входа). Пароль программы ключом площадки не
    # бывает никогда — такое значение не сохраняем.
    changed, autofilled = [], []
    for field, label in acc.CREDENTIAL_FIELDS[account.platform]:
        value = str(form.get(field) or "")
        if value.strip() and verify_password(value.strip(), user.password_hash):
            autofilled.append(label)
            continue
        if acc.set_credential(db, account, field, value):
            changed.append(field)
    log_action(db, user.username, "account_saved",
               f"{account.name}: активен={account.is_active}, кабинет Sync Admin="
               f"{account.sync_account_id or '—'}, изменены ключи: {', '.join(changed) or 'нет'}")
    db.commit()
    if autofilled:
        set_flash(request, f"«{account.name}» сохранён, но в поле «{'», «'.join(autofilled)}» браузер "
                           f"подставил ваш пароль входа — поле НЕ изменено. Впишите ключ из кабинета "
                           f"площадки.", "warn")
    else:
        set_flash(request, f"«{account.name}» сохранён."
                  + (f" Изменены ключи: {', '.join(changed)}." if changed else ""), "good")
    return _back()


@router.post("/api-keys/{account_id}/check")
def check(account_id: int, request: Request, db: Session = Depends(get_db),
          user: User = Depends(get_current_user)):
    account = db.get(PlatformAccount, account_id)
    if account is None:
        return _back()
    ok, message = fetch.check_access(db, account)
    log_action(db, user.username, "account_checked", f"{account.name}: {'ок' if ok else 'ошибка'} — {message}")
    db.commit()
    set_flash(request, f"{account.name}: {message}", "good" if ok else "warn")
    return _back()

"""Страница «API-ключи»: кабинеты площадок, их ключи и связь с кабинетом Sync Admin.

Ключи хранятся ЗАШИФРОВАННЫМИ своим ключом программы и на страницу не
выводятся — только маска. Пустое поле при сохранении ключ не стирает.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from finapp import accounts as acc, mail, settings, syncdb
from finapp.audit import log_action
from finapp.finance import fetch
from finapp.crypto import mask_value
from finapp.database import get_db
from finapp.deps import get_current_user, posted_form
from finapp.flash import pop_flash, set_flash
from finapp.models import (PLATFORMS, ApiCredential, KitVariant, MailMessage, PlatformAccount,
                           PlatformStatement, ReportFetch, SalesReport, User)
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
            "data": _data_on(db, a.id),
            "fields": [(f, label, acc.UNREADABLE if f in bad else mask_value(creds.get(f, "")))
                       for f, label in acc.CREDENTIAL_FIELDS[a.platform]],
            "sync_choices": [s for s in sync_accounts if s.platform == a.platform],
        })
    return templates.TemplateResponse(request, "accounts.html", {
        "current_user": user, "active_page": "accounts", "flash": pop_flash(request),
        "rows": rows, "platforms": PLATFORMS, "sync_error": sync_error,
        "sync_names": {s.id: s.name for s in sync_accounts},
        "mail": mail.load(db), "mail_password_set": bool(settings.get(db, mail.PASSWORD)),
        "mail_last_check": settings.get(db, mail.LAST_CHECK), "mail_last_result": settings.get(db, mail.LAST_RESULT),
        "mail_messages": db.query(MailMessage).order_by(MailMessage.id.desc()).limit(15).all(),
        "lamoda_accounts": [r["account"] for r in rows if r["account"].platform == "lamoda"],
    })


def _data_on(db: Session, account_id: int) -> tuple[int, int]:
    """(отчётов, сводов) на кабинете — то, что удаление кабинета унесло бы."""
    return (db.query(SalesReport).filter(SalesReport.account_id == account_id).count(),
            db.query(PlatformStatement).filter(PlatformStatement.account_id == account_id).count())


def _twin(db: Session, platform: str, name: str, except_id: int | None = None) -> PlatformAccount | None:
    """Кабинет той же площадки с тем же названием. Два одинаковых — это беда, а не
    вкус: 19.10.2026 второй «Яндекс» у KIT остановил автозагрузку продаж из AWER CRM
    (`fetch.crm_account` не угадывает кабинет из двух), и продаж KIT за октябрь не стало."""
    for other in db.query(PlatformAccount).filter(PlatformAccount.platform == platform):
        if other.id != except_id and other.name.strip().casefold() == name.strip().casefold():
            return other
    return None


@router.post("/api-keys/new")
def create(request: Request, platform: str = Form(""), name: str = Form(""),
           db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    name = name.strip()[:128]
    if platform not in PLATFORMS or not name:
        set_flash(request, "Выберите площадку и введите название кабинета.", "warn")
        return _back()
    if _twin(db, platform, name) is not None:
        set_flash(request, f"Кабинет «{name}» у {PLATFORMS[platform]} уже есть — второй не заведён.", "warn")
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
    if name and _twin(db, account.platform, name, except_id=account.id) is not None:
        set_flash(request, f"Кабинет «{name}» у {PLATFORMS[account.platform]} уже есть — ничего не сохранено, "
                           "выберите другое название.", "warn")
        return _back()
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
    labels = dict(acc.CREDENTIAL_FIELDS[account.platform])
    if autofilled:
        set_flash(request, f"«{account.name}» сохранён, но в поле «{'», «'.join(autofilled)}» браузер "
                           f"подставил ваш пароль входа — поле НЕ изменено. Впишите ключ из кабинета "
                           f"площадки.", "warn")
    elif form.get("then_check") == "1":
        ok, message = fetch.check_access(db, account)
        log_action(db, user.username, "account_checked", f"{account.name}: {'ок' if ok else 'ошибка'} — {message}")
        db.commit()
        set_flash(request, f"«{account.name}» сохранён. Проверка доступа: {message}", "good" if ok else "warn")
    else:
        set_flash(request, f"«{account.name}» сохранён."
                  + (f" Изменены ключи: {', '.join(labels.get(f, f) for f in changed)}." if changed else ""),
                  "good")
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


@router.post("/mail/settings")
def mail_settings(request: Request, host: str = Form(""), port: str = Form("993"), user_name: str = Form(""),
                  password: str = Form(""), folder: str = Form("INBOX"), sender: str = Form(mail.DEFAULT_SENDER),
                  account_id: str = Form(""), db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    if password.strip() and verify_password(password.strip(), user.password_hash):
        set_flash(request, "В поле пароля почты браузер подставил ваш пароль входа — почта НЕ сохранена. "
                           "Впишите пароль приложения от почтового ящика.", "warn")
        return _back()
    mail.save(db, host=host, port=port, user=user_name, password=password, folder=folder, sender=sender,
              account_id=account_id)
    log_action(db, user.username, "mail_settings", f"{user_name}@{host}:{port}, папка {folder}, от {sender}")
    db.commit()
    set_flash(request, "Почта сохранена. Письма проверяются раз в 30 минут, или нажмите «Проверить почту сейчас».",
              "good")
    return _back()


@router.post("/mail/check")
def mail_check(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        stats = mail.fetch_new(db)
    except mail.MailError as e:
        db.rollback()
        set_flash(request, f"Почта: {e}", "warn")
        return _back()
    if not stats["configured"]:
        set_flash(request, "Почта не настроена: впишите сервер, логин и пароль.", "warn")
    else:
        set_flash(request, f"Почта проверена: новых писем {stats['messages']}, загружено {stats['imported']}, "
                           f"с ошибкой {stats['failed']}.", "good" if not stats["failed"] else "warn")
    return _back()


@router.post("/api-keys/{account_id}/delete")
def delete(account_id: int, request: Request, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    """Удалить ПУСТОЙ кабинет — заведённый по ошибке или лишний двойник. Кабинет
    с отчётами или сводами не удаляется: отчёты остались бы без кабинета, а их
    суммы — без хозяина; такой кабинет выключают."""
    account = db.get(PlatformAccount, account_id)
    if account is None:
        set_flash(request, "Кабинет не найден.", "warn")
        return _back()
    reports, statements = _data_on(db, account.id)
    if reports or statements:
        set_flash(request, f"Кабинет «{account.name}» не удалён: на нём отчётов {reports}, сводов {statements}. "
                           "Удаляется только пустой кабинет — этот можно выключить.", "warn")
        return _back()
    name, platform = account.name, account.platform
    for model in (ApiCredential, KitVariant, ReportFetch):
        db.query(model).filter(model.account_id == account.id).delete(synchronize_session=False)
    if settings.get(db, mail.ACCOUNT) == str(account.id):
        settings.put(db, mail.ACCOUNT, "")
    db.delete(account)
    log_action(db, user.username, "account_deleted", f"{name} ({platform}), №{account_id}")
    db.commit()
    set_flash(request, f"Кабинет «{name}» удалён.", "good")
    return _back()

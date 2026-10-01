"""Страница «API-ключи»: кабинеты площадок, их ключи и каталоги.

Ключи хранятся ЗАШИФРОВАННЫМИ своим ключом программы и на страницу не
выводятся — только маска. Пустое поле при сохранении ключ не стирает.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import accounts as acc_mod, audit, platforms
from priceapp.crypto import mask_value
from priceapp.database import get_db
from priceapp.deps import get_current_user
from priceapp.flash import flash
from priceapp.models import Account, PlatformItem, User
from priceapp.pages import render
from priceapp.timeutils import now_utc

router = APIRouter()

# Подменяется в тестах: настоящая сеть там не нужна.
CLIENT_FACTORY = None


def _back():
    return RedirectResponse("/api-keys", status_code=303)


@router.get("/api-keys")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = []
    for a in db.query(Account).order_by(Account.platform, Account.name):
        creds = acc_mod.credentials(db, a)
        rows.append({"account": a, "fields": [(f, label, mask_value(creds.get(f, "")))
                                               for f, label in platforms.CREDENTIAL_FIELDS[a.platform]],
                     "items": db.query(PlatformItem).filter(PlatformItem.account_id == a.id).count()})
    return render(request, "accounts.html", user, "accounts", rows=rows, platforms=platforms.PLATFORMS)


@router.post("/api-keys/new")
async def create(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    form = await request.form()
    platform = str(form.get("platform") or "")
    name = str(form.get("name") or "").strip()[:128]
    if platform not in platforms.PLATFORMS or not name:
        flash(request, "Выберите площадку и введите название кабинета.", "warn")
        return _back()
    a = Account(platform=platform, name=name)
    db.add(a)
    db.flush()
    audit.log(db, user.username, "account_created", f"{name} ({platform})")
    db.commit()
    flash(request, f"Кабинет «{name}» заведён. Впишите ключи и комиссию площадки (страница «Цены» → «Правила»).", "ok")
    return _back()


@router.post("/api-keys/{account_id}")
async def save(account_id: int, request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    if a is None:
        flash(request, "Кабинет не найден.", "warn")
        return _back()
    form = await request.form()
    name = str(form.get("name") or "").strip()[:128]
    if name and name != a.name:
        a.name = name
    a.is_active = form.get("is_active") == "1"
    changed = [f for f, _ in platforms.CREDENTIAL_FIELDS[a.platform]
               if acc_mod.set_credential(db, a, f, str(form.get(f) or ""))]
    audit.log(db, user.username, "account_saved", a.name,
              f"активен: {a.is_active}; изменены ключи: {', '.join(changed) or 'нет'}")
    db.commit()
    flash(request, f"«{a.name}» сохранён." + (f" Изменены ключи: {', '.join(changed)}." if changed else ""), "ok")
    return _back()


@router.post("/api-keys/{account_id}/check")
def check(account_id: int, request: Request, db: Session = Depends(get_db),
          user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    if a is None:
        return _back()
    try:
        ok, msg = acc_mod.client_for(db, a, CLIENT_FACTORY).test_connection()
    except platforms.PlatformError as e:
        ok, msg = False, str(e)
    a.last_check_at, a.last_check_ok, a.last_check_message = now_utc(), ok, msg[:500]
    db.commit()
    flash(request, f"{a.name}: {msg}", "ok" if ok else "error")
    return _back()


@router.post("/api-keys/{account_id}/catalog")
def load_catalog(account_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    if a is None:
        return _back()
    try:
        st = acc_mod.load_catalog(db, a, acc_mod.client_for(db, a, CLIENT_FACTORY))
    except Exception as e:
        db.rollback()
        flash(request, f"{a.name}: каталог не загружен — {e}"[:400], "error")
        return _back()
    audit.log(db, user.username, "catalog_loaded", a.name, str(st))
    db.commit()
    text = f"{a.name}: каталог загружен, баркодов {st['rows']}, удалено пропавших {st['removed']}."
    if st["truncated"]:
        text += " ВНИМАНИЕ: площадка отдала каталог не полностью — пропавшие строки не удалены."
    flash(request, text, "warn" if st["truncated"] else "ok")
    return _back()

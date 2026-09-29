from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from markapp import audit
from markapp.catalog import import_catalog
from markapp.database import get_db
from markapp.deps import get_current_user
from markapp.flash import flash
from markapp.models import CatalogItem, User
from markapp.pages import render

router = APIRouter()
MAX_UPLOAD = 40 * 1024 * 1024
PAGE_LIMIT = 300


@router.get("/catalog")
def catalog_page(request: Request, q: str = "", db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    query = db.query(CatalogItem)
    q = q.strip()
    if q:
        like = f"%{q}%"
        query = query.filter(or_(CatalogItem.supplier_sku.ilike(like), CatalogItem.ean.like(like),
                                 CatalogItem.lamoda_sku.ilike(like)))
    total = query.count()
    items = query.order_by(CatalogItem.supplier_sku).limit(PAGE_LIMIT).all()
    return render(request, "catalog.html", user, "catalog", items=items, total=total, q=q,
                  all_count=db.query(CatalogItem).count(), limit=PAGE_LIMIT)


@router.post("/catalog/import")
async def catalog_import(request: Request, file: UploadFile = File(...),
                         db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        flash(request, "Файл больше 40 МБ.", "error")
        return RedirectResponse("/catalog", status_code=303)
    try:
        res = import_catalog(db, data)
    except Exception as e:
        db.rollback()
        flash(request, f"Файл не разобран: {e}", "error")
        return RedirectResponse("/catalog", status_code=303)
    audit.log(db, user.username, "catalog_import", file.filename or "",
              f"новых {res.added}, изменено {res.changed}; " + "; ".join(res.changes[:20]))
    db.commit()
    text = f"Справочник обновлён: новых {res.added}, изменено {res.changed}, без изменений {res.unchanged}."
    if res.changes:
        text += " Изменения: " + "; ".join(res.changes[:3])
    if res.errors:
        text += " Ошибки: " + "; ".join(res.errors)
    flash(request, text, "warn" if res.errors or res.changes else "ok")
    return RedirectResponse("/catalog", status_code=303)

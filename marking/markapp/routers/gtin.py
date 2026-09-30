import io
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from markapp import audit, chz_auth, gtin as G, nk, settings
from markapp.catalog import norm_sku
from markapp.database import get_db
from markapp.deps import get_current_user
from markapp.flash import flash
from markapp.models import CatalogItem, GtinPair, NkCard, User
from markapp.pages import render
from markapp.timeutils import today_local

router = APIRouter()
PAGE_LIMIT = 300
MAX_UPLOAD = 20 * 1024 * 1024


def _summary(res: G.AddResult) -> str:
    text = f"добавлено {res.added}, уже было {res.same}"
    if res.conflicts:
        text += f"; КОНФЛИКТОВ {len(res.conflicts)} (не записаны): " + "; ".join(res.conflicts[:3])
    if res.invalid:
        text += f"; неверных строк {len(res.invalid)}: " + "; ".join(res.invalid[:3])
    return text


@router.get("/gtin")
def gtin_page(request: Request, q: str = "", only: str = "", db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    query = db.query(GtinPair)
    q = q.strip()
    if q:
        like = f"%{q}%"
        query = query.filter(or_(GtinPair.supplier_sku.ilike(like), GtinPair.gtin.like(like)))
    if only == "new":
        query = query.filter(GtinPair.exported_at.is_(None))
    total = query.count()
    pairs = query.order_by(GtinPair.supplier_sku).limit(PAGE_LIMIT).all()
    cards = {c.gtin: c for c in db.query(NkCard).filter(NkCard.gtin.in_([p.gtin for p in pairs])).all()}
    counts = {s: db.query(NkCard).filter(NkCard.status == s).count()
              for s in ("pending", "ok", "not_found", "error")}
    org = settings.lamoda_org(db)
    return render(request, "gtin.html", user, "gtin", pairs=pairs, cards=cards, total=total, q=q,
                  only=only, limit=PAGE_LIMIT, all_count=db.query(GtinPair).count(),
                  pending_export=len(G.pending_export(db)), counts=counts,
                  has_token=chz_auth.token(org) is not None, org=org,
                  attr_color=settings.get(db, settings.NK_ATTR_COLOR),
                  attr_size=settings.get(db, settings.NK_ATTR_SIZE),
                  attr_tnved=settings.get(db, settings.NK_ATTR_TNVED),
                  nk_limit=settings.get(db, settings.NK_LIMIT))


@router.post("/gtin/import")
async def gtin_import(request: Request, files: list[UploadFile] = File(...),
                      db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    total = G.AddResult()
    errors = []
    for f in files:
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            errors.append(f"{f.filename}: больше 20 МБ")
            continue
        try:
            total.merge(G.import_product_gtin(db, data, f.filename or "", user.username))
        except (G.GtinError, Exception) as e:
            errors.append(f"{f.filename}: {e}")
    nk.queue_missing(db)
    audit.log(db, user.username, "gtin_import", f"файлов {len(files)}", _summary(total))
    db.commit()
    text = f"product_gtin: {_summary(total)}."
    if errors:
        text += " Не прочитаны: " + "; ".join(errors)
    flash(request, text, "warn" if (total.conflicts or total.invalid or errors) else "ok")
    return RedirectResponse("/gtin", status_code=303)


@router.post("/gtin/add")
def gtin_add(request: Request, supplier_sku: str = Form(...), gtin: str = Form(...),
             db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    sku = norm_sku(supplier_sku)
    if not db.query(CatalogItem).filter(CatalogItem.supplier_sku == sku).first():
        flash(request, f"Артикула «{sku}» нет в справочнике «Одежда полный» — проверьте написание.", "error")
        return RedirectResponse("/gtin", status_code=303)
    res = G.add_pairs(db, [(sku, gtin.strip())], source="manual", source_name="ручной ввод",
                      exported=False, username=user.username)
    nk.queue_missing(db)
    audit.log(db, user.username, "gtin_manual", sku, f"{gtin.strip()}: {_summary(res)}")
    db.commit()
    flash(request, f"Пара {sku} ↔ {gtin.strip()}: {_summary(res)}.",
          "ok" if res.added else "error")
    return RedirectResponse("/gtin", status_code=303)


@router.get("/gtin/export")
def gtin_export(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """product_gtin для Lamoda — только новые пары, ≤1000 строк в файле.
    Отметка «выгружено» ставится в момент скачивания: второй раз эти пары не
    поедут. Если файл так и не загрузили в Lamoda — «Вернуть в очередь»."""
    pairs = G.pending_export(db)
    if not pairs:
        return RedirectResponse("/gtin", status_code=303)
    files = G.build_product_gtin_files(pairs)
    G.mark_exported(pairs)
    stamp = today_local().strftime("%Y-%m-%d")
    audit.log(db, user.username, "gtin_export", f"пар {len(pairs)}", f"файлов {len(files)}")
    db.commit()
    if len(files) == 1:
        return Response(files[0], media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f'product_gtin_{stamp}.xlsx')}"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, data in enumerate(files, 1):
            z.writestr(f"product_gtin_{stamp}_{i}.xlsx", data)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f'product_gtin_{stamp}.zip')}"})


@router.post("/gtin/unexport")
def gtin_unexport(request: Request, day: str = Form(...), db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Файл скачали, но в Lamoda не загрузили — вернуть пары этого дня в очередь."""
    from datetime import date, timedelta
    from markapp.timeutils import local_day_start_utc
    try:
        d = date.fromisoformat(day)
    except ValueError:
        flash(request, "Дата — ГГГГ-ММ-ДД.", "error")
        return RedirectResponse("/gtin", status_code=303)
    start = local_day_start_utc(d)
    pairs = (db.query(GtinPair).filter(GtinPair.exported_at >= start,
                                        GtinPair.exported_at < start + timedelta(days=1),
                                        GtinPair.source != "product_gtin").all())
    for p in pairs:
        p.exported_at = None
    audit.log(db, user.username, "gtin_unexport", day, f"пар {len(pairs)}")
    db.commit()
    flash(request, f"Вернули в очередь на выгрузку пар: {len(pairs)}.", "ok")
    return RedirectResponse("/gtin", status_code=303)


@router.get("/gtin/card/{gtin}")
def gtin_card(gtin: str, request: Request, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    return render(request, "nk_card.html", user, "gtin", gtin=gtin, card=db.get(NkCard, gtin),
                  pair=db.query(GtinPair).filter(GtinPair.gtin == gtin).first())


@router.post("/gtin/card/{gtin}/refresh")
def gtin_card_refresh(gtin: str, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    problem = G.validate_gtin(gtin)
    if problem:
        flash(request, problem, "error")
    else:
        nk.request_refresh(db, gtin)
        db.commit()
        flash(request, "Карточка поставлена в очередь: воркер запросит её в пределах лимита НК "
                       "(10 запросов за 5 минут на весь кабинет).", "ok")
    return RedirectResponse(f"/gtin/card/{gtin}", status_code=303)


@router.post("/gtin/nk-settings")
def gtin_nk_settings(request: Request, attr_color: str = Form(""), attr_size: str = Form(""),
                     attr_tnved: str = Form(""), nk_limit: str = Form("7"),
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not nk_limit.strip().isdigit() or not 1 <= int(nk_limit) <= 10:
        flash(request, "Лимит — от 1 до 10 запросов за 5 минут (документный предел — 10).", "error")
        return RedirectResponse("/gtin", status_code=303)
    settings.put(db, settings.NK_ATTR_COLOR, attr_color.strip())
    settings.put(db, settings.NK_ATTR_SIZE, attr_size.strip())
    settings.put(db, settings.NK_ATTR_TNVED, attr_tnved.strip())
    settings.put(db, settings.NK_LIMIT, nk_limit.strip())
    n = nk.reparse_all(db)
    audit.log(db, user.username, "nk_settings",
              details=f"цвет: {attr_color}; размер: {attr_size}; ТН ВЭД: {attr_tnved}; лимит {nk_limit}")
    db.commit()
    flash(request, f"Сохранено. Сохранённые карточки разобраны заново: {n}.", "ok")
    return RedirectResponse("/gtin", status_code=303)

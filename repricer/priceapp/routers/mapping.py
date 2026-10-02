"""Страница «Сопоставление»: статус каталога кабинета против справочника 1С,
разбор правил артикулов и кандидаты (по правилам sync_admin)."""
from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import article_matching as am, audit, mapping, settings
from priceapp.database import get_db
from priceapp.deps import get_current_user
from priceapp.excel import xlsx_response
from priceapp.flash import flash
from priceapp.models import Account, User
from priceapp.pages import render
from priceapp.routers.prices import _accounts, _cell, _import_flash, _pick, _read_upload, label

router = APIRouter()
LIMIT = 500
CAND_LABELS = {"unique": "однозначно", "ambiguous": "несколько SKU 1С",
               "size_mismatch": "размер не совпал", "none": "не найдено"}


@router.get("/mapping")
def page(request: Request, view: str = Query("status"), account_id: str = Query(""),
         status: str = Query(""), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    view = view if view in ("status", "analysis", "candidates") else "status"
    accs = _accounts(db)
    account = _pick(accs, account_id)
    ctx = dict(view=view, accounts=accs, account=account, label=label, status=status,
               status_labels=mapping.STATUS_LABELS, kinds=am.KINDS, kind_labels=am.KIND_LABELS,
               cand_labels=CAND_LABELS, dict_at=settings.get(db, settings.DICT_LOADED_AT),
               dict_rows=settings.get(db, settings.DICT_ROWS))
    if account is not None:
        if view == "status":
            rows = mapping.build(db, account.id)
            ctx["counts"] = mapping.counts(rows)
            if status in mapping.STATUS_LABELS:
                rows = [r for r in rows if r.status == status]
            ctx.update(rows=rows[:LIMIT], total=len(rows))
        elif view == "analysis":
            rule = am.get_rule(db, account.id)
            ctx.update(rule=rule, enabled=am.parse_kinds(rule.kinds), an=am.analyze(db, account.id))
        else:
            cands = am.candidates(db, account.id)
            ctx["counts"] = {k: sum(1 for c in cands if c.status == k) for k in CAND_LABELS}
            if status in CAND_LABELS:
                cands = [c for c in cands if c.status == status]
            ctx.update(cands=cands[:LIMIT], total=len(cands))
        db.commit()
    return render(request, "mapping.html", user, "mapping", **ctx)


@router.post("/mapping/rules/{account_id}")
async def save_rule(account_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    if a is None:
        return RedirectResponse("/mapping", status_code=303)
    form = await request.form()
    kinds = [k for k in am.KINDS if k in form.getlist("kinds")]
    rule = am.get_rule(db, a.id)
    before = (rule.kinds, rule.strip_prefix, rule.strip_suffix)
    rule.kinds = ",".join(kinds)
    rule.strip_prefix = str(form.get("strip_prefix") or "").strip()[:64] or None
    rule.strip_suffix = str(form.get("strip_suffix") or "").strip()[:64] or None
    audit.log(db, user.username, "article_rule_saved", label(a),
              f"было {before}, стало {(rule.kinds, rule.strip_prefix, rule.strip_suffix)}")
    db.commit()
    flash(request, f"Правило {label(a)} сохранено." + ("" if kinds else " Ни один вид не включён — кандидатов не будет."),
          "ok" if kinds else "warn")
    return RedirectResponse(f"/mapping?view=analysis&account_id={a.id}", status_code=303)


@router.post("/mapping/confirm/{account_id}")
async def confirm(account_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    back = RedirectResponse(f"/mapping?view=candidates&account_id={account_id}", status_code=303)
    if a is None:
        return back
    form = await request.form()
    pairs = [tuple(str(v).split("|", 1)) for v in form.getlist("pick") if "|" in str(v)]
    if not pairs:
        flash(request, "Ничего не выбрано.", "warn")
        return back
    created, refused = am.confirm(db, a.id, pairs, user.username)
    audit.log(db, user.username, "article_match_confirmed", label(a),
              f"создано {created}, отказано {sum(refused.values())}; {pairs[:50]}")
    db.commit()
    msg = f"Сопоставлено баркодов: {created}."
    if refused:
        msg += " Не сопоставлено: " + "; ".join(f"{n} — {r}" for r, n in refused.items()) + "."
    flash(request, msg, "warn" if refused else "ok")
    return back


@router.get("/mapping/export/{account_id}")
def export(account_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    if a is None:
        return RedirectResponse("/mapping", status_code=303)
    rows = mapping.build(db, a.id)
    data = [[r.item.barcode, r.item.article, r.item.size, r.item.name, r.label, r.item_id,
             ", ".join(r.others)] for r in rows]
    return xlsx_response(["Баркод", "Артикул площадки", "Размер", "Название", "Статус", "ID_1С",
                          "SKU 1С (если неоднозначно)"], data, f"сопоставление_{a.name}.xlsx")


def _num_text(v) -> str:
    """Баркод из Excel приходит числом (2000932200000.0) — возвращаем строкой."""
    t = _cell(v)
    return t[:-2] if t.endswith(".0") and t[:-2].isdigit() else t


@router.post("/mapping/import/{account_id}")
def import_links(account_id: int, request: Request, file: UploadFile = File(...),
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Ручные связи файлом вкладки «Статус»: у строк «нет в 1С» вписать ID_1С.
    Строки с другим статусом и пустые ячейки ничего не меняют."""
    back = RedirectResponse(f"/mapping?view=status&account_id={account_id}", status_code=303)
    a = db.get(Account, account_id)
    if a is None:
        return back
    rows = _read_upload(request, file, "ID_1С")
    if rows is None:
        return back
    current = {r.item.barcode: r for r in mapping.build(db, a.id)}
    pairs = []
    for row in rows:
        barcode, item_id = _num_text(row.get("Баркод")), _cell(row.get("ID_1С"))
        r = current.get(barcode)
        if not barcode or not item_id or (r is not None and r.item_id == item_id):
            continue          # пусто или уже так — без изменений
        pairs.append((barcode, item_id))
    created, refused = mapping.link_manually(db, a.id, pairs, user.username)
    audit.log(db, user.username, "mapping_import", label(a), f"создано {created}, отказано {refused}")
    db.commit()
    _import_flash(request, f"Ручных связей создано: {created}.",
                  [f"{n} — {r}" for r, n in refused.items()])
    return back


CAND_HEADERS = ["Баркод", "Артикул площадки", "Размер", "Название", "Результат", "ID_1С",
                "Артикул 1С", "Размер 1С", "Цвет 1С", "Правило", "Подтвердить (Да)"]


@router.get("/mapping/candidates-export/{account_id}")
def export_candidates(account_id: int, status: str = Query(""), db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    a = db.get(Account, account_id)
    if a is None:
        return RedirectResponse("/mapping", status_code=303)
    cands = am.candidates(db, a.id)
    db.commit()
    if status in CAND_LABELS:
        cands = [c for c in cands if c.status == status]
    data = []
    for c in cands:
        p = c.products[0] if len(c.products) == 1 else None
        data.append([c.item.barcode, c.item.article, c.item.size, c.item.name, CAND_LABELS[c.status],
                     p.uid_1c if p else "", p.article if p else "", p.size if p else "", p.color if p else "",
                     ", ".join(am.KIND_LABELS[k] for k in c.kinds), ""])
    return xlsx_response(CAND_HEADERS, data, f"кандидаты_{a.name}.xlsx")


@router.post("/mapping/candidates-import/{account_id}")
def import_candidates(account_id: int, request: Request, file: UploadFile = File(...),
                      db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """«Да» в «Подтвердить» — то же, что галочка и «Сопоставить выбранные»:
    `am.confirm` пересчитывает предложение и связывает только однозначное."""
    back = RedirectResponse(f"/mapping?view=candidates&account_id={account_id}", status_code=303)
    a = db.get(Account, account_id)
    if a is None:
        return back
    rows = _read_upload(request, file, "Подтвердить (Да)")
    if rows is None:
        return back
    pairs, errors = [], []
    for i, row in enumerate(rows, start=2):
        decision = _cell(row.get("Подтвердить (Да)")).lower()
        if decision == "":
            continue
        if decision != "да":
            errors.append(f"строка {i}: «{row.get('Подтвердить (Да)')}» — нужно «Да» или пусто")
            continue
        pairs.append((_num_text(row.get("Баркод")), _cell(row.get("ID_1С"))))
    created, refused = am.confirm(db, a.id, pairs, user.username) if pairs else (0, {})
    audit.log(db, user.username, "article_match_import", label(a), f"создано {created}, отказано {refused}")
    db.commit()
    _import_flash(request, f"Сопоставлено баркодов: {created}.",
                  errors + [f"{n} — {r}" for r, n in refused.items()])
    return back

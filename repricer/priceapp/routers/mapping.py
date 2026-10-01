"""Страница «Сопоставление»: статус каталога кабинета против справочника 1С,
разбор правил артикулов и кандидаты (по правилам sync_admin)."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import article_matching as am, audit, mapping, settings
from priceapp.database import get_db
from priceapp.deps import get_current_user
from priceapp.excel import xlsx_response
from priceapp.flash import flash
from priceapp.models import Account, User
from priceapp.pages import render
from priceapp.routers.prices import _accounts, _pick, label

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

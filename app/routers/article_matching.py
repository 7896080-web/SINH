"""Страница «Сопоставление артикулов» (/article-matching).

Две вкладки:
  * «Разбор» — по каждому кабинету: как артикул площадки соотносится с
    артикулом 1С на парах, УЖЕ сопоставленных по баркоду (статистика с
    примерами), предложенное правило и настройка правила кабинета;
  * «Кандидаты» — товары площадки без связи по баркоду, для которых правила
    кабинета нашли товар 1С. Однозначные подтверждаются галочками, остальное
    выгружается в Excel в формате «Мэппинга» (колонки ID_1С + Баркод) и
    загружается обратно там же.

Связь баркод → товар создаётся только подтверждением (логика — app/article_matching.py).
"""

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.article_matching import (KINDS, KIND_LABELS, analyze, candidates, confirm, get_rule,
                                  parse_kinds)
from app.audit import log_action
from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.excel_utils import build_xlsx_response
from app.flash import set_flash, pop_flash
from app.models import PlatformAccount, User

router = APIRouter()
templates = shared_templates

ROWS_LIMIT = 300
STATUS_LABELS = {
    "unique": "однозначно",
    "ambiguous": "несколько товаров 1С",
    "size_mismatch": "размер не совпал",
    "none": "не найдено",
}


def _accounts(db: Session) -> list[PlatformAccount]:
    return list(db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True))
                .order_by(PlatformAccount.platform, PlatformAccount.name).all())


def _label(a: PlatformAccount) -> str:
    return f"{a.name} ({a.platform.value.upper()})"


def _pick_account(accounts, account_id: str):
    if (account_id or "").isdigit():
        for a in accounts:
            if a.id == int(account_id):
                return a
    return accounts[0] if accounts else None


def _filtered_candidates(db: Session, account, status: str):
    rows = candidates(db, account.id) if account else []
    counts = {k: 0 for k in STATUS_LABELS}
    for c in rows:
        counts[c.status] += 1
    if status in STATUS_LABELS:
        rows = [c for c in rows if c.status == status]
    return rows, counts


def _render(request: Request, db: Session, user: User, template: str, view: str,
            account_id: str, status: str):
    view = view if view in ("analysis", "candidates") else "analysis"
    accounts = _accounts(db)
    ctx = {
        "request": request, "current_user": user, "active_page": "article-matching",
        "view": view, "accounts": accounts, "label": _label, "kinds": KINDS,
        "kind_labels": KIND_LABELS, "status_labels": STATUS_LABELS, "status": status,
        "flash": pop_flash(request) if template == "article_matching.html" else None,
    }
    if view == "analysis":
        ctx["blocks"] = []
        for a in accounts:
            rule = get_rule(db, a.id)
            ctx["blocks"].append({"account": a, "rule": rule, "enabled": parse_kinds(rule.kinds),
                                  "analysis": analyze(db, a.id)})
        db.commit()
    else:
        account = _pick_account(accounts, account_id)
        rows, counts = _filtered_candidates(db, account, status)
        db.commit()
        ctx.update({"account": account, "account_id": str(account.id) if account else "",
                    "rows": rows[:ROWS_LIMIT], "total": len(rows), "counts": counts,
                    "rows_limit": ROWS_LIMIT})
    return templates.TemplateResponse(request, template, ctx)


@router.get("/article-matching", response_class=HTMLResponse)
def article_matching_page(request: Request, view: str = Query("analysis"), account_id: str = Query(""),
                          status: str = Query(""),
                          db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _render(request, db, user, "article_matching.html", view, account_id, status)


@router.get("/article-matching/rows", response_class=HTMLResponse)
def article_matching_rows(request: Request, account_id: str = Query(""), status: str = Query(""),
                          db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _render(request, db, user, "article_matching_rows.html", "candidates", account_id, status)


@router.post("/article-matching/rules/{account_id}")
async def save_rule(account_id: int, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        set_flash(request, "Кабинет не найден.", "warn")
        return RedirectResponse("/article-matching", status_code=303)
    form = await request.form()
    kinds = [k for k in KINDS if k in form.getlist("kinds")]
    prefix = str(form.get("strip_prefix") or "").strip()[:64]
    suffix = str(form.get("strip_suffix") or "").strip()[:64]
    rule = get_rule(db, account_id)
    before = (rule.kinds, rule.strip_prefix, rule.strip_suffix)
    rule.kinds = ",".join(kinds)
    rule.strip_prefix = prefix or None
    rule.strip_suffix = suffix or None
    log_action(db, user.username, "article_rule_saved",
               f"{_label(account)}: было {before}, стало {(rule.kinds, rule.strip_prefix, rule.strip_suffix)}")
    db.commit()
    message = f"Правило для {_label(account)} сохранено."
    if not kinds:
        message += " Ни один вид не включён — кандидатов не будет."
    set_flash(request, message, "warn" if not kinds else "good")
    return RedirectResponse("/article-matching", status_code=303)


@router.post("/article-matching/confirm")
async def confirm_candidates(request: Request,
                             db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    form = await request.form()
    account_id = str(form.get("account_id") or "")
    back = f"/article-matching?view=candidates&account_id={account_id}"
    pairs = []
    for value in form.getlist("pick"):
        barcode, _, uid = str(value).partition("|")
        if barcode and uid:
            pairs.append((barcode, uid))
    account = db.query(PlatformAccount).filter(
        PlatformAccount.id == int(account_id)).first() if account_id.isdigit() else None
    if account is None or not pairs:
        set_flash(request, "Ничего не выбрано.", "warn")
        return RedirectResponse(back, status_code=303)

    created, refused = confirm(db, account.id, pairs)
    log_action(db, user.username, "article_match_confirmed",
               f"{_label(account)}: создано связей {created}, отказано {sum(refused.values())}; "
               f"{pairs[:50]}")
    db.commit()
    message = f"Сопоставлено баркодов: {created}. Заказы по ним теперь распознаются."
    if refused:
        message += " Не сопоставлено: " + "; ".join(f"{n} — {r}" for r, n in refused.items()) + "."
    set_flash(request, message, "warn" if refused else "good")
    return RedirectResponse(back, status_code=303)


@router.get("/article-matching/export")
def article_matching_export(account_id: str = Query(""), status: str = Query(""),
                            db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Файл в формате «Мэппинга»: заполните ID_1С и загрузите на странице
    «Мэппинг» → «Импорт из Excel». У однозначных ID_1С уже проставлен — их
    импорт равносилен подтверждению здесь."""
    accounts = _accounts(db)
    account = _pick_account(accounts, account_id)
    rows, _ = _filtered_candidates(db, account, status)
    db.commit()
    headers = ["ID_1С", "Баркод", "Артикул на площадке", "Размер на площадке", "Название на площадке",
               "Кабинет", "Результат", "Варианты 1С (ID — артикул — размер — цвет)"]
    data = []
    for c in rows:
        options = "; ".join(f"{p.uid_1c} — {p.article or ''} — {p.size or ''} — {p.color or ''}"
                            for p in c.products)
        data.append([c.products[0].uid_1c if c.status == "unique" else None, c.item.barcode,
                     c.item.article or "", c.item.size or "", c.item.name or "",
                     _label(account), STATUS_LABELS[c.status], options])
    return build_xlsx_response(headers, data, "сопоставление_артикулов.xlsx")

from datetime import datetime
from app.timeutils import now_utc

from fastapi import APIRouter, Request, Depends, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.database import get_db
from app.dependencies import get_current_user
from app.models import SyncAnomaly, SyncSetting, Product, PlatformAccount, AnomalyReason, AnomalyStatus, User
from app.routers.sync_products import enqueue_full_resend
from app.excel_utils import build_xlsx_response, read_xlsx_rows, parse_bool_ru, format_dt
from app.flash import set_flash, pop_flash
from app.audit import log_action

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

REASON_LABELS = {
    AnomalyReason.order_on_disabled: "Заказ на невключённый товар",
    AnomalyReason.missing_barcode: "Нет баркода при включённой галочке",
}

REASON_PRIORITY = {
    AnomalyReason.order_on_disabled: 0,
    AnomalyReason.missing_barcode: 1,
}


def _load_grouped_anomalies(db: Session, status: str, q: str, account_id: str, reason: str):
    query = (
        db.query(
            SyncAnomaly.uid_1c, SyncAnomaly.account_id, SyncAnomaly.reason,
            func.count(SyncAnomaly.id).label("occurrences"),
            func.max(SyncAnomaly.detected_at).label("last_detected"),
            Product.article, Product.name, Product.size, Product.color,
            PlatformAccount.name.label("account_name"), PlatformAccount.platform.label("account_platform"),
        )
        .join(Product, Product.uid_1c == SyncAnomaly.uid_1c)
        .join(PlatformAccount, PlatformAccount.id == SyncAnomaly.account_id)
        .filter(SyncAnomaly.status == status)
        .filter(SyncAnomaly.is_test.is_(False))  # тестовые аномалии со страницы тестирования сюда не попадают
    )

    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.article.ilike(like), Product.name.ilike(like)))

    if account_id:
        query = query.filter(SyncAnomaly.account_id == int(account_id))

    if reason:
        query = query.filter(SyncAnomaly.reason == reason)

    query = query.group_by(
        SyncAnomaly.uid_1c, SyncAnomaly.account_id, SyncAnomaly.reason,
        Product.article, Product.name, Product.size, Product.color,
        PlatformAccount.name, PlatformAccount.platform,
    )

    rows = query.all()
    rows = sorted(rows, key=lambda r: (REASON_PRIORITY.get(r.reason, 99), -r.last_detected.timestamp()))
    return rows


def _resolve_sync_and_anomalies(db: Session, uid_1c: str, account_id: int) -> bool:
    """Включает синхронизацию (если ещё не включена) и закрывает связанные
    аномалии по паре товар+кабинет. Используется и кнопкой в интерфейсе,
    и массовым импортом из Excel — логика ровно одна, не дублируется."""

    setting = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account_id,
    ).first()

    if setting is None:
        setting = SyncSetting(uid_1c=uid_1c, account_id=account_id)
        db.add(setting)

    changed = not setting.enabled
    if changed:
        setting.enabled = True
        setting.enabled_at = now_utc()
        setting.has_proposal = False
        enqueue_full_resend(db, uid_1c, account_id)

    db.query(SyncAnomaly).filter(
        SyncAnomaly.uid_1c == uid_1c,
        SyncAnomaly.account_id == account_id,
        SyncAnomaly.status == AnomalyStatus.new,
    ).update({"status": AnomalyStatus.resolved})

    return changed


def _render(request: Request, db: Session, user: User, status: str, q: str, account_id: str, reason: str, template: str):
    rows = _load_grouped_anomalies(db, status, q, account_id, reason)
    new_count = db.query(SyncAnomaly).filter(
        SyncAnomaly.status == AnomalyStatus.new.value, SyncAnomaly.is_test.is_(False),
    ).count()
    accounts = db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name).all()

    return templates.TemplateResponse(request, template, {
        "request": request, "current_user": user, "active_page": "anomalies",
        "rows": rows, "status": status, "q": q, "account_id": account_id, "reason": reason,
        "accounts": accounts, "reasons": list(AnomalyReason), "reason_labels": REASON_LABELS,
        "new_count": new_count,
        "flash": pop_flash(request) if template == "anomalies.html" else None,
    })


@router.get("/anomalies", response_class=HTMLResponse)
def anomalies_page(
    request: Request, status: str = Query("new"), q: str = Query(""), account_id: str = Query(""),
    reason: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, status, q, account_id, reason, "anomalies.html")


@router.get("/anomalies/rows", response_class=HTMLResponse)
def anomalies_rows(
    request: Request, status: str = Query("new"), q: str = Query(""), account_id: str = Query(""),
    reason: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, status, q, account_id, reason, "anomalies_rows.html")


@router.post("/anomalies/{uid_1c}/{account_id}/resolve", response_class=HTMLResponse)
def resolve_anomaly(
    uid_1c: str, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    _resolve_sync_and_anomalies(db, uid_1c, account_id)
    log_action(db, user.username, "anomaly_resolved", f"{uid_1c} / кабинет #{account_id}")
    db.commit()

    return HTMLResponse("")


@router.get("/anomalies/export")
def anomalies_export(
    status: str = Query("new"), q: str = Query(""), account_id: str = Query(""), reason: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    rows = _load_grouped_anomalies(db, status, q, account_id, reason)

    headers = ["ID_1С", "Артикул", "Наименование", "Кабинет", "Площадка", "Причина",
               "Заказов поймано", "Последний раз", "Синхронизировать"]
    data = [
        [r.uid_1c, r.article, r.name, r.account_name, r.account_platform.value, REASON_LABELS[r.reason],
         r.occurrences, format_dt(r.last_detected), "Нет"]
        for r in rows
    ]

    return build_xlsx_response(headers, data, "аномалии_синхронизации.xlsx")


@router.post("/anomalies/import")
def anomalies_import(
    request: Request, file: UploadFile = File(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Массовая разборка аномалий из отредактированного файла: строки, где в
    колонке «Синхронизировать» проставлено 'Да', обрабатываются. Колонки
    ID_1С и Кабинет — ключ сопоставления (по имени кабинета, регистр важен)."""

    accounts_by_name = {a.name: a for a in db.query(PlatformAccount).all()}
    rows = read_xlsx_rows(file.file.read())

    resolved, skipped, errors = 0, 0, []

    for i, row in enumerate(rows, start=2):
        uid_1c = str(row.get("ID_1С") or "").strip()
        account_name = str(row.get("Кабинет") or "").strip()

        if not uid_1c or not account_name:
            continue

        if not parse_bool_ru(row.get("Синхронизировать")):
            skipped += 1
            continue

        account = accounts_by_name.get(account_name)
        if account is None:
            errors.append(f"строка {i}: кабинет «{account_name}» не найден")
            continue

        product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
        if product is None:
            errors.append(f"строка {i}: товар с ID {uid_1c} не найден")
            continue

        _resolve_sync_and_anomalies(db, uid_1c, account.id)
        resolved += 1

    log_action(db, user.username, "anomalies_bulk_resolved_excel", f"resolved={resolved}")
    db.commit()

    message = f"Обработано (включено и закрыто): {resolved}. Пропущено (без отметки): {skipped}."
    if errors:
        shown = "; ".join(errors[:5])
        more = f" и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        message += f" Ошибок: {len(errors)} ({shown}{more})."
        set_flash(request, message, "warn")
    else:
        set_flash(request, message, "good")

    return RedirectResponse("/anomalies", status_code=303)

from datetime import datetime
from app.timeutils import now_utc

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.dependencies import get_current_user
from app.models import (
    PlatformAccount, WorkerHeartbeat, DispatchQueueItem, DispatchStatus,
    FtpTask, FtpTaskStatus, MappingConflict, SyncAnomaly, AnomalyStatus, User,
)
from app.workers.client_factory import build_client
from app.workers.credentials import CredentialsMissing
from app.workers.order_poller import poll_new_orders, poll_cancellations
from app.workers.catalog_sync import load_platform_catalog
from app.workers.scheduler import PENDING_WAREHOUSE_NAME
from app.audit import log_action
from app.flash import set_flash, pop_flash

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _queue_counts(db: Session, account_id: int) -> dict:
    dispatch_pending = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account_id, DispatchQueueItem.status == DispatchStatus.pending,
        DispatchQueueItem.is_test.is_(False),
    ).count()
    dispatch_errors = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account_id, DispatchQueueItem.status == DispatchStatus.error,
        DispatchQueueItem.is_test.is_(False),
    ).count()
    # Записи, которые уже сорвались и ждут следующей попытки: сама по себе это не
    # ошибка (площадка отвечает не всегда), но растущее число — повод посмотреть
    # в last_error, пока попытки не исчерпались и запись не стала ошибкой.
    dispatch_retry = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account_id, DispatchQueueItem.status == DispatchStatus.pending,
        DispatchQueueItem.attempts > 0, DispatchQueueItem.is_test.is_(False),
    ).count()
    ftp_pending = db.query(FtpTask).filter(
        FtpTask.account_id == account_id, FtpTask.status.in_([FtpTaskStatus.pending, FtpTaskStatus.sent]),
        FtpTask.is_test.is_(False),
    ).count()
    ftp_timeout = db.query(FtpTask).filter(
        FtpTask.account_id == account_id, FtpTask.status == FtpTaskStatus.timeout,
        FtpTask.is_test.is_(False),
    ).count()
    return {
        "dispatch_pending": dispatch_pending, "dispatch_errors": dispatch_errors,
        "dispatch_retry": dispatch_retry,
        "ftp_pending": ftp_pending, "ftp_timeout": ftp_timeout,
    }


def _heartbeat_for(db: Session, worker_name: str):
    return db.query(WorkerHeartbeat).filter(WorkerHeartbeat.worker_name == worker_name).first()


@router.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    accounts = db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name).all()

    account_rows = []
    for account in accounts:
        hb = _heartbeat_for(db, f"poll_orders_account_{account.id}")
        account_rows.append({
            "account": account,
            "queue": _queue_counts(db, account.id),
            "heartbeat": hb,
        })

    shared_workers = ["dispatch", "ftp_send", "ftp_receive", "reconciliation"]
    shared_heartbeats = [{"name": w, "hb": _heartbeat_for(db, w)} for w in shared_workers]

    global_stats = {
        "conflicts": db.query(MappingConflict).count(),
        "anomalies_new": db.query(SyncAnomaly).filter(
            SyncAnomaly.status == AnomalyStatus.new, SyncAnomaly.is_test.is_(False),
        ).count(),
        "dispatch_errors_total": db.query(DispatchQueueItem).filter(DispatchQueueItem.status == DispatchStatus.error).count(),
    }

    return templates.TemplateResponse(request, "diagnostics.html", {
        "request": request, "current_user": user, "active_page": "diagnostics",
        "account_rows": account_rows, "shared_heartbeats": shared_heartbeats,
        "global_stats": global_stats, "now": now_utc(),
        "flash": pop_flash(request),
    })


@router.post("/diagnostics/accounts/{account_id}/test-connection")
def test_connection(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/diagnostics", status_code=303)

    try:
        client = build_client(db, account_id)
        ok, message = client.test_connection()
    except CredentialsMissing as e:
        ok, message = False, str(e)
    except Exception as e:
        ok, message = False, f"Неожиданная ошибка: {e}"

    account.last_connection_check_at = now_utc()
    account.last_connection_ok = ok
    account.last_connection_message = message
    log_action(db, user.username, "connection_test", f"{account.name}: {'OK' if ok else 'FAIL'} — {message}")
    db.commit()

    set_flash(request, f"«{account.name}»: {message}", "good" if ok else "warn")
    return RedirectResponse("/diagnostics", status_code=303)


@router.post("/diagnostics/accounts/{account_id}/poll-now")
def poll_now(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Ручной запуск опроса заказов вне расписания — чтобы проверить
    поведение сразу после ввода ключей, не дожидаясь цикла планировщика."""
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/diagnostics", status_code=303)

    try:
        client = build_client(db, account_id)
        new_stats = poll_new_orders(db, client, account, PENDING_WAREHOUSE_NAME[account.platform])
        cancel_stats = poll_cancellations(db, client, account)
        message = f"Новые: {new_stats}. Отмены: {cancel_stats}."
        log_action(db, user.username, "manual_poll_orders", f"{account.name}: {message}")
        db.commit()
        set_flash(request, f"«{account.name}»: {message}", "good")
    except CredentialsMissing as e:
        set_flash(request, f"«{account.name}»: {e}", "warn")
    except Exception as e:
        set_flash(request, f"«{account.name}»: ошибка при опросе — {e}", "warn")

    return RedirectResponse("/diagnostics", status_code=303)


@router.post("/diagnostics/accounts/{account_id}/catalog-now")
def catalog_now(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/diagnostics", status_code=303)

    try:
        client = build_client(db, account_id)
        stats = load_platform_catalog(db, client, account)
        log_action(db, user.username, "manual_catalog_sync", f"{account.name}: {stats}")
        db.commit()
        set_flash(request, f"«{account.name}»: загружено {stats['fetched']} карточек, новых конфликтов {stats['new_conflicts']}.", "good")
    except CredentialsMissing as e:
        set_flash(request, f"«{account.name}»: {e}", "warn")
    except Exception as e:
        set_flash(request, f"«{account.name}»: ошибка при загрузке каталога — {e}", "warn")

    return RedirectResponse("/diagnostics", status_code=303)


@router.post("/diagnostics/accounts/{account_id}/reset-failures")
def reset_failures(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Сбрасывает счётчик автоотключения вручную — например, после того как
    оператор поправил ключи и хочет дать кабинету новый шанс немедленно,
    не дожидаясь первого успешного цикла."""
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        return RedirectResponse("/diagnostics", status_code=303)

    account.consecutive_failures = 0
    account.last_error = None
    log_action(db, user.username, "failures_reset_manually", account.name)
    db.commit()

    set_flash(request, f"Счётчик сбоев для «{account.name}» сброшен.", "good")
    return RedirectResponse("/diagnostics", status_code=303)

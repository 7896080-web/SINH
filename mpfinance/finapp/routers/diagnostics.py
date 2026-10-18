"""«Диагностика»: обмен с 1С (версия обработки, справочник, себестоимость),
база Sync Admin, фоновые задания, журнал действий."""
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from finapp import backup, catalog, config, onec, settings, syncdb
from finapp.audit import log_action
from finapp.database import get_db
from finapp.deps import get_current_user
from finapp.finance import costs
from finapp.flash import pop_flash, set_flash
from finapp.models import AuditLog, Barcode, OnecTask, Product, User, WorkerHeartbeat
from finapp.templating import ONEC_TASK_LABELS, templates
from finapp.workers.heartbeat import stale_workers

router = APIRouter()


def _back():
    return RedirectResponse("/diagnostics", status_code=303)


@router.get("/diagnostics", response_class=HTMLResponse)
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "diagnostics.html", {
        "current_user": user, "active_page": "diagnostics", "flash": pop_flash(request),
        "epf_version": settings.get(db, settings.ONEC_EPF_VERSION),
        "epf_checked_at": settings.get(db, settings.ONEC_EPF_CHECKED_AT),
        "epf_ready": onec.epf_ready(db),
        "catalog_at": settings.get(db, settings.CATALOG_LOADED_AT),
        "barcodes": db.query(Barcode).count(), "products": db.query(Product).count(),
        "costs_at": settings.get(db, settings.COSTS_LOADED_AT),
        "tasks": db.query(OnecTask).order_by(OnecTask.id.desc()).limit(30).all(),
        "task_labels": ONEC_TASK_LABELS, "commands": onec.COMMANDS,
        "beats": db.query(WorkerHeartbeat).order_by(WorkerHeartbeat.name).all(),
        "stale": stale_workers(db), "last_backup": backup.last_backup(),
        "log": db.query(AuditLog).order_by(AuditLog.id.desc()).limit(50).all(),
        "dirs": {"задания 1С": config.ONEC_TASKS_DIR, "ответы 1С": config.ONEC_RESULTS_DIR,
                 "архив обмена": config.ONEC_ARCHIVE_DIR, "копии базы": config.BACKUP_DIR},
        "sync_status": syncdb.status_text(),
    })


@router.post("/diagnostics/request/{command}")
def request_1c(command: str, request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    try:
        task = onec.enqueue(db, command, user.username)
    except onec.OnecError as e:
        db.rollback()
        set_flash(request, f"Не поставлено: {e}.", "warn")
        return _back()
    log_action(db, user.username, "onec_requested", task.line)
    db.commit()
    set_flash(request, f"Задание {task.command} поставлено ({task.key}). Ответ придёт, когда "
                       f"отработает обработка 1С.", "good")
    return _back()


@router.post("/diagnostics/upload")
def upload(request: Request, kind: str = Form(...), file: UploadFile = File(...),
           db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Ручная загрузка файла из 1С (barcodes_*.txt / cost_*.txt) — пока обмен не настроен."""
    try:
        text = catalog.decode_upload(file.file.read())
        if kind == "dict":
            st = catalog.load_catalog(db, catalog.parse_barcode_dict(text))
            message = (f"Справочник загружен: баркодов {st['barcodes']}, товаров {st['products']} "
                       f"(новых {st['new_products']}).")
        else:
            parsed, bad = costs.parse_cost_file(text)
            if not parsed:
                raise catalog.CatalogError("в файле себестоимости нет ни одной строки (ждём uid|цена)")
            message = costs.summary(costs.import_costs(db, parsed), bad).capitalize() + "."
    except catalog.CatalogError as e:
        db.rollback()
        set_flash(request, f"Файл не принят: {e}", "warn")
        return _back()
    log_action(db, user.username, f"upload_{kind}", f"{file.filename}: {message}")
    db.commit()
    set_flash(request, message, "good")
    return _back()

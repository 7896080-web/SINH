"""«Диагностика»: обмен с 1С (себестоимость, справочник), фоновые задания, журнал."""
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import audit, backup, config, mapping, onec, settings
from priceapp.database import get_db
from priceapp.deps import get_current_user
from priceapp.flash import flash
from priceapp.models import AuditLog, OnecCost, OnecTask, User, WorkerHeartbeat
from priceapp.pages import render
from priceapp.workers.heartbeat import stale_workers

router = APIRouter()


def _back():
    return RedirectResponse("/diagnostics", status_code=303)


def _dirs() -> dict:
    if config.ONEC_SFTP_HOST:
        return {"обмен с 1С": f"SFTP {config.ONEC_SFTP_USER}@{config.ONEC_SFTP_HOST}:{config.ONEC_SFTP_PORT}",
                "задания": config.ONEC_SFTP_TASKS, "ответы": config.ONEC_SFTP_RESULTS,
                "архив на сервере": config.ONEC_SFTP_ARCHIVE, "архив здесь": config.ONEC_ARCHIVE_DIR,
                "копии базы": config.BACKUP_DIR}
    return {"обмен с 1С": "локальные папки", "задания": config.ONEC_TASKS_DIR,
            "ответы": config.ONEC_RESULTS_DIR, "архив": config.ONEC_ARCHIVE_DIR, "копии базы": config.BACKUP_DIR}


@router.get("/diagnostics")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return render(request, "diagnostics.html", user, "diagnostics",
                  ready_at=settings.get(db, settings.EPF_READY_AT),
                  cost_at=settings.get(db, settings.COST_LOADED_AT),
                  cost_rows=db.query(OnecCost).count(),
                  dict_at=settings.get(db, settings.DICT_LOADED_AT),
                  dict_rows=settings.get(db, settings.DICT_ROWS),
                  tasks=db.query(OnecTask).order_by(OnecTask.id.desc()).limit(50).all(),
                  beats=db.query(WorkerHeartbeat).order_by(WorkerHeartbeat.name).all(),
                  stale=stale_workers(db), last_backup=backup.last_backup(),
                  log=db.query(AuditLog).order_by(AuditLog.id.desc()).limit(50).all(), dirs=_dirs())


@router.post("/diagnostics/request/{what}")
def request_1c(what: str, request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    try:
        task = {"cost": onec.enqueue_cost, "dict": onec.enqueue_dict, "ping": onec.enqueue_ping}[what](db)
    except KeyError:
        return _back()
    except onec.OnecError as e:
        flash(request, str(e), "warn")
        return _back()
    audit.log(db, user.username, f"onec_{what}", task.order_id)
    db.commit()
    flash(request, f"Задание {task.command} поставлено. Ответ придёт, когда отработает обработка 1С "
                   f"(обычно 5–10 минут).", "ok")
    return _back()


@router.post("/diagnostics/upload")
def upload(request: Request, kind: str = Form(...), file: UploadFile = File(...),
           db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Ручная загрузка файла из 1С — когда обмен ещё не настроен."""
    try:
        text = mapping.decode_upload(file.file.read())
        if kind == "dict":
            st = mapping.load_dictionary(db, mapping.parse_barcode_dict(text), f"файл {file.filename}")
            msg = f"Справочник загружен: строк {st['rows']}, SKU {st['items']}."
        else:
            st = onec.load_costs(db, onec.parse_cost(text))
            msg = f"Себестоимость загружена: строк {st['rows']}, изменилось {st['changed']}."
    except (mapping.MappingError, onec.OnecError) as e:
        db.rollback()
        flash(request, f"Файл не принят: {e}", "warn")
        return _back()
    audit.log(db, user.username, f"upload_{kind}", file.filename or "", msg)
    db.commit()
    flash(request, msg, "ok")
    return _back()

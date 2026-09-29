from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from markapp import audit, backup, config, onec, settings
from markapp.database import get_db
from markapp.deps import get_current_user
from markapp.flash import flash
from markapp.models import AuditLog, OnecTask, User, WorkerHeartbeat
from markapp.pages import render
from markapp.workers.heartbeat import stale_workers

router = APIRouter()


@router.get("/diagnostics")
def diagnostics(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return render(request, "diagnostics.html", user, "diagnostics",
                  epf_version=settings.get(db, onec.EPF_VERSION),
                  epf_ping_at=settings.get(db, onec.EPF_PING_AT),
                  tasks=db.query(OnecTask).order_by(OnecTask.id.desc()).limit(50).all(),
                  stuck=db.query(OnecTask).filter(OnecTask.status == "timeout").count(),
                  beats=db.query(WorkerHeartbeat).order_by(WorkerHeartbeat.name).all(),
                  stale=stale_workers(db),
                  last_backup=backup.last_backup(),
                  remote=backup.RCLONE_REMOTE,
                  log=db.query(AuditLog).order_by(AuditLog.id.desc()).limit(50).all(),
                  dirs={"задания": config.ONEC_TASKS_DIR, "ответы": config.ONEC_RESULTS_DIR,
                        "архив": config.ONEC_ARCHIVE_DIR, "копии": config.BACKUP_DIR})


@router.post("/diagnostics/ping")
def diagnostics_ping(request: Request, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    task = onec.enqueue_ping(db)
    audit.log(db, user.username, "onec_ping", task.order_id)
    db.commit()
    flash(request, "PING отправлен в 1С. Старая обработка на него молчит — это и есть признак, "
                   "что обновление ещё не поставлено.", "ok")
    return RedirectResponse("/diagnostics", status_code=303)

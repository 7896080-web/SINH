from fastapi import APIRouter, Depends, Request
from pathlib import Path

from fastapi.responses import HTMLResponse, RedirectResponse
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
                  dirs=_dirs())


def _dirs() -> dict:
    """Куда на самом деле ходит обмен — чтобы при сбое было видно, что проверять."""
    if config.ONEC_SFTP_HOST:
        where = f"SFTP {config.ONEC_SFTP_USER}@{config.ONEC_SFTP_HOST}:{config.ONEC_SFTP_PORT}"
        return {"обмен с 1С": where, "задания": config.ONEC_SFTP_TASKS,
                "ответы": config.ONEC_SFTP_RESULTS, "архив на сервере": config.ONEC_SFTP_ARCHIVE,
                "архив здесь": config.ONEC_ARCHIVE_DIR, "копии": config.BACKUP_DIR}
    return {"обмен с 1С": "локальные папки", "задания": config.ONEC_TASKS_DIR,
            "ответы": config.ONEC_RESULTS_DIR, "архив": config.ONEC_ARCHIVE_DIR,
            "копии": config.BACKUP_DIR}


PLUGIN_CHECK = Path(__file__).resolve().parents[2] / "tools" / "plugin_check.html"


@router.get("/diagnostics/plugin-check")
def plugin_check(user: User = Depends(get_current_user)):
    """Проверка плагина КриптоПро — с ТОГО ЖЕ адреса, что и программа: доверие
    плагина зависит от адреса страницы, и проверка с file:// или другого порта
    ничего не сказала бы о работе программы."""
    return HTMLResponse(PLUGIN_CHECK.read_text(encoding="utf-8"))


@router.post("/diagnostics/ping")
def diagnostics_ping(request: Request, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    task = onec.enqueue_ping(db)
    audit.log(db, user.username, "onec_ping", task.order_id)
    db.commit()
    flash(request, "PING отправлен в 1С. Старая обработка на него молчит — это и есть признак, "
                   "что обновление ещё не поставлено.", "ok")
    return RedirectResponse("/diagnostics", status_code=303)

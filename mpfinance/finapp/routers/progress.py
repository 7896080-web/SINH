"""Шкалы прогресса: ход операции кнопки и фоновые задания.

- `/progress/op/<номер>` — что о себе сообщила операция, которую сейчас
  выполняет нажатая кнопка (`finapp/progress.py`).
- `/progress/jobs` — фоновые дела, которые идут дольше одного запроса:
  загрузки по API (WB отдаёт отчёт раз в минуту — это десятки минут), задания
  1С и отчёты, отправленные в 1С. Полоса над каждой страницей
  (`static/progress.js`) показывает их, пока они идут, и итог — когда
  закончились.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from finapp import config, onec, progress
from finapp.database import get_db
from finapp.deps import get_current_user
from finapp.finance import fetch
from finapp.models import (OnecTask, OnecTaskStatus, OneCUploadStatus, ReportFetch, SalesReport, User)
from finapp.timeutils import now_utc

router = APIRouter()

# Итог фонового дела показывается столько после его конца.
RECENT = timedelta(minutes=10)
# Сколько длится запрос детализации WB (лимит — один в минуту) — для оценки
# оставшегося времени.
WB_STEP_SECONDS = 61


@router.get("/progress/op/{op_id}")
def operation(op_id: str, user: User = Depends(get_current_user)):
    if not progress.valid_id(op_id):
        return JSONResponse({"error": "bad id"}, status_code=400)
    return JSONResponse(progress.get(op_id) or {})


def _fetch_item(job: ReportFetch) -> dict:
    account = job.account.name if job.account else ""
    period = (f"{job.date_from:%d.%m}–{job.date_to:%d.%m.%Y}" if job.date_from and job.date_to else "")
    label = " · ".join(p for p in (fetch.KINDS.get(job.kind, job.kind), account, period) if p)
    item = {"key": f"fetch:{job.id}", "label": label, "status": job.status,
            "done": job.progress_done or 0, "total": job.progress_total,
            "note": job.progress_note or ("в очереди — фон разбирает заявки раз в пять минут"
                                          if job.status == "pending" else "")}
    if job.status == "running" and job.kind == "wb_api" and job.progress_total:
        item["eta_seconds"] = max(0, job.progress_total - (job.progress_done or 0)) * WB_STEP_SECONDS
    if job.status in ("done", "failed"):
        item["note"] = job.message or ""
    return item


def _onec_stage(status: str) -> tuple[int, str]:
    if status == OnecTaskStatus.pending.value:
        return 1, "ждёт файла заданий — обмен с 1С раз в минуту"
    return 2, f"файл у 1С, ждём ответ (до {config.ONEC_TIMEOUT_MINUTES} мин)"


@router.get("/progress/jobs")
def jobs(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    since = now_utc() - RECENT
    active, finished = [], []
    for job in db.query(ReportFetch).filter(ReportFetch.status.in_(fetch.ACTIVE)).order_by(ReportFetch.id):
        active.append(_fetch_item(job))
    for job in db.query(ReportFetch).filter(ReportFetch.status.in_(("done", "failed")),
                                            ReportFetch.finished_at >= since):
        finished.append(_fetch_item(job))

    for task in db.query(OnecTask).filter(OnecTask.status.in_(onec.OPEN)).order_by(OnecTask.id):
        done, note = _onec_stage(task.status)
        active.append({"key": f"onec:{task.id}", "label": f"1С: {onec.COMMANDS.get(task.command, task.command)}",
                       "status": "running", "done": done, "total": 3, "note": note})
    for task in db.query(OnecTask).filter(OnecTask.status.notin_(onec.OPEN), OnecTask.answered_at >= since):
        finished.append({"key": f"onec:{task.id}", "label": f"1С: {onec.COMMANDS.get(task.command, task.command)}",
                         "status": "done" if task.status == OnecTaskStatus.done.value else "failed",
                         "done": 3, "total": 3, "note": task.result_detail or ""})

    open_reports = (OneCUploadStatus.pending, OneCUploadStatus.sent)
    for report in db.query(SalesReport).filter(SalesReport.onec_status.in_(open_reports)):
        done, note = _onec_stage(report.onec_status.value)
        active.append({"key": f"report1c:{report.id}", "label": f"Отчёт #{report.id} в 1С",
                       "status": "running", "done": done, "total": 3, "note": note,
                       "href": f"/finance/reports/{report.id}"})
    for report in db.query(SalesReport).filter(SalesReport.onec_status.notin_(open_reports + (OneCUploadStatus.none,)),
                                               SalesReport.onec_completed_at >= since):
        finished.append({"key": f"report1c:{report.id}", "label": f"Отчёт #{report.id} в 1С",
                         "status": "done" if report.onec_status == OneCUploadStatus.done else "failed",
                         "done": 3, "total": 3, "note": "", "href": f"/finance/reports/{report.id}"})
    return JSONResponse({"active": active, "finished": finished})

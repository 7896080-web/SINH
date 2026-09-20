"""Страница «Расхождения» — тот же отчёт, что воркер пишет в лог каждый час.

Отдельной страницей, а не разделом «Диагностики», намеренно. «Диагностика»
отвечает на вопрос «жива ли система»: heartbeat'ы, очереди, кнопки ручного
запуска. Отчёт отвечает на другой — «где система разошлась с реальностью и чем
это кончится». Смешав их, получаешь длинную страницу, которую читают по
диагонали. На «Диагностике» остаётся только сводка со ссылкой сюда.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import User
from app.report import CRITICAL, collect_findings, summary_line
from app.timeutils import now_utc

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# Как часто страница перезапрашивает себя сама. Отчёт собирается шестнадцатью
# проверками по базе — ежеминутный опрос гонял бы их впустую, а находки за
# минуту почти не меняются. Две минуты дают живую картину и не нагружают базу,
# в которую одновременно пишут веб и планировщик.
REFRESH_SECONDS = 120


def _context(db: Session) -> dict:
    findings = collect_findings(db)
    return {
        "findings": findings,
        "critical_count": sum(1 for f in findings if f.level == CRITICAL),
        "summary": summary_line(findings),
        "built_at": now_utc(),
        "refresh_seconds": REFRESH_SECONDS,
    }


@router.get("/report", response_class=HTMLResponse)
def report_page(request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    ctx = _context(db)
    ctx.update({"request": request, "current_user": user, "active_page": "report"})
    return templates.TemplateResponse(request, "report.html", ctx)


@router.get("/report/fragment", response_class=HTMLResponse)
def report_fragment(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Тело отчёта — его и перезапрашивает страница сама."""
    ctx = _context(db)
    ctx.update({"request": request, "current_user": user})
    return templates.TemplateResponse(request, "report_findings.html", ctx)

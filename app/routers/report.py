"""Страница «Расхождения» — тот же отчёт, что воркер пишет в лог каждый час.

Отдельной страницей, а не разделом «Диагностики», намеренно. «Диагностика»
отвечает на вопрос «жива ли система»: heartbeat'ы, очереди, кнопки ручного
запуска. Отчёт отвечает на другой — «где система разошлась с реальностью и чем
это кончится». Смешав их, получаешь длинную страницу, которую читают по
диагонали. На «Диагностике» остаётся только сводка со ссылкой сюда.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.models import User
from app.excel_utils import build_xlsx_response
from app.report import (CRITICAL, FULL_LISTS, collect_findings,
                        summary_line)
from app.timeutils import now_utc

router = APIRouter()
templates = shared_templates


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


# Потолок строк на странице. Список этот рабочий — с ним идут в кабинет площадки
# и правят мэппинг, — и осмысленная порция важнее полноты; полную даёт выгрузка.
# 152 тысячи строк в браузере уже пробовали, см. `products.FILTERED_LIMIT`.
ROWS_LIMIT = 1000


@router.get("/report/rows/{key}", response_class=HTMLResponse)
def finding_rows(key: str, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Полный список строк находки.

    Находка показывает десять и пишет «и ещё N — полный список по ссылке ниже».
    21.09 выяснилось, что ссылка вела в общий каталог товаров: обещание было,
    страницы не было, и оператор справедливо спросил, где ему это смотреть.
    """
    entry = FULL_LISTS.get(key)
    if entry is None:
        ctx = {"request": request, "current_user": user, "active_page": "report",
               "title": "Находка не найдена", "columns": [], "rows": [],
               "total": 0, "key": key, "limit": ROWS_LIMIT}
        return templates.TemplateResponse(request, "report_rows.html", ctx, status_code=404)

    title, columns, fn = entry
    rows = fn(db)
    return templates.TemplateResponse(request, "report_rows.html", {
        "request": request, "current_user": user, "active_page": "report",
        "title": title, "columns": columns, "rows": rows[:ROWS_LIMIT],
        "total": len(rows), "key": key, "limit": ROWS_LIMIT,
    })


@router.get("/report/rows/{key}/export")
def finding_rows_export(key: str, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Тот же список в .xlsx — целиком, без потолка страницы."""
    entry = FULL_LISTS.get(key)
    if entry is None:
        return build_xlsx_response(["Находка"], [[f"неизвестная находка: {key}"]],
                                   "находка.xlsx")
    title, columns, fn = entry
    return build_xlsx_response(columns, fn(db), f"{title}.xlsx")

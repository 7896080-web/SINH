"""Страница «Расхождения учёта со складом».

Отдельно от «Расхождений» (`/report`) намеренно, хотя названия похожи: там
НАХОДКИ — каждая про то, что система разошлась с реальностью и требует решения,
— а здесь рабочий список чисел, каждое из которых поставлено человеком и само по
себе верно. Смешав их, получаешь отчёт, в котором одна находка на шестьдесят
строк, и его перестают читать.

Страница обновляет себя сама (htmx): расхождение меняется от ввода факта, правки
руками, импорта файла и задания порога, а оператор держит её открытой и работает
по ней — руками он не перезагрузит и будет работать по вчерашнему списку.
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.discrepancies import EXPORT_LIMIT, PAGE_LIMIT, collect
from app.excel_utils import build_xlsx_response, format_dt
from app.models import User
from app.templating import templates as shared_templates
from app.timeutils import now_utc

router = APIRouter()
templates = shared_templates

# Реже, чем «Расхождения» (две минуты): список меняется от действий человека, а
# не сам по себе, и ежеминутный опрос гонял бы запрос по всему каталогу впустую.
REFRESH_SECONDS = 180


def _context(db: Session, only_negative: bool, only_broadcasting: bool,
             limit: int) -> dict:
    rows, total = collect(db, only_negative=only_negative,
                          only_broadcasting=only_broadcasting, limit=limit)
    return {
        "rows": rows, "total": total, "page_limit": limit,
        "only_negative": only_negative,
        "only_broadcasting": only_broadcasting,
        # Отметка времени живёт В ФРАГМЕНТЕ, а не в шапке: иначе после
        # автообновления данные свежие, а время от первой загрузки.
        "built_at": now_utc(),
        "refresh_seconds": REFRESH_SECONDS,
    }


@router.get("/discrepancies", response_class=HTMLResponse)
def discrepancies_page(request: Request, only_negative: bool = Query(False),
                       only_broadcasting: bool = Query(False),
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    ctx = _context(db, only_negative, only_broadcasting, PAGE_LIMIT)
    ctx.update({"request": request, "current_user": user,
                "active_page": "discrepancies"})
    return templates.TemplateResponse(request, "discrepancies.html", ctx)


@router.get("/discrepancies/rows", response_class=HTMLResponse)
def discrepancies_rows(request: Request, only_negative: bool = Query(False),
                       only_broadcasting: bool = Query(False),
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Фрагмент таблицы — его и перезапрашивает страница сама."""
    ctx = _context(db, only_negative, only_broadcasting, PAGE_LIMIT)
    ctx.update({"request": request, "current_user": user})
    return templates.TemplateResponse(request, "discrepancies_rows.html", ctx)


@router.get("/discrepancies/export")
def discrepancies_export(only_negative: bool = Query(False),
                         only_broadcasting: bool = Query(False),
                         db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """Выгрузка отдаёт РОВНО ТО, что отобрано на странице.

    Фильтры принимаются оба: `only_unfinished` на «Товарах» уже спотыкался —
    ссылка его передавала, эндпоинт не принимал, FastAPI молча отбрасывал, и
    оператор получал весь каталог вместо отобранного."""
    rows, _ = collect(db, only_negative=only_negative,
                      only_broadcasting=only_broadcasting, limit=EXPORT_LIMIT)
    headers = ["ID_1С", "Артикул", "Размер", "Цвет", "Наименование",
               "Расхождение", "Бронь", "Порог", "Остаток ЦС",
               "Уходит на площадки", "Трансляция",
               "Чем поставлено", "Кто", "Когда",
               "Дата расчёта", "Учёт 1С на дату", "Факт на дату"]
    data = [
        [r.uid_1c, r.article, r.size, r.color, r.name,
         r.discrepancy, r.reserve, r.offset, r.stock,
         r.outgoing, "Да" if r.broadcasting else "Нет",
         r.source, r.username, format_dt(r.measured_at),
         r.base_date.isoformat() if r.base_date else "",
         r.base_stock, r.fact]
        for r in rows
    ]
    return build_xlsx_response(headers, data, "расхождения_со_складом.xlsx")

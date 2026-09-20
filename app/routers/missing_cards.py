"""Страница «Есть на складе — нет на площадке».

Отдельно от «Расхождений» намеренно: там находки, каждая из которых требует
решения «чинить или нет», а здесь рабочий список, с которым идут заводить
карточки. Смешав их, получаешь отчёт, в котором одна находка на пятьсот строк.

Страница обновляет себя сама (htmx, раз в `REFRESH_SECONDS`): список меняется
после каждой выгрузки каталога кабинета и после прихода остатков из 1С, а
оператор держит её открытой и работает по ней — перезагружать руками он не
станет, и будет работать по вчерашнему списку.
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.excel_utils import build_xlsx_response
from app.missing_cards import (PAGE_LIMIT, PLATFORM_LABELS, collect_missing,
                               platforms_in_use)
from app.models import Platform, User
from app.timeutils import now_utc

router = APIRouter()
templates = shared_templates

# Пересчёт идёт по всему каталогу с остатком, поэтому реже, чем на «Расхождениях»:
# список меняется от выгрузки каталога (раз в сутки) и прихода из 1С (раз в час),
# ежеминутный опрос гонял бы тяжёлый запрос впустую.
REFRESH_SECONDS = 300

# Потолок выгрузки: столько строк собрать в .xlsx не жалко, а собрать весь
# каталог в память — уже жалко.
EXPORT_LIMIT = 20000


def _platform(value: str) -> Platform | None:
    try:
        return Platform(value)
    except ValueError:
        return None


def _context(db: Session, platform_value: str, limit: int) -> dict:
    platform = _platform(platform_value)
    rows, total = collect_missing(db, platform, limit=limit)
    used = platforms_in_use(db)
    return {
        "rows": rows, "total": total, "page_limit": limit,
        "platform": platform.value if platform else "",
        "platforms": [(p.value, PLATFORM_LABELS[p]) for p in used],
        "columns": [platform] if platform else used,
        "labels": PLATFORM_LABELS,
        "built_at": now_utc(),
        "refresh_seconds": REFRESH_SECONDS,
    }


@router.get("/report/missing-cards", response_class=HTMLResponse)
def missing_cards_page(request: Request, platform: str = Query(""),
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    ctx = _context(db, platform, PAGE_LIMIT)
    ctx.update({"request": request, "current_user": user, "active_page": "report"})
    return templates.TemplateResponse(request, "missing_cards.html", ctx)


@router.get("/report/missing-cards/rows", response_class=HTMLResponse)
def missing_cards_rows(request: Request, platform: str = Query(""),
                       db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Фрагмент таблицы — его и перезапрашивает страница сама."""
    ctx = _context(db, platform, PAGE_LIMIT)
    ctx.update({"request": request, "current_user": user})
    return templates.TemplateResponse(request, "missing_cards_rows.html", ctx)


@router.get("/report/missing-cards/export")
def missing_cards_export(platform: str = Query(""), db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """Выгрузка отдаёт РОВНО ТО, что отобрано на странице."""
    chosen = _platform(platform)
    rows, _ = collect_missing(db, chosen, limit=EXPORT_LIMIT)
    columns = [chosen] if chosen else platforms_in_use(db)

    headers = ["Артикул", "Наименование", "Размер", "Цвет", "Остаток ЦС"]
    headers += [f"Нет на {PLATFORM_LABELS[p]}" for p in columns]
    data = [
        [r.article, r.name, r.size, r.color, r.stock]
        + ["да" if r.missing.get(p) else "" for p in columns]
        for r in rows
    ]
    return build_xlsx_response(headers, data, "нет_на_площадке.xlsx")

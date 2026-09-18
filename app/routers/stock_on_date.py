"""Страница «Остатки на дату» (/stock-on-date).

Оператору регулярно нужен ответ на вопрос «сколько этого товара лежало на ЦС
такого-то числа»: разбор расхождения с площадкой, сверка с инвентаризацией,
проверка порога трансляции задним числом. В 1С такой отчёт есть, но ходить в неё
руками за каждой цифрой неудобно, а выгрузка `stock_*.txt` отвечает только про
«сейчас».

Здесь оператор заказывает срез на нужное число, планировщик уносит запрос в 1С
строкой `EXPORT_STOCK_ON_DATE|ГГГГММДД`, обработка кладёт ответ в `ondate_*.txt`,
и он появляется на этой же странице.

Главное ограничение, ради которого всё это живёт отдельно: цифры отсюда —
СПРАВКА. Они не попадают ни в остаток товара, ни в сверку, ни в очередь рассылки
на площадки (см. `ftp_channel.apply_stock_on_date_files`).
"""

from datetime import date, datetime

from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.audit import log_action
from app.database import get_db
from app.dependencies import get_current_user
from app.excel_utils import build_xlsx_response
from app.flash import set_flash, pop_flash
from app.models import StockDateRow, StockDateSnapshot, StockDateStatus, User
from app.offset_base import open_request
from app.timeutils import today_local

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Сколько строк показываем на странице. Выгрузка — это склад целиком (тысячи
# позиций), рисовать её всю в браузере незачем: для полного списка есть Excel.
ROWS_LIMIT = 300

STATUS_LABELS = {
    StockDateStatus.pending: "ждёт отправки в 1С",
    StockDateStatus.sent: "запрос ушёл, ждём ответа 1С",
    StockDateStatus.done: "готово",
    StockDateStatus.timeout: "1С не ответила",
}


# Правило «одна открытая заявка на дату» живёт в app/offset_base: оттуда же его
# применяет страница товаров, задавая дату расчёта порога. Двух копий быть не
# должно — разойдутся, и на одну дату уедет две заявки.
_open_request = open_request


def _snapshots(db: Session) -> list[StockDateSnapshot]:
    return db.query(StockDateSnapshot).order_by(StockDateSnapshot.id.desc()).all()


def _current_snapshot(db: Session, snapshot_id: str) -> StockDateSnapshot | None:
    # Значение приходит из строки запроса — мусор там не редкость (обрезанная
    # ссылка, ручная правка адреса), и превращать его в 500 незачем.
    if (snapshot_id or "").strip().isdigit():
        return db.query(StockDateSnapshot).filter(
            StockDateSnapshot.id == int(snapshot_id)).first()
    # По умолчанию — последняя ГОТОВАЯ: только что созданная заявка ещё пустая,
    # и открывать страницу на ней означало бы показывать пустую таблицу.
    return db.query(StockDateSnapshot).filter(
        StockDateSnapshot.status == StockDateStatus.done,
    ).order_by(StockDateSnapshot.id.desc()).first()


def _rows_query(db: Session, snapshot: StockDateSnapshot | None, q: str, nonzero: bool):
    if snapshot is None:
        return None
    query = db.query(StockDateRow).filter(StockDateRow.snapshot_id == snapshot.id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(StockDateRow.article.ilike(like),
                                 StockDateRow.name.ilike(like),
                                 StockDateRow.barcodes.ilike(like),
                                 StockDateRow.uid_1c.ilike(like)))
    if nonzero:
        query = query.filter(StockDateRow.quantity != 0)
    return query.order_by(StockDateRow.article.asc(), StockDateRow.size.asc())


def _render(request: Request, db: Session, user: User, template: str,
            snapshot_id: str, q: str, nonzero: bool):
    snapshot = _current_snapshot(db, snapshot_id)
    query = _rows_query(db, snapshot, q, nonzero)

    rows, total, quantity_total = [], 0, 0
    if query is not None:
        total = query.count()
        rows = query.limit(ROWS_LIMIT).all()
        quantity_total = sum(r.quantity for r in rows)

    return templates.TemplateResponse(request, template, {
        "request": request, "current_user": user, "active_page": "stock-on-date",
        "snapshots": _snapshots(db), "snapshot": snapshot, "rows": rows,
        "total": total, "shown": len(rows), "quantity_total": quantity_total,
        "rows_limit": ROWS_LIMIT, "q": q, "nonzero": nonzero,
        "status_labels": STATUS_LABELS,
        "today": today_local().isoformat(),
        "flash": pop_flash(request) if template == "stock_on_date.html" else None,
    })


@router.get("/stock-on-date", response_class=HTMLResponse)
def stock_on_date_page(
    request: Request, snapshot_id: str = Query(""), q: str = Query(""),
    nonzero: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, "stock_on_date.html", snapshot_id, q, nonzero)


@router.get("/stock-on-date/rows", response_class=HTMLResponse)
def stock_on_date_rows(
    request: Request, snapshot_id: str = Query(""), q: str = Query(""),
    nonzero: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, "stock_on_date_rows.html", snapshot_id, q, nonzero)


@router.post("/stock-on-date/request")
def request_stock_on_date(
    request: Request, value: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Заявка на срез. Сама 1С ничего не меняет по этой команде — только читает
    регистр остатков, поэтому подтверждения тут не спрашиваем."""
    try:
        snapshot_date = datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        set_flash(request, "Дата должна быть в формате ГГГГ-ММ-ДД.", "warn")
        return RedirectResponse("/stock-on-date", status_code=303)

    if snapshot_date > today_local():
        set_flash(request, "Остатков на будущую дату в 1С нет — выберите сегодняшнее или "
                           "прошедшее число.", "warn")
        return RedirectResponse("/stock-on-date", status_code=303)

    existing = _open_request(db, snapshot_date)
    if existing is not None:
        set_flash(request, f"Запрос на {snapshot_date:%d.%m.%Y} уже отправлен — "
                           f"{STATUS_LABELS[existing.status]}.", "info")
        return RedirectResponse("/stock-on-date", status_code=303)

    snapshot = StockDateSnapshot(snapshot_date=snapshot_date, requested_by=user.username)
    db.add(snapshot)
    log_action(db, user.username, "stock_on_date_requested", f"{snapshot_date:%Y-%m-%d}")
    db.commit()

    set_flash(request, f"Запрос остатков на {snapshot_date:%d.%m.%Y} поставлен в очередь. "
                       f"Ответ появится здесь, как только отработает обработка 1С.", "good")
    return RedirectResponse("/stock-on-date", status_code=303)


@router.get("/stock-on-date/{snapshot_id}/export")
def export_stock_on_date(
    snapshot_id: int, q: str = Query(""), nonzero: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    snapshot = db.query(StockDateSnapshot).filter(StockDateSnapshot.id == snapshot_id).first()
    if snapshot is None:
        return RedirectResponse("/stock-on-date", status_code=303)

    query = _rows_query(db, snapshot, q, nonzero)
    headers = ["ID_1С", "Артикул", "Наименование", "Размер", "Цвет", "Баркоды", "Остаток"]
    data = [[r.uid_1c, r.article, r.name, r.size, r.color, r.barcodes, r.quantity]
            for r in query.all()]

    return build_xlsx_response(headers, data,
                               f"остатки_1С_на_{snapshot.snapshot_date:%Y-%m-%d}.xlsx")

"""Стартовая страница «Что требует внимания».

Оператору не нужно обходить пять вкладок, чтобы понять, всё ли в порядке:
здесь всё, что требует решения, по убыванию цены ошибки, и каждая строка ведёт
прямо к своим строкам, а не на общую страницу. Страница только читает.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import overview, rates
from priceapp.database import get_db
from priceapp.deps import get_current_user
from priceapp.models import User
from priceapp.pages import render
from priceapp.routers.prices import product_rows

router = APIRouter()


@router.get("/attention")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from priceapp.workers import background
    att = overview.attention(db, product_rows, background_alive=background.heavy_alive())
    db.commit()     # get_rule мог завести правило площадки по умолчанию
    order = {"bad": 0, "warn": 1, "ok": 2}
    items = sorted(att.items, key=lambda i: order.get(i.level, 3))
    return render(request, "attention.html", user, "attention", items=items, rate=rates.current(db),
                  heavy_at=_local(att.heavy_at), heavy_dirty=att.heavy_dirty)


def _local(iso: str) -> str:
    """ISO UTC -> «02.10 16:21» по местному времени."""
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone().strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return ""


@router.post("/attention/refresh")
def refresh(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Пересчитать счётчики по товарам сейчас — на большом каталоге это до минуты."""
    overview.refresh_heavy(db, product_rows)
    return RedirectResponse("/attention", status_code=303)

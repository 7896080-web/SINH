"""Стартовая страница «Что требует внимания».

Оператору не нужно обходить пять вкладок, чтобы понять, всё ли в порядке:
здесь всё, что требует решения, по убыванию цены ошибки, и каждая строка ведёт
прямо к своим строкам, а не на общую страницу. Страница только читает.
"""
from fastapi import APIRouter, Depends, Request
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
    att = overview.attention(db, product_rows)
    db.commit()     # get_rule мог завести правило площадки по умолчанию
    order = {"bad": 0, "warn": 1, "ok": 2}
    items = sorted(att.items, key=lambda i: order.get(i.level, 3))
    return render(request, "attention.html", user, "attention", items=items, rate=rates.current(db))

"""Справка: типы цен, с которыми работает программа. Отдельной страницей, а не
листком у монитора: её открывают ровно тогда, когда число на экране непонятно."""
from fastapi import APIRouter, Depends, Request

from priceapp.deps import get_current_user
from priceapp.models import User
from priceapp.pages import render

router = APIRouter()


@router.get("/help/prices")
def prices_help(request: Request, user: User = Depends(get_current_user)):
    return render(request, "help_prices.html", user, "help")

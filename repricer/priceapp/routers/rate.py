"""Страница «Курс $»: курс ЦБ, ручной режим, история."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import audit, rates, settings
from priceapp.database import get_db
from priceapp.deps import get_current_user
from priceapp.flash import flash
from priceapp.models import ExchangeRate, User
from priceapp.pages import render

router = APIRouter()
HTTP_SESSION = None   # подменяется в тестах


@router.get("/rate")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return render(request, "rate.html", user, "rate",
                  current=rates.current(db), mode=settings.get(db, settings.RATE_MODE),
                  manual=settings.get(db, settings.RATE_MANUAL), cbr=rates.latest_cbr(db),
                  history=db.query(ExchangeRate).order_by(ExchangeRate.rate_date.desc(),
                                                          ExchangeRate.id.desc()).limit(30).all())


@router.post("/rate/refresh")
def refresh(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        row = rates.fetch_cbr(db, HTTP_SESSION)
        flash(request, f"Курс ЦБ на {row.rate_date:%d.%m.%Y}: {row.usd_rub} ₽ за $1.", "ok")
    except rates.RateError as e:
        flash(request, f"Курс не получен: {e}", "error")
    return RedirectResponse("/rate", status_code=303)


@router.post("/rate/mode")
async def set_mode(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    form = await request.form()
    mode = "manual" if form.get("mode") == "manual" else "cbr"
    manual = str(form.get("manual") or "").strip()
    if mode == "manual":
        try:
            manual = str(rates.parse_manual(manual))
        except rates.RateError as e:
            flash(request, f"Ручной курс не сохранён: {e}", "warn")
            return RedirectResponse("/rate", status_code=303)
    before = (settings.get(db, settings.RATE_MODE), settings.get(db, settings.RATE_MANUAL))
    settings.put(db, settings.RATE_MODE, mode)
    if mode == "manual":
        settings.put(db, settings.RATE_MANUAL, manual)
    audit.log(db, user.username, "rate_mode", mode, f"было {before}, стало {(mode, manual)}")
    db.commit()
    flash(request, ("Считаем по ручному курсу " + manual + " ₽." if mode == "manual"
                    else "Считаем по курсу ЦБ.") + " Цены не изменились — нужен пересчёт на странице «Цены».", "ok")
    return RedirectResponse("/rate", status_code=303)

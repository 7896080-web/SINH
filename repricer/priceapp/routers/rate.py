"""Страница «Курс $»: курс ЦБ, ручной режим, история."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from decimal import Decimal, InvalidOperation

from priceapp import audit, overview, rates, settings
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
                  alert=settings.get(db, settings.RATE_ALERT_PERCENT), shift=overview.rate_shift(db),
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



@router.post("/rate/alert")
async def set_alert(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Порог предупреждения «курс ушёл от курса последнего расчёта». Только
    предупреждает: цены программа сама не пересчитывает и не отправляет."""
    form = await request.form()
    raw = str(form.get("alert") or "").strip().replace(",", ".")
    try:
        value = Decimal(raw)
        if not value.is_finite() or value <= 0 or value > 50:
            raise InvalidOperation
    except InvalidOperation:
        flash(request, f"Порог «{raw}» не принят: число процентов от 0 до 50.", "warn")
        return RedirectResponse("/rate", status_code=303)
    settings.put(db, settings.RATE_ALERT_PERCENT, f"{value.normalize():f}")
    audit.log(db, user.username, "rate_alert", str(value))
    db.commit()
    flash(request, f"Предупреждать, если курс ушёл от курса расчёта больше чем на {value.normalize():f}%.", "ok")
    return RedirectResponse("/rate", status_code=303)

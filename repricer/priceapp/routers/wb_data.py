"""Страница «Данные WB»: что программа копит из WB для оценки цен и акций (`priceapp/wb_data.py`) — по кабинету
и набору: сколько собрано и за какие дни, когда последний раз, что мешает (у токена нет категории) и когда
следующая попытка. Сам сбор идёт фоном; кнопка только снимает паузу после отказа."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp import audit, wb_data
from priceapp.database import get_db
from priceapp.deps import get_current_user, posted_form
from priceapp.flash import flash
from priceapp.models import User, WorkerHeartbeat
from priceapp.pages import render

router = APIRouter()


@router.get("/wb-data")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return render(request, "wb_data.html", user, "wb_data", rows=wb_data.status(db),
                  beat=db.get(WorkerHeartbeat, "wb_data"), labels=wb_data.LABELS, keep_days=wb_data.KEEP_DAYS,
                  funnel_days=wb_data.FUNNEL_BACK_DAYS + 1)


@router.post("/wb-data/retry")
def retry(request: Request, form=Depends(posted_form), db: Session = Depends(get_db),
          user: User = Depends(get_current_user)):
    try:
        acc = int(form.get("acc") or 0)
    except ValueError:
        acc = 0
    n = wb_data.retry_now(db, acc)
    audit.log(db, user.username, "wb_data_retry", str(acc or ""), f"сбор данных WB: снята пауза после отказа — {n}")
    db.commit()
    flash(request, "Пауза снята — сбор попробует снова в течение минуты." if n else "Отказов нет — сбор и так идёт.",
          "ok" if n else "info")
    return RedirectResponse("/wb-data", status_code=303)

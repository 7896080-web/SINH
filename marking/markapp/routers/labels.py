"""Страница «Этикетки»: файл кодов → PDF 58×40.

Коды в базе НЕ сохраняются (реестр кодов — этап 4): страница собирает PDF из
загруженного файла и отдаёт его. Если есть предупреждения (GTIN без артикула),
человек видит их ДО печати: PDF держится в памяти веба десять минут под
случайным ключом и скачивается со страницы предупреждений.
"""
import secrets
import time
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from markapp import audit, labels as L, settings
from markapp.database import get_db
from markapp.deps import get_current_user
from markapp.flash import flash
from markapp.models import User
from markapp.pages import render
from markapp.timeutils import ru, today_local

router = APIRouter()
MAX_UPLOAD = 5 * 1024 * 1024
_READY: dict[str, tuple[float, bytes, str]] = {}
TTL = 600


def _pdf_response(data: bytes, name: str) -> Response:
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


def _cleanup():
    now = time.time()
    for k in [k for k, (t, _, _) in _READY.items() if t < now]:
        _READY.pop(k, None)


@router.get("/labels")
def labels_page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return render(request, "labels.html", user, "labels", today=today_local(),
                  t_title=settings.get(db, L.LABEL_TITLE), t_right=settings.get(db, L.LABEL_RIGHT),
                  t_bottom=settings.get(db, L.LABEL_BOTTOM), module=settings.get(db, L.LABEL_MODULE),
                  placeholders=L.PLACEHOLDERS, warnings=None, token=None)


@router.post("/labels/print")
async def labels_print(request: Request, file: UploadFile = File(...), label_date: str = Form(""),
                       supply_number: str = Form(""), db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        flash(request, "Файл больше 5 МБ.", "error")
        return RedirectResponse("/labels", status_code=303)
    text = raw.decode("utf-8-sig", errors="replace")
    codes, errors = L.parse_codes(text)
    if errors:
        flash(request, f"Этикетки не сделаны: ошибок {len(errors)} — " + "; ".join(errors[:4]), "error")
        return RedirectResponse("/labels", status_code=303)
    if not codes:
        flash(request, "В файле нет кодов.", "error")
        return RedirectResponse("/labels", status_code=303)
    try:
        d = date.fromisoformat(label_date) if label_date else today_local()
    except ValueError:
        d = today_local()
    data = L.label_values(db, codes, settings.lamoda_org(db), ru(d), supply_number.strip())
    try:
        pdf, warnings = L.build_pdf(db, data)
    except L.LabelError as e:
        flash(request, f"Этикетки не выданы: {e}", "error")
        return RedirectResponse("/labels", status_code=303)
    name = f"Этикетки_{supply_number.strip() or ru(d)}_{len(codes)}шт.pdf"
    audit.log(db, user.username, "labels_printed", file.filename or "",
              f"{len(codes)} этикеток; предупреждений {len(warnings)}")
    db.commit()
    if not warnings:
        return _pdf_response(pdf, name)
    _cleanup()
    token = secrets.token_urlsafe(16)
    _READY[token] = (time.time() + TTL, pdf, name)
    return render(request, "labels.html", user, "labels", today=today_local(),
                  t_title=settings.get(db, L.LABEL_TITLE), t_right=settings.get(db, L.LABEL_RIGHT),
                  t_bottom=settings.get(db, L.LABEL_BOTTOM), module=settings.get(db, L.LABEL_MODULE),
                  placeholders=L.PLACEHOLDERS, warnings=warnings, token=token, count=len(codes))


@router.get("/labels/ready/{token}")
def labels_ready(token: str, request: Request, user: User = Depends(get_current_user)):
    _cleanup()
    item = _READY.pop(token, None)
    if item is None:
        flash(request, "Файл уже скачан или устарел — загрузите коды ещё раз.", "error")
        return RedirectResponse("/labels", status_code=303)
    return _pdf_response(item[1], item[2])


@router.post("/labels/settings")
def labels_settings(request: Request, t_title: str = Form(""), t_right: str = Form(""),
                    t_bottom: str = Form(""), module: str = Form("0.5"),
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    problems = L.check_template(t_title + t_right + t_bottom)
    try:
        m = float(module.replace(",", "."))
        if not 0.3 <= m <= 1.0:
            raise ValueError
    except ValueError:
        problems.append("размер модуля — от 0,3 до 1 мм (203 dpi: 0,5; 300 dpi: 0,508)")
    if problems:
        flash(request, "Не сохранено: " + "; ".join(problems), "error")
        return RedirectResponse("/labels", status_code=303)
    settings.put(db, L.LABEL_TITLE, t_title.strip())
    settings.put(db, L.LABEL_RIGHT, t_right.strip())
    settings.put(db, L.LABEL_BOTTOM, t_bottom.strip())
    settings.put(db, L.LABEL_MODULE, str(m))
    audit.log(db, user.username, "label_template", details=f"{t_title} | {t_right} | {t_bottom} | {m}")
    db.commit()
    flash(request, "Шаблон этикетки сохранён.", "ok")
    return RedirectResponse("/labels", status_code=303)

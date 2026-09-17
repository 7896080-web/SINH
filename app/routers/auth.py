from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.audit import log_action
from app.database import get_db
from app.models import User
from app.security import verify_password
from app.login_security import (
    is_locked, lockout_remaining_minutes, record_login_failure, record_login_success,
    MAX_FAILED_ATTEMPTS,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/mapping", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"request": request})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username, User.is_active.is_(True)).first()

    # Аккаунт заблокирован — не тратим время на проверку пароля вообще,
    # и не сбрасываем счётчик неудачным паролем, введённым во время блокировки.
    if user and is_locked(user):
        minutes = lockout_remaining_minutes(user)
        return templates.TemplateResponse(request, 
            "login.html",
            {
                "request": request, "username": username,
                "error": f"Слишком много неудачных попыток. Попробуйте снова через {minutes} мин.",
            },
            status_code=429,
        )

    if not user or not verify_password(password, user.password_hash):
        error = "Неверный логин или пароль"
        if user:
            just_locked = record_login_failure(db, user)
            # Вход в журнале действий: система работает с боевыми токенами
            # площадок и остатками, и «кто и когда заходил» — такая же часть
            # аудита, как смена склада или токена. Пароль, разумеется, не пишем.
            log_action(db, username, "login_locked" if just_locked else "login_failed",
                       f"неудачная попытка входа ({user.failed_login_attempts})")
            db.commit()
            if just_locked:
                error = "Слишком много неудачных попыток. Аккаунт заблокирован на 30 минут."
            else:
                remaining = MAX_FAILED_ATTEMPTS - user.failed_login_attempts
                error = f"Неверный логин или пароль. Осталось попыток: {remaining}."
        return templates.TemplateResponse(request, 
            "login.html",
            {"request": request, "error": error, "username": username},
            status_code=401,
        )

    record_login_success(db, user)
    log_action(db, user.username, "login_ok", "вход выполнен")
    db.commit()

    request.session["user_id"] = user.id
    return RedirectResponse("/mapping", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

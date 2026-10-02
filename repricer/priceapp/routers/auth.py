from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from priceapp.database import get_db
from priceapp.models import User
from priceapp.security import verify_password
from priceapp.templating import templates

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html",
                                      {"request": request, "error": None,
                                       "current_user": None, "active_page": None, "flash": None})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username.strip(), User.is_active.is_(True)).first()
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "login.html",
                                          {"request": request, "error": "Неверный логин или пароль",
                                           "current_user": None, "active_page": None, "flash": None},
                                          status_code=401)
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/attention", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

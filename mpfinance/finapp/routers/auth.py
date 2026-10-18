from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from finapp.database import get_db
from finapp.models import User
from finapp.security import verify_password
from finapp.templating import templates

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None, "username": ""})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username.strip(), User.is_active.is_(True)).first()
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "login.html",
                                          {"error": "Неверный логин или пароль", "username": username},
                                          status_code=401)
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/finance", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

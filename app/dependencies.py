from fastapi import Request, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User


class NotAuthenticated(Exception):
    """Поднимается, если в сессии нет действующего пользователя.
    Перехватывается обработчиком в app/main.py и превращается в редирект на /login.
    """


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise NotAuthenticated()
    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if not user:
        raise NotAuthenticated()
    return user

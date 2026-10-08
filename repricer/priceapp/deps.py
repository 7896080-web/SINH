from fastapi import Depends, Request
from sqlalchemy.orm import Session

from priceapp.database import get_db
from priceapp.models import User


class NotAuthenticated(Exception):
    """Нет действующего пользователя в сессии — редирект на вход (main.py)."""


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise NotAuthenticated()
    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if user is None:
        raise NotAuthenticated()
    return user


async def posted_form(request: Request):
    """Тело формы — асинхронной зависимостью, чтобы сам обработчик мог быть
    обычным `def`. FastAPI выполняет такие в пуле потоков: тяжёлая работа с
    базой (массовая наценка, «Передать» по каталогу — десятки секунд) больше не
    занимает цикл событий, и остальные страницы открываются, пока она идёт.
    `async def` обработчик с синхронной работой внутри замораживал ВЕСЬ интерфейс
    (аудит 08.10: «Справка» — 6,5 с вместо 0,01 с во время «Установить»)."""
    return await request.form()

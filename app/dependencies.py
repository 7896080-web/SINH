from fastapi import Request, Depends
from sqlalchemy.orm import Session

from app import access
from app.database import get_db
from app.models import User


class NotAuthenticated(Exception):
    """Поднимается, если в сессии нет действующего пользователя.
    Перехватывается обработчиком в app/main.py и превращается в редирект на /login.
    """


class Forbidden(Exception):
    """Пользователь вошёл, но этой страницы его роли не полагается.

    Отдельно от `NotAuthenticated` намеренно: там человек не представился и его
    надо отправить на вход, а здесь он представился — редирект на `/login`
    выглядел бы как «вас разлогинило» и отправил бы кладовщика по кругу.
    """

    def __init__(self, home: str, user: User):
        # Куда ему всё-таки можно: страница отказа даёт ссылку, а не тупик.
        self.home = home
        # И КТО пришёл. Страница отказа — обычная страница приложения: у неё та
        # же шапка и то же меню, а меню спрашивает роль. Несём пользователя с
        # собой, а не ищем его заново в обработчике: здесь он уже в руках, и
        # второй запрос к базе однажды разошёлся бы с первым.
        self.user = user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise NotAuthenticated()
    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if not user:
        raise NotAuthenticated()
    # Проверка роли стоит ЗДЕСЬ, а не в каждом обработчике, и это не экономия
    # строк: забыть дописать её в новый роутер нельзя, потому что страница без
    # этой зависимости не открывается вовсе — она не знает, кто пришёл. То есть
    # единственный способ «обойти» проверку закрывает страницу всем сразу, а не
    # тихо открывает её складу.
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    if not access.allowed(role, request.url.path):
        raise Forbidden(access.home_for(role), user)
    return user

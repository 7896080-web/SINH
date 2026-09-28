import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy.orm import Session

from app import access
from app.database import Base, engine, get_db
from app.models import User
from app.templating import templates
from app.dependencies import Forbidden, NotAuthenticated
from app.routers import (
    auth, api_keys, mapping, products, anomalies, health, diagnostics, testing,
    platform_matching, stock_on_date, report, missing_cards, notifications,
    discrepancies, returns,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создаёт таблицы, если их ещё нет. Для боевого использования предпочтительнее
    # Alembic-миграции — оставлено как быстрый старт для разработки/тестового контура.
    Base.metadata.create_all(bind=engine)
    yield


# Автодокументация FastAPI выключена ВСЯ (`/docs`, `/redoc`, `/openapi.json`).
# Она открыта БЕЗ входа — зависимости `get_current_user` у неё нет, — то есть
# любой, кто дотянулся до порта, получал полную карту приложения: адреса всех
# ручек, имена и типы полей форм, включая массовые правки остатков. Публичного
# API у нас нет вовсе, страницы отдаёт сам сервер разметкой, так что цена
# выключения — ноль, а цена включения отложенная и потому незаметная.
app = FastAPI(title="Синхронизация площадок", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

SESSION_SECRET = os.environ.get("SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("Не задана переменная окружения SESSION_SECRET")

# Secure-флаг на сессионную cookie. По умолчанию выключен — иначе браузер не
# отдаёт cookie по обычному HTTP (локальная разработка, тесты, сервер без SSL).
# В проде за HTTPS ОБЯЗАТЕЛЬНО выставить SESSION_COOKIE_SECURE=1 (install.sh
# делает это автоматически, когда SSL выпущен).
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=SESSION_COOKIE_SECURE,
    max_age=60 * 60 * 8,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(api_keys.router)
app.include_router(mapping.router)
app.include_router(platform_matching.router)
app.include_router(products.router)
app.include_router(anomalies.router)
app.include_router(health.router)
app.include_router(diagnostics.router)
app.include_router(report.router)
app.include_router(missing_cards.router)
app.include_router(discrepancies.router)
app.include_router(returns.router)
app.include_router(testing.router)
app.include_router(stock_on_date.router)
app.include_router(notifications.router)


@app.exception_handler(NotAuthenticated)
def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
def handle_forbidden(request: Request, exc: Forbidden):
    """Страницы этой роли не полагается.

    Не редирект: молча увести человека на другую страницу — значит оставить его
    гадать, нажалась ли ссылка. Говорим прямо и даём ссылку туда, где ему есть
    что делать. Код 403, а не 404: прятать сам факт существования страницы не от
    кого — это внутренняя админка, а не публичный сайт.
    """
    # `current_user` обязателен: шапку и меню рисует тот же `base.html`, и без
    # него страница отказа падает с ошибкой шаблона — то есть отказ превращается
    # в 500, а человек видит не «этой страницы нет», а «всё сломалось».
    return templates.TemplateResponse(request, "forbidden.html",
                                      {"request": request, "home": exc.home,
                                       "current_user": exc.user,
                                       "active_page": None},
                                      status_code=403)


@app.get("/")
def root(request: Request, db: Session = Depends(get_db)):
    """Корень ведёт туда, где человеку есть что делать.

    Складская учётная запись на `/mapping` получила бы отказ сразу после
    успешного входа и решила бы, что её сломали.
    """
    user_id = request.session.get("user_id")
    if user_id:
        user = db.query(User).filter(User.id == user_id,
                                     User.is_active.is_(True)).first()
        if user is not None:
            role = user.role.value if hasattr(user.role, "value") else str(user.role)
            return RedirectResponse(access.home_for(role), status_code=303)
    # Не вошли — как было: на общую страницу, она сама отправит на вход.
    # Менять этот путь вместе с ролями незачем, он и так верен.
    return RedirectResponse("/mapping", status_code=303)

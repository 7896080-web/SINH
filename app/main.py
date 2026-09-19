import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.database import Base, engine
from app.dependencies import NotAuthenticated
from app.routers import (
    auth, api_keys, mapping, products, anomalies, health, diagnostics, testing,
    platform_matching, stock_on_date, report,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создаёт таблицы, если их ещё нет. Для боевого использования предпочтительнее
    # Alembic-миграции — оставлено как быстрый старт для разработки/тестового контура.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Синхронизация площадок", lifespan=lifespan)

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
app.include_router(testing.router)
app.include_router(stock_on_date.router)


@app.exception_handler(NotAuthenticated)
def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def root():
    return RedirectResponse("/mapping", status_code=303)

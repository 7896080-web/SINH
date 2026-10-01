import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from markapp.database import Base, SessionLocal, engine
from markapp.deps import NotAuthenticated
from markapp.routers import (auth, catalog, diagnostics, gtin, health, labels, mapping,
                             organizations, supplies)
from markapp.settings import ensure_defaults


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Схему ведёт alembic; create_all — только чтобы свежая разработческая база
    # поднималась без ручных шагов. На существующие таблицы он не влияет.
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_defaults(db)
    finally:
        db.close()
    # На рабочем компьютере службы-воркера нет — фоновая работа в этом же
    # процессе. На сервере со службой marking_worker её выключают: MARKING_BACKGROUND=0.
    background = os.environ.get("MARKING_BACKGROUND", "1") != "0"
    if background:
        from markapp.workers import background as bg
        bg.start()
    yield
    if background:
        bg.stop()


# Автодокументация выключена: она открыта без входа и отдаёт карту всех ручек.
app = FastAPI(title="Маркировка и поставки", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

SESSION_SECRET = os.environ.get("MARKING_SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("Не задана переменная окружения MARKING_SESSION_SECRET")

# Имя cookie своё: sync_admin на той же машине тоже ставит «session», и при
# общем имени вход в одну программу выбивал бы из другой (обе на 127.0.0.1).
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax",
                   session_cookie="marking_session", max_age=60 * 60 * 10)

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")

for r in (auth, supplies, catalog, mapping, gtin, labels, organizations, diagnostics, health):
    app.include_router(r.router)


@app.exception_handler(NotAuthenticated)
def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def root():
    return RedirectResponse("/supplies", status_code=303)

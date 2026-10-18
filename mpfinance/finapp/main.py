import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from finapp.deps import NotAuthenticated
from finapp.routers import accounts, auth, diagnostics, finance, health
from finapp.templating import STATIC_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Схему ведёт alembic (накат и установка зовут `alembic upgrade head` до
    # запуска службы); здесь только журнал и фон.
    app_log = logging.getLogger("mpfinance")
    if not app_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        app_log.addHandler(handler)
        app_log.setLevel(logging.INFO)
    background = os.environ.get("MPFIN_BACKGROUND", "1") != "0"
    if background:
        from finapp.workers import background as bg
        bg.start()
    yield
    if background:
        bg.stop()


# Автодокументация выключена: она открыта без входа и отдаёт карту всех ручек.
app = FastAPI(title="Финансы площадок", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)

SESSION_SECRET = os.environ.get("MPFIN_SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("Не задана переменная окружения MPFIN_SESSION_SECRET")

# Имя cookie своё: sync_admin и «Репрайсер» на той же машине ставят свои, и при
# общем имени вход в одну программу выбивал бы из другой (все на 127.0.0.1).
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax",
                   session_cookie="mpfinance_session", max_age=60 * 60 * 10)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

for r in (auth, finance, accounts, diagnostics, health):
    app.include_router(r.router)


@app.exception_handler(NotAuthenticated)
def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def root():
    return RedirectResponse("/finance", status_code=303)

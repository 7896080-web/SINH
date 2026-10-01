import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from priceapp.database import Base, SessionLocal, engine
from priceapp.deps import NotAuthenticated
from priceapp.routers import accounts, auth, diagnostics, health, mapping, prices, rate
from priceapp.settings import ensure_defaults


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Схему ведёт alembic; create_all — чтобы свежая база поднималась без ручных шагов.
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_defaults(db)
    finally:
        db.close()
    background = os.environ.get("REPRICER_BACKGROUND", "1") != "0"
    if background:
        from priceapp.workers import background as bg
        bg.start()
    yield
    if background:
        bg.stop()


# Автодокументация выключена: она открыта без входа и отдаёт карту всех ручек.
app = FastAPI(title="Репрайсер", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

SESSION_SECRET = os.environ.get("REPRICER_SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("Не задана переменная окружения REPRICER_SESSION_SECRET")

# Имя cookie своё: sync_admin и «Маркировка» на той же машине ставят свои, и при
# общем имени вход в одну программу выбивал бы из другой (все на 127.0.0.1).
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax",
                   session_cookie="repricer_session", max_age=60 * 60 * 10)

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")

for r in (auth, prices, mapping, rate, accounts, diagnostics, health):
    app.include_router(r.router)


@app.exception_handler(NotAuthenticated)
def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def root():
    return RedirectResponse("/prices", status_code=303)

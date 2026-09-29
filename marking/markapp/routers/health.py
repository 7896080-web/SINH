"""/health — жива ли программа: база отвечает, фоновые задания свежие.

Без входа: его опрашивает накат после перезапуска служб. Отдаёт только
имена заданий и их возраст — ничего, что стоило бы прятать.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from markapp.database import get_db
from markapp.workers.heartbeat import stale_workers

router = APIRouter()


@router.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        return JSONResponse({"ok": False, "db": str(e)[:200]}, status_code=503)
    stale = stale_workers(db)
    body = {"ok": not stale, "stale": stale}
    return JSONResponse(body, status_code=200 if not stale else 503)

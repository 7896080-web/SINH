"""/health — жива ли программа: база отвечает, фоновые задания свежие.

Без входа: его опрашивает накат после перезапуска служб. Отдаёт только
имена заданий и их возраст — ничего, что стоило бы прятать.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from priceapp.database import get_db
from priceapp.workers.heartbeat import stale_workers

router = APIRouter()


@router.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        return JSONResponse({"ok": False, "db": str(e)[:200]}, status_code=503)
    from priceapp.workers import background
    stale = stale_workers(db)
    dead = background.dead_threads()
    body = {"ok": not stale and not dead, "stale": stale, "dead_threads": dead}
    return JSONResponse(body, status_code=200 if body["ok"] else 503)

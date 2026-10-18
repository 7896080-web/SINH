"""/health — жива ли программа: база отвечает, фоновые задания свежие.

Без входа: его опрашивает накат после перезапуска службы. Отдаёт только имена
заданий и их возраст — ничего, что стоило бы прятать.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from finapp.database import get_db
from finapp.workers import background
from finapp.workers.heartbeat import stale_workers

router = APIRouter()


@router.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:                       # noqa: BLE001
        return JSONResponse({"ok": False, "db": str(e)[:200]}, status_code=503)
    stale = stale_workers(db)
    dead = background.dead_threads()
    body = {"ok": not stale and not dead, "stale": stale, "dead_threads": dead}
    return JSONResponse(body, status_code=200 if body["ok"] else 503)

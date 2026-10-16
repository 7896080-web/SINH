"""Ход массового действия для шкалы прогресса (`priceapp/progress.py`, `static/busy.js`)."""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from priceapp import progress
from priceapp.deps import get_current_user
from priceapp.models import User

router = APIRouter()


@router.get("/progress/{token}")
def state(token: str, user: User = Depends(get_current_user)):
    s = progress.get(token) if progress.TOKEN_RE.match(token) else None
    return JSONResponse(s or {"stage": "", "done": 0, "total": None, "finished": False, "stage_seconds": 0},
                        headers={"Cache-Control": "no-store"})

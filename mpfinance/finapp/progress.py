"""Ход долгой операции для шкалы прогресса на кнопке.

Кнопка отправляет форму в фоне (`static/progress.js`) и передаёт заголовок
`X-Progress-Id` — случайный номер операции. Пока запрос идёт, страница раз в
полсекунды спрашивает `/progress/op/<номер>` и рисует то, что операция о себе
сообщила (`report`). Операция, которая о себе не сообщает, ничего не ломает:
страница тогда показывает «выполняется» без процентов.

Хранится в памяти процесса — служба одна (uvicorn без воркеров), а ход
операции нужен ровно пока идёт запрос. В тестах и в фоновых заданиях номера
нет, и `report` ничего не делает.
"""
from __future__ import annotations

import re
import threading
import time
from contextvars import ContextVar

_current: ContextVar[str | None] = ContextVar("mpfin_progress_op", default=None)
_ops: dict[str, dict] = {}
_lock = threading.Lock()
# Ход операции живёт дольше самого запроса: последний опрос страницы может
# прийти чуть позже ответа. Старше — убираем, чтобы словарь не рос.
KEEP_SECONDS = 600
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def valid_id(op_id: str | None) -> bool:
    return bool(op_id) and bool(_ID_RE.match(op_id))


def begin(op_id: str | None):
    """Привязать текущий запрос к номеру операции (зовёт middleware)."""
    if not valid_id(op_id):
        return None
    with _lock:
        now = time.monotonic()
        for key in [k for k, v in _ops.items() if now - v["at"] > KEEP_SECONDS]:
            del _ops[key]
        _ops[op_id] = {"done": None, "total": None, "note": "", "at": now}
    return _current.set(op_id)


def end(token) -> None:
    if token is not None:
        _current.reset(token)


def report(done: int | None = None, total: int | None = None, note: str | None = None) -> None:
    """Сообщить ход: `done` из `total` (шаги, строки, письма) и что сейчас
    делается. Без привязанной операции — ничего."""
    op_id = _current.get()
    if op_id is None:
        return
    with _lock:
        entry = _ops.get(op_id)
        if entry is None:
            return
        if done is not None:
            entry["done"] = max(0, int(done))
        if total is not None:
            entry["total"] = max(0, int(total))
        if note is not None:
            entry["note"] = note[:200]
        entry["at"] = time.monotonic()


def every(n: int, i: int, total: int, note: str) -> None:
    """`report` на каждой n-й строке цикла — чтобы не брать замок на каждой."""
    if i % n == 0 or i == total:
        report(i, total, note)


def get(op_id: str) -> dict | None:
    with _lock:
        entry = _ops.get(op_id)
        return None if entry is None else {k: entry[k] for k in ("done", "total", "note")}

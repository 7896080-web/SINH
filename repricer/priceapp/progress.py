"""Шкала прогресса массовых действий.

Массовое действие («Передать» по каталогу, «Рассчитать», импорт файла, «Привести все площадки к
эталону») идёт в одном запросе десятки секунд — а бегущая полоса без чисел не говорит, сколько
сделано и сколько ждать: человек жмёт второй раз или решает, что программа зависла.

Страница (static/busy.js) при отправке формы дописывает к адресу номер операции `_progress`
и, пока запрос идёт, раз в полсекунды спрашивает `/progress/<номер>`. Обработчик, помеченный
`@tracked`, этот номер подхватывает, а код глубже отмечает ход работы — `stage()` (этап; со
счётом или без) и `each()` (цикл со счётом). Без номера всё это — пустые вызовы: фоновые
задания и тесты не платят ничего.

Хранится в памяти процесса: служба одна, а ход операции после её конца не нужен никому.
"""
from __future__ import annotations

import contextvars
import functools
import re
import threading
import time

_store: dict[str, dict] = {}
_lock = threading.Lock()
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("progress", default=None)
TOKEN_RE = re.compile(r"^[0-9a-f]{8,40}$")
KEEP_SECONDS = 15 * 60


def _cleanup(now: float) -> None:
    for t in [t for t, s in _store.items() if now - s["at"] > KEEP_SECONDS]:
        _store.pop(t, None)


def start(token: str) -> None:
    now = time.time()
    with _lock:
        _cleanup(now)
        _store[token] = {"stage": "Начинаю", "done": 0, "total": None, "finished": False,
                         "at": now, "stage_at": now}


def finish(token: str) -> None:
    with _lock:
        s = _store.get(token)
        if s:
            s.update(finished=True, at=time.time())


def stage(text: str, total: int | None = None) -> None:
    """Новый этап: `total` — сколько шагов (None — счёта нет, шкала бежит). Пока закреплён
    внешний этап (`pin`), внутренний — только подпись: шкала считает внешние шаги."""
    t = _current.get()
    if not t:
        return
    now = time.time()
    with _lock:
        s = _store.get(t)
        if not s:
            return
        if s.get("pinned"):
            s.update(detail=text, at=now)
        else:
            s.update(stage=text, total=total, done=0, at=now, stage_at=now, detail="")


def step(n: int = 1) -> None:
    t = _current.get()
    if not t:
        return
    with _lock:
        s = _store.get(t)
        if s and not s.get("pinned"):
            s["done"] += n
            s["at"] = time.time()


def pin(text: str, total: int) -> None:
    """Внешний этап со своим счётом (импорт эталонов: «эталон N из M»): этапы и циклы внутри
    каждого шага шкалу не сбрасывают, а только подписывают, что идёт сейчас."""
    stage(text, total)
    t = _current.get()
    if t:
        with _lock:
            if t in _store:
                _store[t]["pinned"] = True


def advance(n: int = 1) -> None:
    """Шаг закреплённого внешнего этапа."""
    t = _current.get()
    if not t:
        return
    with _lock:
        s = _store.get(t)
        if s:
            s["done"] += n
            s["at"] = time.time()


def unpin() -> None:
    t = _current.get()
    if t:
        with _lock:
            if t in _store:
                _store[t]["pinned"] = False


def each(iterable, text: str, total: int | None = None):
    """Цикл со счётом: `for x in progress.each(rows, "Применяю строки файла"):`."""
    if total is None:
        try:
            total = len(iterable)
        except TypeError:
            total = None
    stage(text, total)
    for x in iterable:
        yield x
        step()


def get(token: str) -> dict | None:
    with _lock:
        s = _store.get(token)
        return None if s is None else {k: s[k] for k in ("stage", "done", "total", "finished")} | {
            "detail": s.get("detail", ""), "stage_seconds": round(time.time() - s["stage_at"], 1)}


def tracked(fn):
    """Обработчик массового действия: номер операции — из адреса (`?_progress=…`), ход виден
    странице, пока он работает. Обработчик синхронный (пул потоков): переменная контекста
    живёт в его потоке, и код глубже видит номер без передачи параметром."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        req = kwargs.get("request")
        token = req.query_params.get("_progress", "") if req is not None else ""
        if not TOKEN_RE.match(token or ""):
            return fn(*args, **kwargs)
        start(token)
        mark = _current.set(token)
        try:
            return fn(*args, **kwargs)
        finally:
            _current.reset(mark)
            finish(token)
    # Подпись — с уже вычисленными типами: в модулях с `from __future__ import annotations` они
    # строки, и FastAPI вычислял бы их в пространстве имён ЭТОГО модуля (UploadFile, Session…).
    import inspect
    import typing
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn, include_extras=True)
    wrapper.__signature__ = sig.replace(
        parameters=[p.replace(annotation=hints.get(p.name, p.annotation)) for p in sig.parameters.values()],
        return_annotation=hints.get("return", sig.return_annotation))
    return wrapper

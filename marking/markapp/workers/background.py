"""Фоновая работа ВНУТРИ программы — без отдельной службы (ТЗ, 12).

Программа стоит на обычном рабочем компьютере с КриптоПро и работает, пока её
запустили. Отдельной службы-воркера нет: те же задания, что у серверного
`scheduler.py`, крутит поток веб-процесса.

- обмен с 1С — каждые 30 с, но сеть трогает только при заданиях, ждущих
  отправки или ответа (`onec.has_work`): обращения к 1С — по факту работы с
  поставкой, а не постоянный опрос;
- Нацкаталог — раз в минуту, в пределах лимита, только если карточки ждут;
- статусы кодов и документов ввода в оборот — раз в минуту, по токену;
- копия базы — не чаще раза в сутки (`backup.MIN_GAP`), первая — через 10 минут
  после запуска: компьютер выключают на ночь, и «раз в сутки по расписанию»
  у него не наступало бы никогда.

Поток один: задания не пересекаются, и SQLite не спорит сам с собой.
"""
import logging
import threading
import time

from markapp.workers import scheduler

logger = logging.getLogger("marking.background")

TICK = 30
NK_EVERY = 60
BACKUP_FIRST_AFTER = 600

_stop = threading.Event()
_thread: threading.Thread | None = None


def _loop() -> None:
    started = time.monotonic()
    last_nk = 0.0
    while not _stop.is_set():
        now = time.monotonic()
        _safe(scheduler.job_onec_exchange)
        if now - last_nk >= NK_EVERY:
            _safe(scheduler.job_nk_fetch)
            _safe(scheduler.job_codes_status)
            last_nk = now
        if now - started >= BACKUP_FIRST_AFTER:
            _safe(scheduler.job_backup)      # сам пропустит, если копия моложе суток
        _stop.wait(TICK)


def _safe(job) -> None:
    """Задание ловит свои ошибки само, но и его `beat` может упасть (база занята,
    диск). Исключение, вышедшее из цикла, убило бы поток насовсем: обмен с 1С,
    статусы кодов и копии встали бы до перезапуска программы."""
    try:
        job()
    except Exception:
        logger.exception("фоновое задание %s упало", getattr(job, "__name__", job))


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="marking-background", daemon=True)
    _thread.start()
    logger.info("фоновая работа запущена")


def stop() -> None:
    _stop.set()

"""Фоновая работа ВНУТРИ программы — без службы (как у «Маркировки»).

Программа стоит на офисном компьютере и работает, пока её запустили ярлыком.
Поток один: задания не пересекаются, и SQLite не спорит сам с собой.
- обмен с 1С — каждые 30 с, но сервер трогает только при заданиях;
- отправка подтверждённых цен — каждые 2 минуты;
- курс ЦБ — при запуске и раз в 6 часов;
- себестоимость и каталоги — раз в сутки (через минуту после запуска: компьютер
  выключают на ночь, и «раз в сутки по расписанию» иначе не наступало бы);
- текущие цены площадок — ПРИ КАЖДОМ запуске (площадка меняет цену сама не чаще
  раза в сутки, но пока программа была выключена, могла поменять) и раз в сутки,
  после них — проверка диапазонов безопасности акций;
- копия базы — через 10 минут после запуска, не чаще раза в сутки.
"""
import logging
import threading
import time

from priceapp.workers import jobs

logger = logging.getLogger("repricer.background")

TICK = 30
DISPATCH_EVERY = 120
RATE_EVERY = 6 * 3600
REFRESH_FIRST_AFTER = 60
REFRESH_EVERY = 3600          # задание само решит, пора ли (раз в сутки)
BACKUP_FIRST_AFTER = 600

_stop = threading.Event()
_thread: threading.Thread | None = None


def _loop() -> None:
    started = time.monotonic()
    last = {"dispatch": 0.0, "rate": -RATE_EVERY, "refresh": None}
    while not _stop.is_set():
        now = time.monotonic()
        jobs.job_onec_exchange()
        if now - last["rate"] >= RATE_EVERY:
            jobs.job_rate()
            last["rate"] = now
        if now - last["dispatch"] >= DISPATCH_EVERY:
            jobs.job_price_dispatch()
            last["dispatch"] = now
        if now - started >= REFRESH_FIRST_AFTER and (
                last["refresh"] is None or now - last["refresh"] >= REFRESH_EVERY):
            jobs.job_daily_refresh(force_prices=last["refresh"] is None)
            last["refresh"] = now
        if now - started >= BACKUP_FIRST_AFTER:
            jobs.job_backup()
        _stop.wait(TICK)


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="repricer-background", daemon=True)
    _thread.start()
    logger.info("фоновая работа запущена")


def stop() -> None:
    _stop.set()

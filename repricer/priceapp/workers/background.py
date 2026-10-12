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
- опрос WB (текущие цены кабинетов + правило эталонной цены) — при запуске и раз в 6 часов,
  а также сразу, если курс ушёл от курса эталонных цен больше чем на 2% (`reference.py`);
- копия базы — через 10 минут после запуска, не чаще раза в сутки.

Потоков ДВА. Быстрый — обмен с 1С, курс, отправка, копия: их нельзя задерживать.
Тяжёлый — суточное обновление (каталоги, цены, диапазоны безопасности) и счётчики
«Внимания»: на боевом каталоге это десятки минут, и в одном потоке с отправкой
подтверждённые цены всё это время не уходили бы. SQLite при этом спорит сам с
собой только за запись: WAL и `busy_timeout`, длинные записи коммитятся порциями.
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
WB_POLL_EVERY = 6 * 3600      # опрос WB: 4 раза в сутки
BACKUP_FIRST_AFTER = 600

HEAVY_TICK = 30
ATTENTION_EVERY = 600         # счётчики «Внимания» — не реже раза в 10 минут, при правках — сразу

_stop = threading.Event()
_thread: threading.Thread | None = None
_heavy: threading.Thread | None = None


def heavy_alive() -> bool:
    return _heavy is not None and _heavy.is_alive()


def _loop() -> None:
    started = time.monotonic()
    last = {"dispatch": 0.0, "rate": -RATE_EVERY}
    while not _stop.is_set():
        now = time.monotonic()
        jobs.job_onec_exchange()
        if now - last["rate"] >= RATE_EVERY:
            jobs.job_rate()
            last["rate"] = now
        if now - last["dispatch"] >= DISPATCH_EVERY:
            jobs.job_price_dispatch()
            last["dispatch"] = now
        if now - started >= BACKUP_FIRST_AFTER:
            jobs.job_backup()
        _stop.wait(TICK)


def _heavy_loop() -> None:
    started = time.monotonic()
    last = {"refresh": None, "attention": None, "wb": None}
    while not _stop.is_set():
        now = time.monotonic()
        if now - started >= REFRESH_FIRST_AFTER and (
                last["refresh"] is None or now - last["refresh"] >= REFRESH_EVERY):
            jobs.job_daily_refresh(force_prices=last["refresh"] is None)
            last["refresh"] = now
        if now - started >= REFRESH_FIRST_AFTER and (
                last["wb"] is None or now - last["wb"] >= WB_POLL_EVERY or jobs.wb_poll_requested()):
            jobs.job_wb_poll()
            last["wb"] = time.monotonic()
        if last["attention"] is None or now - last["attention"] >= ATTENTION_EVERY or jobs.attention_dirty():
            jobs.job_attention()
            last["attention"] = time.monotonic()
        _stop.wait(HEAVY_TICK)


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    global _heavy
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="repricer-background", daemon=True)
    _thread.start()
    _heavy = threading.Thread(target=_heavy_loop, name="repricer-heavy", daemon=True)
    _heavy.start()
    logger.info("фоновая работа запущена")


def stop() -> None:
    _stop.set()

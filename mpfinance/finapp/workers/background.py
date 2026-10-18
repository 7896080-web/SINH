"""Фоновая работа ВНУТРИ службы — без отдельного планировщика (как у «Репрайсера»).

Потоков два, чтобы долгое не держало быстрое:
- быстрый — обмен с 1С каждые 30 с (своя папка, дёшево), суточное задание и
  копия базы через 10 минут после запуска;
- тяжёлый — почта (отчёты Lamoda) раз в 30 минут и загрузка отчётов по API раз
  в 5 минут: одна заявка WB идёт десятки минут (WB отдаёт запрос в минуту), и в
  одном потоке с обменом ответы 1С всё это время лежали бы неразобранными.
SQLite спорит сам с собой только за запись: WAL и `busy_timeout`.
"""
import logging
import threading
import time

from finapp.workers import jobs

logger = logging.getLogger("mpfinance.background")

TICK = 30
DAILY_FIRST_AFTER = 60
DAILY_EVERY = 3600            # задание само решит, пора ли (раз в сутки)
BACKUP_FIRST_AFTER = 600
FETCH_FIRST_AFTER = 240
FETCH_EVERY = 300
MAIL_EVERY = 1800

_stop = threading.Event()
_fast: threading.Thread | None = None
_heavy: threading.Thread | None = None


def dead_threads() -> list[str]:
    """Какие фоновые потоки должны работать, но умерли (пусто, если фон не запускали)."""
    if _fast is None:
        return []
    return [name for name, thread in (("быстрый (1С, копия)", _fast), ("тяжёлый (почта, отчёты по API)", _heavy))
            if thread is None or not thread.is_alive()]


def _safe(fn):
    """Задание не роняет поток НИКОГДА: отметка пишется из `except` и при занятой
    базе падает сама, а исключение вне цикла навсегда остановило бы поток."""
    try:
        fn()
    except Exception:                            # noqa: BLE001
        logger.exception("фоновое задание %s упало", getattr(fn, "__name__", fn))


def _fast_loop() -> None:
    started = time.monotonic()
    last_daily = None
    while not _stop.is_set():
        now = time.monotonic()
        _safe(jobs.job_onec_exchange)
        if now - started >= DAILY_FIRST_AFTER and (last_daily is None or now - last_daily >= DAILY_EVERY):
            _safe(jobs.job_daily)
            last_daily = now
        if now - started >= BACKUP_FIRST_AFTER:
            _safe(jobs.job_backup)
        _stop.wait(TICK)


def _heavy_loop() -> None:
    _stop.wait(FETCH_FIRST_AFTER)
    last_mail = None
    while not _stop.is_set():
        if last_mail is None or time.monotonic() - last_mail >= MAIL_EVERY:
            _safe(jobs.job_mail)
            last_mail = time.monotonic()
        _safe(jobs.job_finance_fetch)
        _stop.wait(FETCH_EVERY)


def start() -> None:
    global _fast, _heavy
    if _fast is not None and _fast.is_alive():
        return
    _stop.clear()
    _fast = threading.Thread(target=_fast_loop, name="mpfinance-fast", daemon=True)
    _heavy = threading.Thread(target=_heavy_loop, name="mpfinance-heavy", daemon=True)
    _fast.start()
    _heavy.start()
    logger.info("фоновая работа запущена")


def stop() -> None:
    _stop.set()

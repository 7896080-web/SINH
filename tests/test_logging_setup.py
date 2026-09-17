"""Настройка лога воркера.

Две вещи, из-за которых лог на боевом сервере было тяжело читать:

1. APScheduler писал по две строки на КАЖДЫЙ цикл задания («Running job» и
   «executed successfully»). При dispatch раз в 45 секунд и отдельном задании на
   каждый кабинет это сотни строк в час: файл дорос до 13 МБ, и собственные
   строки системы в нём тонули.
2. Время в логе местное, а в базе и в интерфейсе — UTC. Разница в три часа один
   раз уже дала ложный вывод «1С обработала задание дважды»: заявка закрыта
   «в 13:00», файл ответа «от 15:59» — одно событие в двух шкалах.
"""
import logging
import re

from app.workers.scheduler import LOG_FORMAT, _log_time_offset, configure_logging


def _formatted(message: str = "проверка", level: int = logging.INFO) -> str:
    formatter = logging.Formatter(LOG_FORMAT.format(offset=_log_time_offset()))
    record = logging.LogRecord("sync_worker", level, __file__, 1, message, None, None)
    return formatter.format(record)


# ------------------------------------------------ смещение видно в каждой строке

def test_offset_looks_like_a_timezone():
    assert re.fullmatch(r"[+-]\d{4}", _log_time_offset())


def test_time_in_the_log_carries_its_offset():
    """Главное: по строке лога должно быть однозначно понятно, в какой шкале
    время — иначе её не с чем сопоставлять ни с базой, ни с файлами 1С."""
    line = _formatted()

    assert re.search(r"\d{2}:\d{2}:\d{2},\d{3}[+-]\d{4} INFO sync_worker: проверка", line)


def test_offset_stands_right_after_the_time():
    """Без пробела — иначе смещение читается как отдельное поле."""
    assert re.search(r"\d{3}[+-]\d{4} ", _formatted())


# ------------------------------------------------ шум планировщика приглушён

def test_apscheduler_info_is_silenced():
    configure_logging()

    assert not logging.getLogger("apscheduler").isEnabledFor(logging.INFO)


def test_apscheduler_problems_are_still_logged():
    """Пропущенный запуск (WARNING) и исключение внутри задания (ERROR) — ровно
    то, ради чего этот лог читают; глушить их нельзя."""
    configure_logging()
    apscheduler = logging.getLogger("apscheduler")

    assert apscheduler.isEnabledFor(logging.WARNING)
    assert apscheduler.isEnabledFor(logging.ERROR)


def test_our_own_lines_are_not_silenced(caplog):
    """Глушим именно apscheduler, а не всё подряд: строки воркера про обмен с 1С
    и про рассылку должны остаться на INFO."""
    configure_logging()

    with caplog.at_level(logging.INFO, logger="sync_worker"):
        logging.getLogger("sync_worker").info("ftp_receive: остатки на дату -> %s", {"files": 1})

    assert any("остатки на дату" in r.getMessage() for r in caplog.records)

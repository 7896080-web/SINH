"""Настройка лога воркера.

Две вещи, из-за которых лог на боевом сервере было тяжело читать:

1. APScheduler писал по две строки на КАЖДЫЙ цикл задания («Running job» и
   «executed successfully»). При dispatch раз в 45 секунд и отдельном задании на
   каждый кабинет это сотни строк в час: файл дорос до 13 МБ, и собственные
   строки системы в нём тонули.
2. Время в логе местное, а в базе и в интерфейсе — UTC. Разница в три часа один
   раз уже дала ложный вывод «1С обработала задание дважды»: заявка закрыта
   «в 13:00», файл ответа «от 15:59» — одно событие в двух шкалах.
3. Кириллица уходила в лог в кодировке локали (cp1251 на русской Windows), и
   инструмент, читающий файл как UTF-8, показывал вместо неё мусор. Строку
   «нет свежего файла выгрузки остатков — пропуск» — по которой видно, что
   сверка не сверяет, — нельзя было ни прочитать, ни найти поиском.
"""
import logging
import re
import sys

from app.workers.scheduler import LOG_FORMAT, _log_time_offset, configure_logging, use_utf8


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


# ------------------------------------------------ остановка службы — не авария

def test_stopping_the_service_logs_one_line(monkeypatch, caplog):
    """NSSM останавливает планировщик через Ctrl+C. Раньше Python печатал в лог
    трассировку KeyboardInterrupt, и штатный рестарт (любой деплой) выглядел там
    как падение."""
    from app.workers import scheduler

    class Stopped:
        def start(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(scheduler, "build_scheduler", lambda: Stopped())

    with caplog.at_level(logging.INFO, logger="sync_worker"):
        scheduler.main()                       # наружу ничего не летит

    assert any("остановлен" in r.getMessage() for r in caplog.records)


def test_a_real_failure_still_surfaces(monkeypatch):
    """Обратная сторона: глушим ТОЛЬКО остановку. Упавший планировщик обязан
    оставить в логе стек, иначе о его смерти никто не узнает."""
    import pytest

    from app.workers import scheduler

    class Broken:
        def start(self):
            raise RuntimeError("база недоступна")

    monkeypatch.setattr(scheduler, "build_scheduler", lambda: Broken())

    with pytest.raises(RuntimeError):
        scheduler.main()


# ------------------------------------------------ кириллица читается как UTF-8

class _Stream:
    """Поток, как его отдаёт Python при перенаправлении stderr в файл."""

    def __init__(self):
        self.encoding = "cp1251"
        self.errors = "strict"

    def reconfigure(self, encoding=None, errors=None):
        self.encoding = encoding
        self.errors = errors


def test_log_stream_is_switched_to_utf8():
    """Кодировка лога не должна зависеть от локали машины: на русской Windows
    Python по умолчанию пишет stderr в cp1251, и кириллица в файле оказывается
    однобайтовой."""
    stream = _Stream()

    assert use_utf8(stream) is True
    assert stream.encoding == "utf-8"


def test_unexpected_character_spoils_a_letter_not_the_line():
    """Запись в лог не имеет права упасть из-за одного неудобного символа —
    например, имени файла, которое файловая система отдала суррогатом."""
    stream = _Stream()

    use_utf8(stream)

    assert stream.errors == "replace"


def test_stream_that_cannot_be_switched_does_not_break_the_worker():
    """Перехват вывода (в тестах, в отладчике) подменяет stderr объектом без
    `reconfigure`. Лог тогда остаётся в прежней кодировке — но воркер работает."""
    class Captured:
        pass

    assert use_utf8(Captured()) is False


def test_stream_refusing_to_switch_does_not_break_the_worker():
    class Locked:
        def reconfigure(self, encoding=None, errors=None):
            raise OSError("поток уже используется")

    assert use_utf8(Locked()) is False


def test_configuring_the_log_switches_the_real_stderr(monkeypatch):
    """Связь настройки с потоком: сама по себе `use_utf8` ничего не решает, если
    её забыть вызвать на том потоке, который NSSM пишет в worker.err.log."""
    stream = _Stream()
    monkeypatch.setattr(sys, "stderr", stream)

    configure_logging()

    assert stream.encoding == "utf-8"

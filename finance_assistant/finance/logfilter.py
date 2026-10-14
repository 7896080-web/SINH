"""Маскировка секретов в журналах: токен бота (он бывает в адресах запросов
к Telegram) и ключ Claude API не должны попасть в журнал даже случайно."""

import logging
import re

SECRET_RE = re.compile(r"(bot)?\d{6,}:[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{8,}")


def mask(text: str) -> str:
    return SECRET_RE.sub("•••", text)


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        masked = mask(message)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask(record.exc_text)
        if masked != message:
            record.msg, record.args = masked, ()
        return True


def install_secret_filter():
    """На все обработчики корневого журнала (их создаёт logging.basicConfig)."""
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretFilter())

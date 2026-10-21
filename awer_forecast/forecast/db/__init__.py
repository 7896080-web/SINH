"""Подключение к БД. Одна точка создания движка — PostgreSQL на проде, SQLite в тестах."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            # Внешние ключи в SQLite по умолчанию выключены — без этого тесты не
            # поймали бы нарушение REFERENCES, которое на PostgreSQL упадёт.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True)

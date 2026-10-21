"""Мелкие операции с БД, которые должны одинаково работать на PostgreSQL и SQLite."""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection


def _insert_for(conn: Connection, table: Table):
    if conn.dialect.name == "postgresql":
        return postgresql.insert(table)
    if conn.dialect.name == "sqlite":
        return sqlite.insert(table)
    raise RuntimeError(f"неподдерживаемая СУБД: {conn.dialect.name}")


def insert_ignore(conn: Connection, table: Table, rows: Sequence[dict], keys: Iterable[str]) -> int:
    """INSERT … ON CONFLICT (keys) DO NOTHING. Возвращает число реально вставленных строк."""
    if not rows:
        return 0
    stmt = _insert_for(conn, table).on_conflict_do_nothing(index_elements=list(keys))
    inserted = 0
    for row in rows:                       # построчно: rowcount по батчу на SQLite недостоверен
        inserted += conn.execute(stmt, row).rowcount or 0
    return inserted


def upsert(conn: Connection, table: Table, rows: Sequence[dict], keys: Sequence[str],
           update: Sequence[str] | None = None) -> None:
    """INSERT … ON CONFLICT (keys) DO UPDATE SET <update> = excluded.<update>."""
    if not rows:
        return
    insert = _insert_for(conn, table)
    columns = update if update is not None else [c for c in rows[0] if c not in keys]
    if not columns:
        stmt = insert.on_conflict_do_nothing(index_elements=list(keys))
    else:
        stmt = insert.on_conflict_do_update(
            index_elements=list(keys), set_={c: insert.excluded[c] for c in columns}
        )
    conn.execute(stmt, list(rows))

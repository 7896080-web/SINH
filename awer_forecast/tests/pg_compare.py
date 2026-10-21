"""Сравнение двух баз PostgreSQL по information_schema — общая часть для
тестов и разовой сверки «schema.sql целиком ↔ миграции Alembic»."""
from __future__ import annotations

from sqlalchemy import create_engine, text

COLUMNS_SQL = """
SELECT table_name, column_name, data_type, character_maximum_length, numeric_precision,
       numeric_scale, is_nullable, column_default
FROM information_schema.columns WHERE table_schema = 'public' AND table_name <> 'alembic_version'
ORDER BY table_name, column_name
"""
CONSTRAINTS_SQL = """
SELECT tc.table_name, tc.constraint_type, tc.constraint_name,
       string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position) AS cols
FROM information_schema.table_constraints tc
LEFT JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
WHERE tc.table_schema = 'public' AND tc.constraint_type IN ('PRIMARY KEY','UNIQUE','FOREIGN KEY','CHECK')
  AND tc.table_name <> 'alembic_version' AND tc.constraint_name NOT LIKE '%_not_null'
GROUP BY tc.table_name, tc.constraint_type, tc.constraint_name
ORDER BY 1, 2, 3
"""
INDEXES_SQL = """
SELECT tablename, indexname, indexdef FROM pg_indexes
WHERE schemaname = 'public' AND tablename <> 'alembic_version' ORDER BY 1, 2
"""
CHECKS_SQL = """
SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint
WHERE contype = 'c' AND connamespace = 'public'::regnamespace ORDER BY 1, 2
"""


def normalize_default(value):
    if value is None:
        return None
    v = value.replace("CURRENT_TIMESTAMP", "now()")
    if v.startswith("nextval("):
        return "nextval"  # имена последовательностей совпадают, но не важны
    return v


def snapshot(url: str) -> dict:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            cols = [tuple(r[:7]) + (normalize_default(r[7]),) for r in conn.execute(text(COLUMNS_SQL))]
            cons = [tuple(r) for r in conn.execute(text(CONSTRAINTS_SQL))]
            idx = [tuple(r) for r in conn.execute(text(INDEXES_SQL))]
            checks = [tuple(r) for r in conn.execute(text(CHECKS_SQL))]
    finally:
        engine.dispose()
    return {"columns": cols, "constraints": cons, "indexes": idx, "checks": checks}


def diff(a: dict, b: dict) -> dict:
    out = {}
    for key in a:
        only_a = sorted(set(a[key]) - set(b[key]))
        only_b = sorted(set(b[key]) - set(a[key]))
        if only_a or only_b:
            out[key] = {"only_first": only_a, "only_second": only_b}
    return out

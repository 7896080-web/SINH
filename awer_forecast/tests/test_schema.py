"""Схема БД: вся docs/schema.sql попадает в базу миграциями, отличия — только объявленные."""
import os
import re
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from forecast.db.schema import metadata
from conftest import alembic_config

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "docs" / "schema.sql"

# Что Фаза 1 добавила к schema.sql (миграции 0002–0003) — любое другое отличие = ошибка.
PHASE1_NEW_TABLES = {"job_runs", "channel_sales_daily"}
PHASE1_NEW_COLUMNS = {
    "order_items": {"lamoda_sku", "external_sku"},
    "fbo_shipments": {"qty_accepted", "warehouse_code", "created_at"},   # created_at — миграция 0003
    "stock_1c": {"taken_at"},                                               # миграция 0003
    "shipment_recommendations": {"batch_id", "confirmed_by", "confirmed_at", "assembly_id"},
    "purchase_recommendations": {"batch_id", "stock_1c_qty_snapshot", "stock_fbo_qty_snapshot",
                                 "in_transit_qty_snapshot", "on_order_qty_snapshot", "daily_demand",
                                 "reorder_point_qty", "explanation", "ordered_qty", "ordered_by", "ordered_at"},
}
CONSTRAINT_WORDS = ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT")


def parse_schema_sql() -> dict[str, dict]:
    """{таблица: {колонка: {"not_null": bool, "pk": bool}}} из CREATE TABLE в schema.sql."""
    sql = re.sub(r"--[^\n]*", "", SCHEMA_SQL.read_text(encoding="utf-8"))
    tables = {}
    for match in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.S):
        name, body = match.groups()
        columns, table_pk = {}, set()
        for line in body.split("\n"):
            line = line.strip().rstrip(",")
            if not line:
                continue
            word = line.split()[0]
            if word in CONSTRAINT_WORDS:
                pk = re.match(r"PRIMARY KEY \(([^)]*)\)", line)
                if pk:
                    table_pk = {c.strip() for c in pk.group(1).split(",")}
                continue
            upper = line.upper()
            columns[word] = {"not_null": "NOT NULL" in upper or "PRIMARY KEY" in upper,
                             "pk": "PRIMARY KEY" in upper}
        for col in table_pk:
            columns[col]["pk"] = True
            columns[col]["not_null"] = True
        tables[name] = columns
    return tables


def test_schema_sql_has_70_tables_all_in_metadata():
    spec = parse_schema_sql()
    assert len(spec) == 70
    assert set(metadata.tables) == set(spec) | PHASE1_NEW_TABLES


def test_columns_keys_and_nullability_match_schema_sql():
    spec = parse_schema_sql()
    for name, columns in spec.items():
        table = metadata.tables[name]
        actual = {c.name for c in table.columns}
        assert actual == set(columns) | PHASE1_NEW_COLUMNS.get(name, set()), name
        for col, props in columns.items():
            column = table.c[col]
            assert column.primary_key == props["pk"], f"{name}.{col}: PK"
            assert (not column.nullable) == props["not_null"], f"{name}.{col}: NOT NULL"


def test_migrations_match_models_on_sqlite(db_url):
    engine = create_engine(db_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), metadata)
        tables = set(inspect(conn).get_table_names())
    engine.dispose()
    assert diff == []
    assert tables == set(metadata.tables) | {"alembic_version"}


def test_migrations_downgrade_and_upgrade_again(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


PG_URL = os.environ.get("FORECAST_TEST_PG_URL")   # postgresql+psycopg://user:pass@host/postgres (с правом CREATE DATABASE)


@pytest.mark.skipif(not PG_URL, reason="FORECAST_TEST_PG_URL не задан — проверка на PostgreSQL пропущена")
def test_migration_0001_equals_raw_schema_sql_on_postgresql():
    """Миграция 0001 даёт ровно ту же базу, что `psql -f docs/schema.sql`: колонки, типы,
    NULL, умолчания, ключи, индексы, CHECK. Затем 0002 накатывается и откатывается."""
    from pg_compare import diff, snapshot

    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    raw_db, mig_db = f"t_raw_{uuid.uuid4().hex[:8]}", f"t_mig_{uuid.uuid4().hex[:8]}"
    base = PG_URL.rsplit("/", 1)[0]
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {raw_db}"))
        conn.execute(text(f"CREATE DATABASE {mig_db}"))
    try:
        raw = create_engine(f"{base}/{raw_db}")
        with raw.begin() as conn:
            # Напрямую драйвером и без параметров: иначе «%» в комментариях schema.sql
            # psycopg принимает за плейсхолдеры.
            conn.connection.driver_connection.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        raw.dispose()
        cfg = alembic_config(f"{base}/{mig_db}")
        command.upgrade(cfg, "0001")
        assert diff(snapshot(f"{base}/{raw_db}"), snapshot(f"{base}/{mig_db}")) == {}
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0001")
        assert diff(snapshot(f"{base}/{raw_db}"), snapshot(f"{base}/{mig_db}")) == {}
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {raw_db} WITH (FORCE)"))
            conn.execute(text(f"DROP DATABASE IF EXISTS {mig_db} WITH (FORCE)"))
        admin.dispose()

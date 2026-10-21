"""Спрос WB/Ozon/сайта из журнала перемещений Sync Admin (ftp_tasks) — только на чтение."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Enum, ForeignKey, Integer, MetaData, String, Table, create_engine, select,
)

from forecast.db.schema import t_channel_sales_daily, t_stock_1c
from forecast.ingest.syncadmin import store_marketplace_demand
from forecast.integrations.onec.files import parse_sales
from forecast.integrations.syncadmin import open_sync_admin, read_marketplace_demand
from forecast.config import PlanningSettings
from forecast.planning.recalc import recalc
from factories import add_size

# Те же имена и типы колонок, что в app/models.py Sync Admin (только используемые нами).
sa_meta = MetaData()
sa_accounts = Table("platform_accounts", sa_meta,
                    Column("id", Integer, primary_key=True),
                    Column("platform", Enum("wb", "ozon", "kit", name="platform"), nullable=False),
                    Column("name", String(128), nullable=False))
sa_tasks = Table("ftp_tasks", sa_meta,
                 Column("id", Integer, primary_key=True),
                 Column("command", String(32), nullable=False),
                 Column("barcode", String(64)),
                 Column("warehouse_from", String(64)),
                 Column("warehouse_to", String(64)),
                 Column("quantity", Integer),
                 Column("order_id", String(128), nullable=False),
                 Column("account_id", Integer, ForeignKey("platform_accounts.id"), nullable=False),
                 Column("movement_date", Date),
                 Column("status", Enum("pending", "sent", "done", "failed", "timeout", name="ftptaskstatus")),
                 Column("created_at", DateTime),
                 Column("result_status", String(16)),
                 Column("is_test", Boolean, nullable=False))

TODAY = date(2026, 10, 21)
NOON = datetime(2026, 10, 20, 9, 0)            # 12:00 по Москве


def make_sync_admin_db(path):
    eng = create_engine(f"sqlite:///{path}")
    sa_meta.create_all(eng)
    with eng.begin() as conn:
        conn.execute(sa_accounts.insert(), [{"id": 1, "platform": "wb", "name": "WB ИП"},
                                            {"id": 2, "platform": "ozon", "name": "Ozon ИП"},
                                            {"id": 3, "platform": "kit", "name": "Сайт"},
                                            {"id": 4, "platform": "wb", "name": "WB второй кабинет"}])
    eng.dispose()
    return path


@pytest.fixture
def sync_admin_db(tmp_path):
    return make_sync_admin_db(tmp_path / "sync_admin.db")


def add_task(path, command, order_id, account_id, *, barcode="4600000000011", qty=1, created_at=NOON,
             movement_date=None, status="done", is_test=False):
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as conn:
        conn.execute(sa_tasks.insert().values(command=command, barcode=barcode, quantity=qty, order_id=order_id,
                                              account_id=account_id, movement_date=movement_date, status=status,
                                              created_at=created_at, is_test=is_test))
    eng.dispose()


def demand(path, since=TODAY - timedelta(days=30)):
    eng = open_sync_admin(str(path))
    try:
        return read_marketplace_demand(eng, since)
    finally:
        eng.dispose()


def test_orders_net_of_cancellations_by_channel(sync_admin_db):
    add_task(sync_admin_db, "CREATE_MOVEMENT", "W1", 1, qty=2)
    add_task(sync_admin_db, "CREATE_MOVEMENT", "W2", 4, qty=1)                  # второй кабинет WB — тот же канал
    add_task(sync_admin_db, "CREATE_MOVEMENT", "O1", 2, qty=3)
    add_task(sync_admin_db, "CANCEL_MOVEMENT", "O1", 2, qty=1)                  # частичный возврат
    add_task(sync_admin_db, "CREATE_MOVEMENT", "O2", 2, qty=1)
    add_task(sync_admin_db, "CANCEL_MOVEMENT", "O2", 2, qty=1)                  # отменён целиком
    add_task(sync_admin_db, "CREATE_MOVEMENT", "K1", 3, qty=1)                  # сайт (Яндекс KIT)
    add_task(sync_admin_db, "CONFIRM_MOVEMENT", "W1", 1, qty=2)                 # подтверждение — не новый спрос
    d = demand(sync_admin_db)
    day = date(2026, 10, 20)
    assert d.totals == {("wb", "4600000000011", day): 3, ("ozon", "4600000000011", day): 2,
                        ("own_site", "4600000000011", day): 1}
    assert (d.orders, d.cancelled, d.partially_returned) == (5, 1, 1)


def test_test_orders_are_never_demand(sync_admin_db):
    add_task(sync_admin_db, "CREATE_MOVEMENT", "T1", 1, qty=5, is_test=True)
    assert demand(sync_admin_db).totals == {}


def test_day_is_order_day_in_moscow_or_backdated_movement_date(sync_admin_db):
    add_task(sync_admin_db, "CREATE_MOVEMENT", "LATE", 1, created_at=datetime(2026, 10, 19, 22, 30))  # 01:30 МСК 20-го
    add_task(sync_admin_db, "CREATE_MOVEMENT", "BACK", 1, created_at=NOON, movement_date=date(2026, 10, 1))
    days = {day for (_c, _b, day) in demand(sync_admin_db).totals}
    assert days == {date(2026, 10, 20), date(2026, 10, 1)}


def test_failed_1c_document_still_counts_as_demand_and_resend_is_not_doubled(sync_admin_db):
    add_task(sync_admin_db, "CREATE_MOVEMENT", "W1", 1, qty=1, status="failed")
    add_task(sync_admin_db, "CREATE_MOVEMENT", "W1", 1, qty=1, status="done")   # повторная отправка
    assert sum(demand(sync_admin_db).totals.values()) == 1


def test_window_and_missing_barcode(sync_admin_db):
    add_task(sync_admin_db, "CREATE_MOVEMENT", "OLD", 1, created_at=datetime(2026, 8, 1, 9, 0))
    add_task(sync_admin_db, "CREATE_MOVEMENT", "NOBC", 1, barcode=None)
    d = demand(sync_admin_db)
    assert d.totals == {} and any("без баркода" in s for s in d.skipped)


def test_sync_admin_database_is_opened_read_only(sync_admin_db):
    eng = open_sync_admin(str(sync_admin_db))
    with pytest.raises(Exception) as exc:
        with eng.begin() as conn:
            conn.exec_driver_sql("DELETE FROM ftp_tasks")
    eng.dispose()
    assert "readonly database" in str(exc.value)


def test_missing_database_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="Sync Admin"):
        open_sync_admin(str(tmp_path / "nope.db"))


def test_store_replaces_window_and_feeds_purchase(engine, sync_admin_db):
    add_task(sync_admin_db, "CREATE_MOVEMENT", "W1", 1, qty=2)
    since = TODAY - timedelta(days=30)
    with engine.begin() as conn:
        psid = add_size(conn, "AW-1", "", "M", ["4600000000011"])
        conn.execute(t_stock_1c.insert().values(product_size_id=psid, snapshot_date=TODAY, qty=0))
        store_marketplace_demand(conn, demand(sync_admin_db, since), since)
        store_marketplace_demand(conn, demand(sync_admin_db, since), since)        # повтор не задваивает
        rows = conn.execute(select(t_channel_sales_daily)).all()
        assert [(r.channel, float(r.qty), r.source) for r in rows] == [("wb", 2.0, "sync_admin")]
        report = recalc(conn, PlanningSettings(min_history_days=1), TODAY, 1, datetime(2026, 10, 21, 6))
    assert report["forecasts"] == 1          # спрос WB из журнала дошёл до прогноза закупки


def test_sales_files_reject_channels_with_their_own_source():
    parsed = parse_sales("20261010|111|1|100|wb\n20261010|111|1|100|own_site\n20261010|111|1|100|retail_2\n")
    assert [r["channel"] for r in parsed.rows] == ["retail_2"]
    assert all("Sync Admin" in e for e in parsed.errors)

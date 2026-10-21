"""Ежедневный цикл целиком: Lamoda (имитация) + файлы 1С → рекомендации; курсор заказов; сбои."""
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from forecast.config import PlanningSettings, Settings
from forecast.db.schema import t_job_runs, t_order_items, t_shipment_recommendations, t_stock_1c
from forecast.integrations.lamoda.client import format_datetime, parse_datetime
from forecast.jobs import JobAlreadyRunning, run_daily, run_step, start_job
from forecast.timeutil import today_msk, utcnow
from factories import FakeLamoda, add_size, item, order
from test_syncadmin import add_task, make_sync_admin_db

NO_SLEEP = lambda seconds: None  # noqa: E731 — повторы без реальных пауз


def settings_for(db_url, exchange_dir, seller="777", sync_admin="") -> Settings:
    return Settings(database_url=db_url, session_secret="x", login="m", password_hash="", lamoda_base_url="http://l",
                    lamoda_client_id="id", lamoda_client_secret="secret", lamoda_seller_id=seller,
                    lamoda_country="RU", orders_backfill_days=30, orders_overlap_hours=6,
                    onec_exchange_dirs=(exchange_dir,), onec_sales_dir=None, planning=PlanningSettings(),
                    sync_admin_database=sync_admin)


@pytest.fixture
def setup(engine, db_url, tmp_path):
    with engine.begin() as conn:
        add_size(conn, "AW-1", "", "M", ["4600000000011"], lamoda_sku="MPAW1INM")
    exchange = tmp_path / "archive"
    exchange.mkdir()
    (exchange / f"stock_{today_msk():%Y%m%d}070000.txt").write_text(
        "u1|AW-1|Куртка|9|4600000000011|M|\n", encoding="utf-8")
    fake = FakeLamoda()
    now = utcnow()
    for i in range(20):
        moment = format_datetime(now - timedelta(days=20 - i, hours=2))
        fake.orders.append(order(f"O{i}", moment, [item(f"I{i}", "MPAW1INM", "DELIVERED")]))
    fake.stocks = [{"sku": "MPAW1INM", "warehouseCode": "BYKOVO", "quantity": 1}]
    fake.nomenclatures = [{"sku": "MPAW1INM", "parentSku": "MPAW1", "externalSku": "x", "barcode": "4600000000011"}]
    sync_admin = make_sync_admin_db(tmp_path / "sync_admin.db")
    add_task(sync_admin, "CREATE_MOVEMENT", "W1", 1, created_at=utcnow() - timedelta(days=2))
    return settings_for(db_url, exchange, sync_admin=str(sync_admin)), fake


def test_daily_cycle_end_to_end_and_idempotent(engine, setup):
    settings, fake = setup
    results = run_daily(engine, settings, http=fake.client(), sleep=NO_SLEEP)
    assert [r.status for r in results] == ["ok"] * 8, [(r.job, r.message) for r in results]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t_order_items)).scalar() == 20
        assert conn.execute(select(t_stock_1c.c.qty)).scalar() == 9
        rec = conn.execute(select(t_shipment_recommendations)).one()
        cursor = conn.execute(select(t_job_runs.c.cursor).where(t_job_runs.c.job == "lamoda_orders")).scalar()
    assert float(rec.qty_to_ship) > 0
    assert parse_datetime(cursor) is not None

    fake.calls.clear()
    run_daily(engine, settings, http=fake.client(), sleep=NO_SLEEP)
    order_query = [c[2] for c in fake.calls if c[1] == "/v2/orders"][0]
    # второй запуск — от курсора первого минус перекрытие, а не вся история заново
    expected_from = (parse_datetime(cursor) - timedelta(hours=6)).replace(microsecond=0)
    assert parse_datetime(order_query["updatedAtFrom"]) == expected_from
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t_order_items)).scalar() == 20


def test_lamoda_failure_is_reported_and_other_steps_still_run(engine, db_url, setup):
    settings, fake = setup
    bad = settings_for(db_url, settings.onec_exchange_dirs[0], seller="wrong")
    results = {r.job: r for r in run_daily(engine, bad, http=fake.client(), sleep=NO_SLEEP)}
    assert results["lamoda_orders"].status == "error" and "sellerId" in results["lamoda_orders"].message
    assert results["onec_stock"].status == "ok" and results["recalc"].status == "ok"
    with engine.connect() as conn:
        statuses = dict(conn.execute(select(t_job_runs.c.job, t_job_runs.c.status)).all())
    assert statuses["lamoda_stocks"] == "error" and statuses["recalc"] == "ok"


def test_failed_orders_poll_does_not_move_cursor(engine, setup):
    settings, fake = setup
    run_step(engine, settings, "lamoda_catalog", http=fake.client(), sleep=NO_SLEEP)
    first = run_step(engine, settings, "lamoda_orders", http=fake.client(), sleep=NO_SLEEP)
    fake.fail_next = [httpx.Response(201, json={"access_token": "z", "expires_in": 900})] + [
        httpx.Response(503, json={"error": {"code": "SERVICE_UNAVAILABLE", "message": "down"}})] * 10
    failed = run_step(engine, settings, "lamoda_orders", http=fake.client(), sleep=NO_SLEEP)
    assert first.status == "ok" and failed.status == "error"
    with engine.connect() as conn:
        cursors = [c for (c,) in conn.execute(select(t_job_runs.c.cursor).where(t_job_runs.c.job == "lamoda_orders"))]
    assert cursors[1] is None and cursors[0] is not None


def test_same_job_cannot_run_twice_at_once(engine):
    start_job(engine, "recalc", "a")
    with pytest.raises(JobAlreadyRunning):
        start_job(engine, "recalc", "b")


def test_missing_1c_file_is_a_clear_error(engine, db_url, tmp_path):
    settings = settings_for(db_url, tmp_path / "nothing")
    result = run_step(engine, settings, "onec_stock")
    assert result.status == "error" and "stock_*.txt" in result.message

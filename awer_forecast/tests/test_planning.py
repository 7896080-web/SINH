"""Расчёт: цензурирование, сглаживание, страховой запас, рекомендации двух страниц."""
import math
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from forecast.config import PlanningSettings
from forecast.db.schema import (
    t_central_stock_aggregate_demand, t_channel_sales_daily, t_demand_forecast, t_demand_timeseries,
    t_fbo_shipments, t_order_items, t_purchase_recommendations, t_reorder_points, t_shipment_recommendations,
    t_stock_1c, t_stock_fbo,
)
from forecast.planning import smoothing
from forecast.planning.policy import order_up_to, safety_stock
from forecast.planning.recalc import recalc
from forecast.planning.series import lamoda_series
from factories import add_size

AS_OF = date(2026, 10, 21)
SETTINGS = PlanningSettings(service_level_z=1.65, review_interval_days=7, fbo_lead_days_default=7,
                            supplier_lead_days_default=60, history_days=90, min_history_days=14)


# ---------------- сглаживание и формулы ----------------

def test_constant_demand_gives_level_and_zero_error():
    f = smoothing.fit([2.0] * 30)
    assert f.method == "smoothing" and f.level == pytest.approx(2.0) and f.sigma == pytest.approx(0.0)


def test_level_follows_recent_shift():
    f = smoothing.fit([1.0] * 40 + [3.0] * 20)
    assert 2.5 < f.level <= 3.0


def test_wholesale_spike_is_capped_before_smoothing():
    values = [1.0, 2.0] * 30
    spiked = values[:-10] + [40.0] + values[-9:]
    base = smoothing.fit(values)
    raw = smoothing.fit(spiked, outlier_sigma=0)           # без среза
    capped = smoothing.fit(spiked)
    assert capped.outliers == 1 and raw.outliers == 0
    # срезанный до порога опт почти не сдвигает уровень и не раздувает σ, сырой — да
    assert capped.level - base.level < 0.25 < raw.level - base.level
    assert capped.sigma < raw.sigma / 3


@pytest.mark.parametrize("values", [
    ([0.0] * 9 + [1.0]) * 12,          # 1 шт. раз в 10 дней — крайний размер
    ([0.0] * 4 + [2.0]) * 12,
    [1.0] * 20 + [3.0] + [1.0] * 10,   # случайные 3 шт. при «обычно по одной» — не опт
])
def test_sparse_and_ordinary_sales_are_not_outliers(values):
    f = smoothing.fit(values)
    assert f.outliers == 0
    assert f.level > 0.05


def test_short_history_uses_mean_with_poisson_floor():
    f = smoothing.fit([1.0, 1.0, 1.0], min_history=14)
    assert f.method == "mean" and f.level == 1.0 and f.sigma == pytest.approx(1.0)
    assert smoothing.fit([]).method == "none"


def test_fixed_alpha_is_respected():
    assert smoothing.fit([1.0, 2.0] * 20, alpha=0.3).alpha == 0.3


def test_safety_stock_and_reorder_point_basic_formula():
    assert safety_stock(1.65, 2.0, 16) == pytest.approx(1.65 * 2.0 * 4)
    p = order_up_to(daily_demand=0.5, sigma=1.0, lead_days=14, z=1.65, position=3)
    assert p.reorder_point == pytest.approx(0.5 * 14 + 1.65 * math.sqrt(14))
    assert p.need == math.ceil(p.reorder_point) - 3
    assert order_up_to(0.0, 0.0, 14, 1.65, 0).need == 0
    assert order_up_to(0.05, 0.0, 14, 1.65, 0).need == 1        # любой спрос при пустой позиции → 1 шт
    assert order_up_to(0.5, 1.0, 14, 1.65, 50).need == 0


# ---------------- ряд и цензурирование ----------------

def _seed_sales(conn, psid, days: dict[int, int], start=AS_OF - timedelta(days=60)):
    """days: {смещение от start: продано}; пишет order_items в статусе DELIVERED."""
    n = 0
    for offset, qty in days.items():
        for _ in range(qty):
            n += 1
            conn.execute(t_order_items.insert().values(
                order_id=f"O{psid}-{offset}-{n}", lamoda_item_id=str(n), product_size_id=psid, status="DELIVERED",
                price_actual=Decimal("1000"), event_date=start + timedelta(days=offset)))


def test_stockout_days_are_excluded_not_zero(engine):
    from forecast.ingest.lamoda import rebuild_sales_daily
    with engine.begin() as conn:
        psid = add_size(conn, "A", "", "M", ["1"], lamoda_sku="L1")
        start = AS_OF - timedelta(days=30)
        _seed_sales(conn, psid, {i: 1 for i in range(0, 20)}, start=start)
        for i in range(30):
            qty = 0 if i >= 20 else 5                           # последние 10 дней — пусто на FBO
            conn.execute(t_stock_fbo.insert().values(product_size_id=psid, warehouse_code="BYKOVO",
                                                     snapshot_date=start + timedelta(days=i), qty=qty))
        rebuild_sales_daily(conn)
        series = lamoda_series(conn, AS_OF, 90)[psid]
    assert series.stockout_days == 10
    assert series.training == [1.0] * 20
    assert smoothing.fit(series.training).level == pytest.approx(1.0)


def test_series_starts_at_first_presence(engine):
    from forecast.ingest.lamoda import rebuild_sales_daily
    with engine.begin() as conn:
        psid = add_size(conn, "A", "", "M", ["1"], lamoda_sku="L1")
        _seed_sales(conn, psid, {50: 1}, start=AS_OF - timedelta(days=60))
        rebuild_sales_daily(conn)
        series = lamoda_series(conn, AS_OF, 90)[psid]
    assert series.start == AS_OF - timedelta(days=10) and len(series.values) == 10


# ---------------- пересчёт целиком ----------------

def _world(conn, *, cs=10, fbo=1, supplier=None, available=True):
    psid = add_size(conn, "A100", "чёрный", "50", ["4600000000011"], lamoda_sku="MPA100IN50",
                    supplier=supplier, available=available)
    _seed_sales(conn, psid, {i: 1 for i in range(60)})          # 1 шт/день
    conn.execute(t_stock_1c.insert().values(product_size_id=psid, snapshot_date=AS_OF, qty=cs))
    conn.execute(t_stock_fbo.insert().values(product_size_id=psid, warehouse_code="BYKOVO",
                                             snapshot_date=AS_OF, qty=fbo))
    return psid


def run(conn, batch_id=1):
    return recalc(conn, SETTINGS, AS_OF, batch_id, datetime(2026, 10, 21, 6, 0) + timedelta(minutes=batch_id))


def latest(conn, table, **filters):
    q = select(table).where(*[table.c[k] == v for k, v in filters.items()])
    return conn.execute(q).all()


def test_fbo_recommendation_tops_up_to_reorder_point(engine):
    with engine.begin() as conn:
        psid = _world(conn, cs=100, fbo=1)
        report = run(conn)
        rec = latest(conn, t_shipment_recommendations, status="pending")[0]
        rp = latest(conn, t_reorder_points)[0]
        fc = latest(conn, t_demand_forecast)[0]
    # спрос 1/день, σ = 0, L = 7 + 7 = 14 → точка заказа 14, есть 1 → отгрузить 13
    assert float(rp.reorder_point_qty) == pytest.approx(14.0) and float(rp.lead_time_used_days) == 14
    assert rec.product_size_id == psid and float(rec.qty_to_ship) == 13
    assert float(rec.stock_1c_qty_snapshot) == 100 and float(rec.stock_fbo_qty_snapshot) == 1
    assert rec.confirmed_qty is None and rec.confidence_pct is None
    assert "точка заказа" in rec.confidence_explanation
    assert fc.channel == "lamoda" and float(fc.forecast_qty) == pytest.approx(28.0)
    assert report["shipment"]["to_ship"] == 1


def test_fbo_recommendation_capped_by_central_stock_and_counts_in_transit(engine):
    with engine.begin() as conn:
        psid = _world(conn, cs=5, fbo=1)
        conn.execute(t_fbo_shipments.insert().values(shipment_id="SH1", product_size_id=psid, qty=4, qty_accepted=1,
                                                     created_date=AS_OF - timedelta(days=2), status="CONFIRMED"))
        conn.execute(t_fbo_shipments.insert().values(shipment_id="SH0", product_size_id=psid, qty=50,
                                                     created_date=AS_OF - timedelta(days=9), status="CLOSED"))
        run(conn)
        rec = latest(conn, t_shipment_recommendations, status="pending")[0]
    # нужно 14 − (1 + 3 в пути) = 10, но на ЦС только 5
    assert float(rec.qty_to_ship) == 5 and "в пути 3" in rec.confidence_explanation


def test_sizes_without_central_stock_are_not_on_fbo_page(engine):
    with engine.begin() as conn:
        _world(conn, cs=0, fbo=0)
        run(conn)
        assert latest(conn, t_shipment_recommendations) == []
        assert len(latest(conn, t_reorder_points)) == 1          # точка заказа считается всё равно


def test_confirmed_quantity_survives_recalculation(engine):
    with engine.begin() as conn:
        psid = _world(conn)
        run(conn, batch_id=1)
        conn.execute(update(t_shipment_recommendations).where(t_shipment_recommendations.c.batch_id == 1)
                     .values(confirmed_qty=7, confirmed_by="manager", status="confirmed"))
        run(conn, batch_id=2)
        new = latest(conn, t_shipment_recommendations, batch_id=2)[0]
        old = latest(conn, t_shipment_recommendations, batch_id=1)[0]
    assert new.status == "confirmed" and float(new.confirmed_qty) == 7 and new.confirmed_by == "manager"
    assert old.status == "superseded"


def test_purchase_uses_all_channels_and_supplier_lead_time(engine):
    supplier = {"name": "Фабрика", "production_days": 30, "delivery_days": 10, "moq": 1}
    with engine.begin() as conn:
        psid = _world(conn, cs=3, fbo=1, supplier=supplier)
        for i in range(60):                                       # розница из 1С: 0.5/день
            if i % 2 == 0:
                conn.execute(t_channel_sales_daily.insert().values(
                    channel="retail_1", barcode="4600000000011", sale_date=AS_OF - timedelta(days=60 - i), qty=1))
        report = run(conn)
        rec = latest(conn, t_purchase_recommendations, status="pending")[0]
        agg = latest(conn, t_central_stock_aggregate_demand)[0]
    assert set(agg.channel_breakdown) == {"lamoda", "retail_1"}
    assert 1.4 < float(rec.daily_demand) < 1.6
    assert rec.ready_by_date == AS_OF + timedelta(days=40)
    assert float(rec.qty_to_purchase) == math.ceil(float(rec.reorder_point_qty)) - 4
    assert "розница 1" in rec.explanation and report["purchase"]["rows"] == 1


def test_not_available_to_reorder_gives_warning_not_purchase(engine):
    with engine.begin() as conn:
        _world(conn, cs=0, fbo=0, available=False)
        run(conn)
        rows = latest(conn, t_purchase_recommendations)
    assert len(rows) == 1 and rows[0].status == "no_reorder" and float(rows[0].qty_to_purchase) == 0
    assert "идёт на исчерпание" in rows[0].explanation


def test_ordered_purchase_counts_as_on_order(engine):
    with engine.begin() as conn:
        psid = _world(conn, cs=0, fbo=0)
        run(conn, batch_id=1)
        first = latest(conn, t_purchase_recommendations, status="pending")[0]
        conn.execute(update(t_purchase_recommendations).where(
            t_purchase_recommendations.c.recommendation_id == first.recommendation_id)
            .values(status="ordered", ordered_qty=first.qty_to_purchase))
        run(conn, batch_id=2)
        pending = latest(conn, t_purchase_recommendations, status="pending")
    assert pending == []                                           # заказанное закрывает потребность


def test_demand_timeseries_written_with_flags(engine):
    with engine.begin() as conn:
        _world(conn)
        run(conn)
        rows = latest(conn, t_demand_timeseries)
    assert len(rows) == 60 and not any(r.is_stockout_corrected for r in rows)


def test_discontinued_models_get_no_replenishment(engine):
    with engine.begin() as conn:
        psid = add_size(conn, "OLD", "", "M", ["9"], lamoda_sku="LOLD", status="discontinued")
        _seed_sales(conn, psid, {i: 1 for i in range(60)})
        conn.execute(t_stock_1c.insert().values(product_size_id=psid, snapshot_date=AS_OF, qty=10))
        run(conn)
        rec = latest(conn, t_shipment_recommendations)[0]
        assert float(rec.qty_to_ship) == 0 and "снят" in rec.confidence_explanation
        assert latest(conn, t_purchase_recommendations) == []

"""Приём данных: заказы Lamoda (идемпотентность), остатки и поставки FBO, файлы 1С."""
import os
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

from forecast.catalog.resolver import SkuResolver
from forecast.db.schema import (
    t_channel_sales_daily, t_fbo_shipments, t_order_items, t_sales_daily, t_stock_1c, t_stock_fbo,
)
from forecast.ingest.lamoda import (
    ingest_fbo_shipments, ingest_fbo_stocks, ingest_orders, rebuild_sales_daily, remap_unmapped_order_items,
)
from forecast.ingest.onec import ingest_sales_files, ingest_stock_file
from forecast.integrations.lamoda.client import LamodaClient
from forecast.integrations.onec.files import find_newest_stock_file, parse_sales, parse_stock_rows
from factories import FakeLamoda, add_size, item, order

FROM = datetime(2026, 10, 1, tzinfo=timezone.utc)
TO = datetime(2026, 10, 21, tzinfo=timezone.utc)


def client_for(fake):
    return LamodaClient("id", "secret", fake.seller_id, http=fake.client(), sleep=lambda s: None)


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar()


def setup_sizes(engine):
    with engine.begin() as conn:
        m = add_size(conn, "A100", "чёрный", "M", ["4600000000011"], lamoda_sku="MPA100INM")
        l_ = add_size(conn, "A100", "чёрный", "L", ["4600000000028", "2000000000028"], lamoda_sku="MPA100INL")
    return m, l_


# ---------------- заказы ----------------

def test_orders_ingest_is_idempotent_and_converts_kopecks(engine):
    m, _ = setup_sizes(engine)
    fake = FakeLamoda()
    fake.orders = [order("CZ1", "2026-10-05T10:00:00.000Z", [item("i1", "MPA100INM", "DELIVERED", 1999.0)])]
    with engine.connect() as conn:
        resolver = SkuResolver.load(conn)
    first = ingest_orders(engine, client_for(fake), resolver, FROM, TO)
    second = ingest_orders(engine, client_for(fake), resolver, FROM, TO)
    assert first["items_inserted"] == 1 and second["items_inserted"] == 0
    assert count(engine, t_order_items) == 1
    with engine.connect() as conn:
        row = conn.execute(select(t_order_items)).one()
    assert row.order_id == "CZ1" and row.tracking_id == "RUCZ1" and row.lamoda_item_id == "i1"
    assert row.product_size_id == m and row.price_actual == Decimal("1999.00")
    assert row.price_base == Decimal("2398.80") and row.event_date == date(2026, 10, 5)


def test_new_status_of_same_item_is_a_new_row(engine):
    setup_sizes(engine)
    fake = FakeLamoda()
    fake.orders = [order("CZ1", "2026-10-05T10:00:00.000Z", [item("i1", "MPA100INM", "IN_DELIVERY")])]
    with engine.connect() as conn:
        resolver = SkuResolver.load(conn)
    ingest_orders(engine, client_for(fake), resolver, FROM, TO)
    fake.orders = [order("CZ1", "2026-10-07T10:00:00.000Z", [item("i1", "MPA100INM", "DELIVERED")])]
    ingest_orders(engine, client_for(fake), resolver, FROM, TO)
    ingest_orders(engine, client_for(fake), resolver, FROM, TO)
    with engine.connect() as conn:
        statuses = [r.status for r in conn.execute(select(t_order_items).order_by(t_order_items.c.order_item_id))]
    assert statuses == ["IN_DELIVERY", "DELIVERED"]


def test_unknown_status_is_stored_but_does_not_break(engine):
    setup_sizes(engine)
    fake = FakeLamoda()
    fake.orders = [order("CZ1", "2026-10-05T10:00:00.000Z", [item("i1", "MPA100INM", "TELEPORTED")])]
    with engine.connect() as conn:
        resolver = SkuResolver.load(conn)
    report = ingest_orders(engine, client_for(fake), resolver, FROM, TO)
    assert report["unknown_statuses"] == ["TELEPORTED"] and report["items_inserted"] == 1


def test_unmapped_items_are_remapped_after_catalog_update(engine):
    fake = FakeLamoda()
    fake.orders = [order("CZ1", "2026-10-05T10:00:00.000Z",
                         [item("i1", "MPNEWINM", "DELIVERED", external_sku="2000000000999")])]
    with engine.connect() as conn:
        resolver = SkuResolver.load(conn)
    report = ingest_orders(engine, client_for(fake), resolver, FROM, TO)
    assert report["unmapped_items"] == 1
    with engine.begin() as conn:
        psid = add_size(conn, "N1", "", "M", ["2000000000999"])        # SKU продавца = баркод
        assert remap_unmapped_order_items(conn, SkuResolver.load(conn)) == 1
        assert conn.execute(select(t_order_items.c.product_size_id)).scalar() == psid


def test_sales_daily_counts_returns_once_and_only_for_seen_sales(engine):
    m, _ = setup_sizes(engine)
    fake = FakeLamoda()
    fake.orders = [
        order("A", "2026-10-03T10:00:00.000Z", [item("a1", "MPA100INM", "DELIVERED", 1000),
                                                item("a2", "MPA100INM", "DELIVERED", 1000)]),
        order("B", "2026-10-04T10:00:00.000Z", [item("b1", "MPA100INM", "REFUNDED", 1000)]),   # выкупа не видели
    ]
    with engine.connect() as conn:
        resolver = SkuResolver.load(conn)
    ingest_orders(engine, client_for(fake), resolver, FROM, TO)
    # позиция a1 возвращается: сначала REFUND_NECESSARY, затем REFUNDED — возврат считается один раз
    fake.orders = [order("A", "2026-10-09T10:00:00.000Z", [item("a1", "MPA100INM", "REFUND_NECESSARY", 1000),
                                                           item("a2", "MPA100INM", "DELIVERED", 1000)])]
    ingest_orders(engine, client_for(fake), resolver, FROM, TO)
    fake.orders = [order("A", "2026-10-12T10:00:00.000Z", [item("a1", "MPA100INM", "REFUNDED", 1000),
                                                           item("a2", "MPA100INM", "DELIVERED", 1000)])]
    ingest_orders(engine, client_for(fake), resolver, FROM, TO)
    with engine.begin() as conn:
        rebuild_sales_daily(conn)
        rows = {r.sale_date: r for r in conn.execute(select(t_sales_daily))}
    assert rows[date(2026, 10, 3)].qty_delivered == 2
    assert rows[date(2026, 10, 9)].qty_returned == 1
    assert date(2026, 10, 12) not in rows and date(2026, 10, 4) not in rows
    assert rows[date(2026, 10, 9)].revenue == Decimal("-1000.00")


# ---------------- FBO ----------------

def test_fbo_stocks_snapshot_sums_warehouses_and_keeps_zeros(engine):
    m, l_ = setup_sizes(engine)
    stocks = [{"sku": "MPA100INM", "warehouseCode": "BYKOVO", "quantity": 3},
              {"sku": "MPA100INM", "warehouseCode": "SOFINO", "quantity": 2},
              {"sku": "MPA100INL", "warehouseCode": "BYKOVO", "quantity": 0},
              {"sku": "ALIEN", "warehouseCode": "BYKOVO", "quantity": 9}]
    with engine.begin() as conn:
        report = ingest_fbo_stocks(conn, stocks, SkuResolver.load(conn), date(2026, 10, 21))
        again = ingest_fbo_stocks(conn, stocks, SkuResolver.load(conn), date(2026, 10, 21))
        rows = {(r.product_size_id, r.warehouse_code): r.qty for r in conn.execute(select(t_stock_fbo))}
    assert report["unmatched_total"] == 1 and again["matched"] == 3
    assert rows == {(m, "BYKOVO"): 3, (m, "SOFINO"): 2, (l_, "BYKOVO"): 0}


def test_fbo_shipments_in_transit_and_arrival(engine):
    m, l_ = setup_sizes(engine)
    fake = FakeLamoda()
    fake.shipments = [{"id": "SH1", "externalShipmentId": "x", "status": "CONFIRMED", "plannedDate": "2026-10-25",
                       "createdAt": "2026-10-18T09:00:00Z", "warehouseCode": "BYKOVO", "statistics": {}}]
    fake.shipment_items["SH1"] = [
        {"id": "1", "sku": "MPA100INM", "ean": None, "quantityPlanned": 5, "quantityAccepted": 0},
        {"id": "2", "sku": "unknown", "ean": "2000000000028", "quantityPlanned": 2, "quantityAccepted": 0},
    ]
    client = client_for(fake)
    with engine.begin() as conn:
        report = ingest_fbo_shipments(conn, client, SkuResolver.load(conn), date(2026, 10, 21))
        rows = {r.product_size_id: r for r in conn.execute(select(t_fbo_shipments))}
    assert report["in_transit"] == 1 and rows[m].qty == 5 and rows[l_].qty == 2   # второй — по EAN из пула
    assert rows[m].expected_arrival_date == date(2026, 10, 25) and rows[m].created_date == date(2026, 10, 18)
    fake.shipments[0]["status"] = "RECEIVED"
    with engine.begin() as conn:
        report = ingest_fbo_shipments(conn, client, SkuResolver.load(conn), date(2026, 10, 26))
        row = conn.execute(select(t_fbo_shipments).where(t_fbo_shipments.c.product_size_id == m)).one()
    assert report["closed_now"] == 1 and row.status == "RECEIVED" and row.actual_arrival_date == date(2026, 10, 26)


def test_shipment_first_seen_closed_gets_no_arrival_date(engine):
    setup_sizes(engine)
    fake = FakeLamoda()
    fake.shipments = [{"id": "OLD", "status": "CLOSED", "plannedDate": "2026-09-01", "createdAt": "2026-08-25T00:00:00Z"}]
    with engine.begin() as conn:
        ingest_fbo_shipments(conn, client_for(fake), SkuResolver.load(conn), date(2026, 10, 21))
    assert count(engine, t_fbo_shipments) == 0


# ---------------- 1С ----------------

STOCK_FILE = (
    "﻿uid-1|A100|Куртка «Север» | 50|4|4600000000011,2000041459377|M|чёрный\n"
    "uid-2|A100|Куртка дубль|1|4600000000011|M|чёрный\n"            # другая номенклатура 1С, тот же размер
    "uid-3|A100|Куртка|-2|4600000000028|L|чёрный\n"
    "uid-4|Z9|Чужой товар|7|9999999999999|S|\n"
)


def test_stock_file_parsing_handles_bom_and_pipe_in_name():
    rows = parse_stock_rows(STOCK_FILE)
    assert rows[0]["uid_1c"] == "uid-1" and rows[0]["quantity"] == 4
    assert rows[0]["name"] == "Куртка «Север» | 50" and rows[0]["barcodes"] == ["4600000000011", "2000041459377"]


def test_newest_stock_file_is_read_without_moving(tmp_path):
    results, archive = tmp_path / "results", tmp_path / "archive"
    results.mkdir(), archive.mkdir()
    (archive / "stock_20261020080000.txt").write_text("old", encoding="utf-8")
    (archive / "stock_20261021080000.txt").write_text("new", encoding="utf-8")
    (results / "stock_20261019080000.txt").write_text("older", encoding="utf-8")
    newest = find_newest_stock_file([results, archive])
    assert newest.name == "stock_20261021080000.txt"
    assert sorted(os.listdir(archive)) == ["stock_20261020080000.txt", "stock_20261021080000.txt"]


def test_stock_1c_snapshot_sums_sizes_and_zero_fills(engine, tmp_path):
    m, l_ = setup_sizes(engine)
    with engine.begin() as conn:
        x = add_size(conn, "X1", "", "S", ["5555"])
    path = tmp_path / "stock_20261021073000.txt"
    path.write_text(STOCK_FILE, encoding="utf-8")
    with engine.begin() as conn:
        report = ingest_stock_file(conn, path, SkuResolver.load(conn))
        rows = {r.product_size_id: r.qty for r in conn.execute(select(t_stock_1c))}
        snapshot = conn.execute(select(t_stock_1c.c.snapshot_date).distinct()).scalar()
    assert rows == {m: 5, l_: -2, x: 0}
    assert snapshot == date(2026, 10, 21) and report["unmatched"] == 1 and report["negative"] == 1


def test_sales_file_parsing_and_period_replace(engine, tmp_path):
    content = ("20261010|4600000000011|2|3998,00|retail_1\n"
               "20261010|4600000000011|1|1999.00|retail_1\n"
               "20261011|4600000000011|-1|-1999|retail_1\n"
               "плохая строка\n"
               "20261011|4600000000011|1|1999|lamoda\n")
    parsed = parse_sales(content)
    assert len(parsed.rows) == 3 and len(parsed.errors) == 2
    path = tmp_path / "sales_20261010_20261011_20261012080000.txt"
    path.write_text(content, encoding="utf-8")
    with engine.begin() as conn:
        ingest_sales_files(conn, [path])
        ingest_sales_files(conn, [path])                   # повтор не задваивает
        rows = {r.sale_date: (r.qty, r.revenue) for r in conn.execute(select(t_channel_sales_daily))}
    assert rows == {date(2026, 10, 10): (Decimal("3"), Decimal("5997.00")),
                    date(2026, 10, 11): (Decimal("-1"), Decimal("-1999.00"))}
    fixed = tmp_path / "sales_20261010_20261011_20261013080000.txt"
    fixed.write_text("20261010|4600000000011|1|1999|retail_1\n", encoding="utf-8")
    with engine.begin() as conn:
        ingest_sales_files(conn, [fixed])
        rows = {r.sale_date: r.qty for r in conn.execute(select(t_channel_sales_daily))}
    assert rows == {date(2026, 10, 10): Decimal("1")}      # исправленная выгрузка заменила период

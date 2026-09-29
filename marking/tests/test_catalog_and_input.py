from decimal import Decimal

import openpyxl
import io

from conftest import fixture_bytes
from markapp.catalog import import_catalog, parse_catalog
from markapp.models import CatalogItem
from markapp.supplies import parse_input


def test_catalog_fixture_parses_by_column_codes():
    parsed = parse_catalog(fixture_bytes("lamoda_catalog_full_2026-09-28.xlsx"))
    assert not parsed.errors
    assert len(parsed.rows) == 801
    rec = next(r for r in parsed.rows if r["supplier_sku"] == "3030 KAHVE 4XL АВЕР Рубашка Д/р сатин")
    assert rec["ean"] == "2000932309880"
    # price, а не special_price (12000 против 6500 на поставке 12550)
    assert rec["price"] == Decimal("12000.00")


def test_catalog_import_is_idempotent_and_reports_changes(db):
    data = fixture_bytes("lamoda_catalog_full_2026-09-28.xlsx")
    first = import_catalog(db, data)
    db.commit()
    assert first.added == 801
    second = import_catalog(db, data)
    db.commit()
    assert (second.added, second.changed) == (0, 0)
    item = db.query(CatalogItem).filter_by(supplier_sku="3030 KAHVE 4XL АВЕР Рубашка Д/р сатин").one()
    item.price = Decimal("1.00")
    db.commit()
    third = import_catalog(db, data)
    db.commit()
    assert third.changed == 1 and "цена" in third.changes[0]


def test_catalog_duplicates_are_refused_not_guessed():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Upload Template"
    ws.append(["служебная"])
    ws.append(["описания"])
    ws.append(["Артикул #supplier_sku", "Штрихкод #product_identifier", "Цена #price"])
    ws.append(["A 1", "2000000000001", 100])
    ws.append(["A 1", "2000000000002", 200])
    ws.append(["B 1", "2000000000003", 300])
    buf = io.BytesIO()
    wb.save(buf)
    parsed = parse_catalog(buf.getvalue())
    assert [r["supplier_sku"] for r in parsed.rows] == ["B 1"]
    assert parsed.errors and "A 1" in parsed.errors[0]


def test_typical_input_file():
    parsed = parse_input(fixture_bytes("lamoda_shipment_input_typical.xlsx"))
    assert not parsed.errors
    assert len(parsed.rows) == 80
    assert sum(r.qty for r in parsed.rows) == 338
    assert parsed.extra_headers == []


def test_input_with_extra_column_keeps_it():
    parsed = parse_input(fixture_bytes("lamoda_shipment_input_with_tnved.xlsx"))
    assert not parsed.errors
    assert (len(parsed.rows), sum(r.qty for r in parsed.rows)) == (89, 366)
    assert parsed.extra_headers == ["ТНВЭД"]
    assert parsed.rows[0].extras == ["6105909000"]


def _input(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Лист1"
    ws.append(["Дата поставки"])
    ws.append(["Номер поставки"])
    ws.append(["Номер документа отгрузки"])
    ws.append(["Seller SKU", "Количество", "Стоимость", "EAN", "Вес ЮИ", "Data Matrix"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_input_quantity_must_be_positive_integer():
    parsed = parse_input(_input([["A", 2.0], ["B", 1.5], ["C", 0], ["D", "x"], ["E", 3]]))
    assert [r.sku for r in parsed.rows] == ["A", "E"]
    assert len(parsed.errors) == 3


def test_only_sku_and_quantity_are_needed():
    parsed = parse_input(_input([["A", 2]]))
    assert not parsed.errors and parsed.rows[0].file_price == "" and parsed.rows[0].file_ean == ""

"""Справочник: пул баркодов, сопоставление по любому баркоду, импорт Excel-карты."""
import pytest
from sqlalchemy import func, select

from forecast.catalog.excel_import import CatalogFileError, import_catalog
from forecast.catalog.lamoda_sync import sync_nomenclatures
from forecast.catalog.resolver import SkuResolver, normalize_code
from forecast.db.schema import t_barcode_pool, t_product_models, t_product_sizes, t_suppliers
from factories import add_size, make_xlsx

HEADER = ["Артикул", "Цвет", "Размер", "Штрихкод", "Seller SKU", "Lamoda SKU", "Категория", "Поставщик",
          "Доступен к заказу"]


# ---------------- сопоставление ----------------

def test_any_barcode_from_pool_resolves_to_same_size(engine):
    with engine.begin() as conn:
        psid = add_size(conn, "A100", "чёрный", "50", ["4600000000011", "2000041459377", "0123456789012"])
        other = add_size(conn, "A100", "чёрный", "52", ["4600000000028"])
        resolver = SkuResolver.load(conn)
    for barcode in ("4600000000011", "2000041459377", "0123456789012"):
        assert resolver.resolve(barcodes=[barcode]) == psid
    # среди нескольких баркодов записи достаточно совпадения одного
    assert resolver.resolve(barcodes=["9999999999999", "2000041459377"]) == psid
    assert resolver.resolve(barcodes=["4600000000028"]) == other
    assert resolver.resolve(barcodes=["9999999999999"]) is None


def test_barcodes_pointing_to_different_sizes_are_a_conflict(engine):
    with engine.begin() as conn:
        add_size(conn, "A100", "", "50", ["111"])
        add_size(conn, "A100", "", "52", ["222"])
        resolver = SkuResolver.load(conn)
    assert resolver.resolve_barcodes(["111", "222"]) and len(resolver.resolve_barcodes(["111", "222"])) == 2
    assert resolver.resolve(barcodes=["111", "222"]) is None


def test_lamoda_records_resolve_by_sku_seller_sku_or_barcode_as_external_sku(engine):
    with engine.begin() as conn:
        by_sku = add_size(conn, "B1", "", "M", ["300"], lamoda_sku="MP002XM0WFWSINM")
        by_seller = add_size(conn, "B1", "", "L", ["301"], seller_sku="AWER-B1-L")
        by_barcode = add_size(conn, "B1", "", "XL", ["2000000000302"])
        resolver = SkuResolver.load(conn)
    assert resolver.resolve(lamoda_sku="MP002XM0WFWSINM") == by_sku
    assert resolver.resolve(external_sku="AWER-B1-L") == by_seller
    assert resolver.resolve(external_sku="2000000000302") == by_barcode   # SKU продавца = баркод
    assert resolver.resolve(lamoda_sku="NOPE", external_sku="NOPE") is None


def test_normalize_code_handles_excel_numbers():
    assert normalize_code(2000041459377.0) == "2000041459377"
    assert normalize_code(" 2000 0414 ") == "20000414"
    assert normalize_code(1.5) == ""
    assert normalize_code(None) == ""


# ---------------- импорт Excel ----------------

def _import(engine, content: bytes):
    with engine.begin() as conn:
        return import_catalog(conn, content)


def test_import_builds_models_sizes_and_barcode_pool(engine):
    content = make_xlsx([
        ["A100", "чёрный", "50", "4600000000011, 2000041459377", "AW-A100-50", "MP002XM0A100IN50", "Куртки",
         "Фабрика", "да"],
        ["A100", "чёрный", "50", "0123456789012", None, None, None, None, None],   # тот же SKU, ещё баркод
        ["A100", "чёрный", "52", 4600000000028, None, None, "Куртки", "Фабрика", "да"],
        ["A200", None, "M", "4600000000035;4600000000042", None, None, None, None, "нет"],
    ], HEADER, title_rows=[["Карта сопоставления AWER"], []])
    report = _import(engine, content)
    assert report.header_row == 3
    assert report.errors == [] and report.conflicts == []
    assert (report.models_created, report.sizes_created, report.barcodes_added) == (2, 3, 6)
    with engine.connect() as conn:
        resolver = SkuResolver.load(conn)
        a100_50 = resolver.resolve(barcodes=["0123456789012"])
        assert resolver.resolve(barcodes=["4600000000011"]) == a100_50
        assert resolver.resolve(lamoda_sku="MP002XM0A100IN50") == a100_50
        assert resolver.resolve(external_sku="AW-A100-50") == a100_50
        a200 = conn.execute(select(t_product_models).where(t_product_models.c.article == "A200")).one()
        assert a200.color is None and a200.category == "Без категории" and a200.available_to_reorder is False
        supplier = conn.execute(select(t_suppliers)).one()
        assert supplier.name == "Фабрика" and supplier.production_days == 60   # срок по умолчанию
    assert any("проверьте" in s for s in report.suppliers_created)


def test_reimport_is_idempotent(engine):
    content = make_xlsx([["A100", "чёрный", "50", "111,222", None, None, None, None, None]], HEADER)
    _import(engine, content)
    report = _import(engine, content)
    assert (report.models_created, report.sizes_created, report.barcodes_added, report.barcodes_known) == (0, 0, 0, 2)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t_barcode_pool)).scalar() == 2


def test_barcode_of_another_size_is_reported_not_moved(engine):
    _import(engine, make_xlsx([["A100", "", "50", "111", None, None, None, None, None]], HEADER))
    report = _import(engine, make_xlsx([["A100", "", "52", "111", None, None, None, None, None]], HEADER))
    assert len(report.conflicts) == 1 and "111" in report.conflicts[0]
    with engine.connect() as conn:
        row = conn.execute(select(t_product_sizes.c.size_label).join(
            t_barcode_pool, t_barcode_pool.c.product_size_id == t_product_sizes.c.product_size_id)
            .where(t_barcode_pool.c.barcode == "111")).scalar()
    assert row == "50"


def test_rows_without_barcode_are_skipped_with_error(engine):
    report = _import(engine, make_xlsx([["A100", "", "50", None, None, None, None, None, None],
                                        ["A100", "", "52", "333", None, None, None, None, None]], HEADER))
    assert report.rows_imported == 1 and len(report.errors) == 1


def test_file_without_required_columns_is_rejected(engine):
    with pytest.raises(CatalogFileError, match="Баркод"):
        _import(engine, make_xlsx([["A100", "50"]], ["Артикул", "Размер"]))
    with pytest.raises(CatalogFileError, match="xlsx"):
        _import(engine, b"not an excel file")


# ---------------- сверка с каталогом Lamoda ----------------

def test_lamoda_nomenclatures_fill_missing_skus_by_barcode(engine):
    with engine.begin() as conn:
        psid = add_size(conn, "A100", "", "50", ["111", "222"])
        report = sync_nomenclatures(conn, [
            {"sku": "MP1IN50", "parentSku": "MP1", "externalSku": "AW-50", "barcode": "222"},
            {"sku": "MPX", "parentSku": "MPX0", "externalSku": "?", "barcode": "999", "name": "чужой"},
        ])
        size = conn.execute(select(t_product_sizes).where(t_product_sizes.c.product_size_id == psid)).one()
        model = conn.execute(select(t_product_models)).one()
        seller = conn.execute(select(t_barcode_pool.c.seller_sku).where(t_barcode_pool.c.barcode == "222")).scalar()
    assert (report["matched"], report["unmatched_total"], report["sizes_filled"]) == (1, 1, 1)
    assert size.lamoda_sku == "MP1IN50" and model.lamoda_sku == "MP1" and seller == "AW-50"

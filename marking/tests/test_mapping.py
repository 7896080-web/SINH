"""Страница сопоставления: правила sync_admin на реальных данных.

Справочник 1С собирается из выгрузки остатков (`stock_1c_2026-09-28.xlsx`:
uid, артикул, наименование, размер, цвет, баркоды через запятую) в формат
`barcodes_*.txt` sync_admin — строка на баркод.
"""
import io

import openpyxl
import pytest

from conftest import fixture_bytes
from markapp import mapping as M, onec, settings
from markapp.catalog import import_catalog
from markapp.models import OnecBarcode, OnecTask

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _dict_text_from_stock() -> str:
    wb = openpyxl.load_workbook(io.BytesIO(fixture_bytes("stock_1c_2026-09-28.xlsx")), read_only=True)
    lines = []
    for uid, art, name, size, color, bcs, _ in list(wb.active.iter_rows(values_only=True))[1:]:
        for b in str(bcs or "").split(","):
            if b.strip():
                lines.append(f"{uid}|{art}|{name}|{b.strip()}|{size}|{color}")
    return "\n".join(lines)


@pytest.fixture
def catalog(db):
    import_catalog(db, fixture_bytes("lamoda_catalog_full_2026-09-28.xlsx"))
    db.commit()


@pytest.fixture
def dictionary(db, catalog):
    M.load_dictionary(db, M.parse_barcode_dict(_dict_text_from_stock()), "тест")
    db.commit()


def test_pipe_inside_the_name_is_cut_from_the_right():
    rows = M.parse_barcode_dict("u1|A-1|Рубашка | сатин|2000000000017|XL|Синий")
    assert rows == [{"item_id": "u1", "article": "A-1", "name": "Рубашка | сатин",
                     "barcode": "2000000000017", "size": "XL", "color": "Синий"}]


def test_real_catalog_against_real_1c_stock(db, dictionary):
    """558 артикулов каталога есть в выгрузке 1С; 12 из них — не по первому
    штрихкоду пула, и они обязаны сопоставиться (правило 2)."""
    rows = M.build(db)
    c = M.counts(rows)
    assert len(rows) == 801
    assert c["ok"] == 558 and c["not_in_1c"] == 243
    assert c["ambiguous"] == c["shared_sku"] == c["no_ean"] == 0
    second = [r for r in rows if r.status == "ok" and len(r.pool) > 1 and r.pool.index(r.ean) >= 0]
    assert len(second) == 12
    assert all(r.ean in r.pool for r in rows if r.status == "ok")


def test_barcode_on_two_skus_is_ambiguous(db, catalog):
    from markapp.models import CatalogItem
    ean = db.query(CatalogItem).first().ean
    M.load_dictionary(db, M.parse_barcode_dict(
        f"u1|A|Товар один|{ean}|M|Синий\nu2|B|Товар два|{ean}|L|Синий"), "тест")
    db.flush()
    r = next(r for r in M.build(db) if r.ean == ean)
    assert r.status == "ambiguous" and len(r.others) == 2 and not r.item_id


def test_two_lamoda_articles_on_one_sku_break_one_to_one(db, catalog):
    from markapp.models import CatalogItem
    a, b = db.query(CatalogItem).limit(2).all()
    M.load_dictionary(db, M.parse_barcode_dict(
        f"u1|A|Товар|{a.ean}|M|Синий\nu1|A|Товар|{b.ean}|M|Синий"), "тест")
    db.flush()
    rows = {r.supplier_sku: r for r in M.build(db)}
    assert rows[a.supplier_sku].status == rows[b.supplier_sku].status == "shared_sku"
    assert rows[a.supplier_sku].others == [b.supplier_sku]


def test_empty_file_does_not_wipe_the_snapshot(db, dictionary):
    before = db.query(OnecBarcode).count()
    with pytest.raises(M.MappingError):
        M.load_dictionary(db, M.parse_barcode_dict("мусор\nбез разделителей"), "тест")
    assert db.query(OnecBarcode).count() == before > 0


def test_dictionary_request_needs_mark_2(db, exchange_dirs):
    settings.put(db, onec.EPF_VERSION, "mark-1")
    db.commit()
    with pytest.raises(onec.OnecError, match="mark-2"):
        onec.enqueue_barcode_dict(db)
    settings.put(db, onec.EPF_VERSION, "mark-2")
    db.commit()
    task = onec.enqueue_barcode_dict(db)
    assert task.line == f"BARCODE_DICT|{task.order_id}"


def test_dictionary_answer_is_loaded_and_archived(db, catalog, exchange_dirs):
    settings.put(db, onec.EPF_VERSION, "mark-2")
    db.commit()
    task = onec.enqueue_barcode_dict(db)
    db.commit()
    onec.publish_pending(db)
    res = exchange_dirs.ONEC_RESULTS_DIR
    (res / "barcodes_mark_9.txt").write_text(_dict_text_from_stock(), encoding="utf-8")
    (res / "result_mark_9.txt").write_text(f"{task.order_id}|OK|справочник выгружен|BARCODE_DICT",
                                           encoding="utf-8")
    onec.collect_results(db)
    assert M.counts(M.build(db))["ok"] == 558
    assert not list(res.glob("*")) and (exchange_dirs.ONEC_ARCHIVE_DIR / "barcodes_mark_9.txt").exists()


def test_ok_without_file_keeps_previous_snapshot(db, dictionary, exchange_dirs):
    settings.put(db, onec.EPF_VERSION, "mark-2")
    db.commit()
    task = onec.enqueue_barcode_dict(db)
    db.commit()
    onec.publish_pending(db)
    (exchange_dirs.ONEC_RESULTS_DIR / "result_mark_9.txt").write_text(
        f"{task.order_id}|OK|справочник выгружен|BARCODE_DICT", encoding="utf-8")
    onec.collect_results(db)
    db.refresh(task)
    assert task.status == "failed" and "не принят" in task.result_detail
    assert M.counts(M.build(db))["ok"] == 558


def test_page_filter_and_full_export(client, db, dictionary):
    r = client.get("/mapping")
    assert r.status_code == 200 and "сопоставлен 558" in r.text and "нет в 1С 243" in r.text
    r = client.get("/mapping?status=not_in_1c")
    assert "найдено 243" in r.text
    r = client.get("/mapping/export?status=problems")
    ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
    assert ws.max_row == 1 + 243


def test_file_upload_through_the_page(client, db, catalog):
    r = client.post("/mapping/import", files={"file": ("barcodes_x.txt",
                    _dict_text_from_stock().encode("utf-8"), "text/plain")})
    assert "Справочник 1С загружен" in r.text
    r = client.post("/mapping/import", files={"file": ("x.txt", b"", "text/plain")})
    assert "Справочник не загружен" in r.text


def test_request_button_refuses_old_epf(client, db):
    settings.put(db, onec.EPF_VERSION, "mark-1")
    db.commit()
    r = client.post("/mapping/request")
    assert "mark-2" in r.text
    assert db.query(OnecTask).count() == 0


def test_unknown_status_in_the_address_does_not_break_the_page(client, db, dictionary):
    r = client.get("/mapping?status=чтото")
    assert r.status_code == 200 and "найдено 0" in r.text


def test_export_never_turns_text_into_a_formula(client, db, catalog):
    from markapp.models import CatalogItem
    item = db.query(CatalogItem).first()
    M.load_dictionary(db, M.parse_barcode_dict(f"u1|=1+1|=HYPERLINK('x')|{item.ean}|M|Синий"), "тест")
    db.commit()
    r = client.get("/mapping/export")
    ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
    cells = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert cells and all(c.data_type == "s" for c in cells)


def test_numeric_ean_cell_keeps_its_leading_zero():
    from markapp.catalog import ean_from_cell
    assert ean_from_cell(460123456786) == "0460123456786"     # верная контрольная цифра
    assert ean_from_cell(2000932309880) == "2000932309880"
    assert ean_from_cell(460123456789) == "460123456789"      # не EAN — без догадки
    assert ean_from_cell("0460123456789") == "0460123456789"

"""Мэппинг Sync Admin в репрайсере: правила старшинства как в Sync Admin."""
import io
import os
import subprocess
import sys
from pathlib import Path

from openpyxl import Workbook

from priceapp import mapping
from priceapp.models import SyncBarcode
from priceapp.pricing import article_map
from tests import factories as F

ROOT = Path(__file__).resolve().parent.parent
HEAD = ["ID_1С", "Баркод", "Артикул", "Наименование", "Размер", "Цвет", "Источник", "Добавлен"]


def _file(rows) -> bytes:
    b = Workbook()
    b.active.append(HEAD)
    for r in rows:
        b.active.append(r)
    buf = io.BytesIO()
    b.save(buf)
    return buf.getvalue()


def _status(db, acc):
    return {r.item.barcode: (r.status, r.item_id) for r in mapping.build(db, acc.id)}


def test_parse_takes_only_sync_admin_links():
    rows = mapping.parse_sync_mapping(_file([
        ["u1", "b1", "A", "", "M", "", "excel_import", ""],
        ["u2", "b2", "A", "", "L", "", "выгрузка из 1С", ""],
        ["u3", "b3", "A", "", "S", "", "1c_dict", ""],
        ["u4", "b4", "B", "", "S", "", "pool_match", ""],
        ["u5", "b5", "B", "", "S", "", "excel_repoint", ""]]))
    assert [(r["barcode"], r["source"]) for r in rows] == [("b1", "excel_import"), ("b4", "pool_match"),
                                                          ("b5", "excel_repoint")]


def test_not_a_mapping_file_is_refused():
    b = Workbook()
    b.active.append(["что-то", "другое"])
    buf = io.BytesIO()
    b.save(buf)
    try:
        mapping.parse_sync_mapping(buf.getvalue())
        assert False
    except mapping.MappingError as e:
        assert "Мэппинг" in str(e)


def test_human_links_beat_dictionary_guess_does_not(db):
    w = F.account(db, "wb", "ИП 1")
    F.sku(db, "dict1", "100", barcodes=["b1"])          # справочник: b1 -> dict1
    F.sku(db, "dict2", "200", barcodes=["b2"])          # справочник: b2 -> dict2
    for code, ext in (("b1", "1:1"), ("b2", "2:1"), ("b3", "3:1"), ("b4", "4:1")):
        F.item(db, w, code, external_id=ext)
    mapping.load_sync_mapping(db, [
        {"item_id": "rep1", "barcode": "b1", "article": "X", "name": "", "size": "", "color": "",
         "source": "excel_repoint"},                     # переподвязка — главнее справочника
        {"item_id": "g2", "barcode": "b2", "article": "", "name": "", "size": "", "color": "",
         "source": "pool_match"},                        # догадка — справочник её перебивает
        {"item_id": "g3", "barcode": "b3", "article": "Y", "name": "", "size": "", "color": "",
         "source": "pool_match"},                        # догадка там, где справочник молчит
        {"item_id": "imp4", "barcode": "b4", "article": "Z", "name": "", "size": "", "color": "",
         "source": "excel_import"}])
    db.commit()
    st = _status(db, w)
    assert st["b1"] == ("sync", "rep1")
    assert st["b2"] == ("ok", "dict2")
    assert st["b3"] == ("sync", "g3")
    assert st["b4"] == ("sync", "imp4")
    assert article_map(db)["imp4"] == "Z"
    assert set(mapping.account_items(db, w.id)) == {"rep1", "dict2", "g3", "imp4"}


def test_empty_snapshot_is_refused_and_keeps_the_old_one(db):
    db.add(SyncBarcode(barcode="b1", item_id="u1", source="excel_import"))
    db.commit()
    try:
        mapping.load_sync_mapping(db, [])
        assert False
    except mapping.MappingError:
        pass
    assert db.query(SyncBarcode).count() == 1


def test_script_shows_diff_and_writes_only_with_apply(db, tmp_path):
    w = F.account(db, "wb", "ИП 1")
    F.sku(db, "dict1", "100", barcodes=["b1"])
    F.item(db, w, "b1", external_id="1:1"); F.item(db, w, "b9", external_id="9:1")
    f = tmp_path / "m.xlsx"
    f.write_bytes(_file([["rep1", "b1", "A", "", "", "", "excel_repoint", ""],
                         ["new9", "b9", "B", "", "", "", "excel_import", ""]]))
    env = {**os.environ, "PYTHONIOENCODING": "cp1251"}
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts" / "load_sync_mapping.py"), str(f), *a],
                                    capture_output=True, cwd=ROOT, env=env)
    r = run()
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = r.stdout.decode("cp1251")
    assert "стало сопоставлено 1; другой SKU 1С 1" in out and "b1: было dict1 -> стало rep1" in out
    assert "Ничего не записано" in out
    db.expire_all()
    assert db.query(SyncBarcode).count() == 0
    r = run("--apply")
    assert r.returncode == 0 and "Записано" in r.stdout.decode("cp1251")
    db.expire_all()
    assert db.query(SyncBarcode).count() == 2

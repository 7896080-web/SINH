"""Файл поставки для Lamoda с кодами (ТЗ 6.3): эталон — файл kiz-tool по 12550,
принятый Lamoda. Запись кодов сверяется с ним побайтно (сырой XML)."""
import io
import re
import zipfile
from collections import OrderedDict
from datetime import date
from decimal import Decimal

import openpyxl
import pytest

from conftest import FIXTURES
from markapp import settings, supply_file
from markapp.crypto import encrypt_value
from markapp.labels import GS, normalize
from markapp.models import GtinPair, MarkCode, Supply, SupplyRow

REF = FIXTURES / "reference_kiz_tool_supply_12550.xlsx"


def _reference_rows():
    ws = openpyxl.load_workbook(REF)["Лист1"]
    rows = []
    for r in ws.iter_rows(min_row=5, values_only=True):
        if r[0]:
            rows.append((str(r[0]), str(r[2]), str(r[3]), normalize(str(r[5]))))
    return rows


def _shared_codes(data: bytes) -> list[str]:
    """Сырой текст кодов в XML: принятый файл держит их в sharedStrings, openpyxl
    пишет строки прямо в лист (inlineStr) — сравнивается содержимое <t>."""
    z = zipfile.ZipFile(io.BytesIO(data))
    part = "xl/sharedStrings.xml" if "xl/sharedStrings.xml" in z.namelist() else "xl/worksheets/sheet1.xml"
    return re.findall(r"<t[^>]*>(01\d{14}21.*?)</t>", z.read(part).decode("utf-8"))


@pytest.fixture
def supply12550(db):
    ref = _reference_rows()
    org = settings.lamoda_org(db)
    s = Supply(number="12550", doc_number="12550", organization_id=org.id, status="moved",
               supply_date=date(2026, 10, 1))
    grouped = OrderedDict()
    for sku, price, ean, code in ref:
        grouped.setdefault((sku, price, ean), []).append(code)
    s.rows = [SupplyRow(position=i, supplier_sku=sku, qty=len(codes), price=Decimal(price), ean=ean)
              for i, ((sku, price, ean), codes) in enumerate(grouped.items(), 1)]
    db.add(s)
    db.flush()
    for (sku, _, _), codes in grouped.items():
        gtin = codes[0][2:16]
        db.add(GtinPair(supplier_sku=sku, gtin=gtin, source="manual"))
        for code in codes:
            db.add(MarkCode(cis=code.split(GS)[0], full_enc=encrypt_value(code), gtin=gtin, supplier_sku=sku,
                            supply_id=s.id, status="INTRODUCED"))
    db.commit()
    return s, ref


def test_codes_are_written_byte_for_byte_like_the_accepted_file(db, supply12550):
    s, ref = supply12550
    data = supply_file.build(db, s)
    ours = _shared_codes(data)
    # В общих строках эталона есть и пример кода с листа инструкций — он не наш.
    theirs = [t for t in _shared_codes(REF.read_bytes()) if "_x001D_" in t]
    assert len(ours) == len(ref) == 44
    assert ours == theirs                     # GS → _x001D_, < > & — сущности, как у принятого файла


def test_layout_one_row_per_code(db, supply12550):
    s, ref = supply12550
    ws = openpyxl.load_workbook(io.BytesIO(supply_file.build(db, s)))["Лист1"]
    assert ws["B1"].value == 46296 and ws["B2"].value == "12550" and ws["B3"].value == "12550"
    rows = [r for r in ws.iter_rows(min_row=5, values_only=True) if r[0]]
    assert len(rows) == 44 and all(r[1] == 1 for r in rows)
    assert [(r[0], r[2], r[3]) for r in rows] == [(a, b, c) for a, b, c, _ in ref]
    assert "Инструкции по заполнению" in openpyxl.load_workbook(io.BytesIO(supply_file.build(db, s))).sheetnames


def test_refused_unless_every_code_is_introduced(db, supply12550):
    s, _ = supply12550
    c = db.query(MarkCode).first()
    c.status = "APPLIED"
    db.commit()
    with pytest.raises(supply_file.SupplyFileError, match="не в обороте"):
        supply_file.build(db, s)


def test_refused_when_codes_do_not_match_quantities(db, supply12550):
    s, _ = supply12550
    s.rows[0].qty += 1
    db.commit()
    with pytest.raises(supply_file.SupplyFileError, match="штук"):
        supply_file.build(db, s)


def test_route_gives_the_file(client, db, supply12550):
    s, _ = supply12550
    r = client.post(f"/supplies/{s.id}/codes/supply-xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert "12550.xlsx" in r.headers["content-disposition"]

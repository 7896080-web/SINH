"""УПД с КИЗ не выпускается, пока коды программы не в обороте.

УПД уходит в ЧЗ через ЭДО, и ЧЗ отказывает в передаче кода не в обороте —
уже после того, как Lamoda подписала документ. Проверка «выпустить с
ошибками» (`force`) это не снимает: это не замечание к XML.
"""
from conftest import fixture_bytes
from markapp import upd_service as U
from markapp.models import MarkCode, Supply, UpdDocument

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FBO = "lamoda_postavki_fbo_12550.xlsx"


def _moved_supply_with_codes(client, db, status: str | None):
    r = client.post("/catalog/import", files={"file": ("cat.xlsx", fixture_bytes(
        "lamoda_catalog_full_2026-09-28.xlsx"), XLSX)})
    assert "новых 801" in r.text
    client.post("/supplies/new", data={"number": "12550", "doc_number": "", "supply_date": "2026-10-02"},
                files={"file": ("in.xlsx", fixture_bytes("lamoda_shipment_input_typical.xlsx"), XLSX)})
    s = db.query(Supply).one()
    s.status = "moved"
    rows = U.read_fbo(fixture_bytes(FBO)).rows
    if status is not None:
        for r in rows:
            db.add(MarkCode(cis=r.kiz.strip(), full_enc="x", gtin=r.gtin, supplier_sku=r.name.strip(),
                            supply_id=s.id, status=status))
    db.commit()
    r = client.post(f"/supplies/{s.id}/fbo", files={"file": ("fbo.xlsx", fixture_bytes(FBO), XLSX)})
    assert "Выгрузка сверена с поставкой: 338 строк" in r.text
    return s, rows


def test_upd_is_refused_while_codes_are_not_introduced(client, db):
    s, rows = _moved_supply_with_codes(client, db, "APPLIED")
    r = client.get(f"/supplies/{s.id}")
    assert "Выпуск закрыт: не в обороте 338 из 338 кодов (нанесён: 338)" in r.text
    for force in ("", "1"):
        r = client.post(f"/supplies/{s.id}/upd", data={"doc_date": "2026-10-01", "totals_mode": "rows",
                                                       "force": force})
        assert "УПД не выпущен: не в обороте 338 из 338 кодов" in r.text
    db.refresh(s)
    assert s.status == "moved" and db.query(UpdDocument).count() == 0


def test_one_code_not_introduced_is_enough_to_refuse(client, db):
    s, rows = _moved_supply_with_codes(client, db, "INTRODUCED")
    c = db.query(MarkCode).filter(MarkCode.cis == rows[0].kiz.strip()).one()
    c.status = ""
    db.commit()
    r = client.post(f"/supplies/{s.id}/upd", data={"doc_date": "2026-10-01", "totals_mode": "rows"})
    assert "не в обороте 1 из 338 кодов (статус не запрашивался: 1)" in r.text
    assert db.query(UpdDocument).count() == 0


def test_upd_is_issued_when_every_code_is_introduced(client, db):
    s, _ = _moved_supply_with_codes(client, db, "INTRODUCED")
    r = client.get(f"/supplies/{s.id}")
    assert "Все 338 кодов выгрузки — в обороте." in r.text
    r = client.post(f"/supplies/{s.id}/upd", data={"doc_date": "2026-10-01", "totals_mode": "rows"})
    assert "УПД 12550 выпущен: 338 поз." in r.text
    assert "заказаны не программой" not in r.text


def test_foreign_codes_only_warn(client, db):
    """Коды заказаны вне программы: статуса она не знает — предупреждение, не запрет."""
    s, _ = _moved_supply_with_codes(client, db, None)
    r = client.post(f"/supplies/{s.id}/upd", data={"doc_date": "2026-10-01", "totals_mode": "rows"})
    assert "УПД 12550 выпущен: 338 поз." in r.text
    assert "коды заказаны не программой — ввод в оборот она не видит" in r.text

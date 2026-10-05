"""Правка количеств одной формой и «Уменьшить до остатка 1С» (05.10.2026):
поставка 12561 — 7 строк из 38 «не хватает», править по одной было долго,
а страница после каждой правки прыгала наверх."""
from datetime import date
from decimal import Decimal

import pytest

from markapp import onec, settings
from markapp import supplies as S
from markapp.models import OnecTask, Supply, SupplyRow


def _checked_short(db):
    org = settings.lamoda_org(db)
    s = Supply(number="12599", doc_number="12599", organization_id=org.id, status="draft",
               supply_date=date(2026, 10, 6))
    s.rows = [SupplyRow(position=1, supplier_sku="A", qty=5, ean="2000000000011", price=Decimal("100")),
              SupplyRow(position=2, supplier_sku="B", qty=3, ean="2000000000028", price=Decimal("100")),
              SupplyRow(position=3, supplier_sku="C", qty=2, ean="2000000000035", price=Decimal("100"))]
    db.add(s)
    db.flush()
    task = onec.enqueue_check(db, s)
    task.status = "sent"
    db.commit()
    # баркод|ID товара|артикул|наименование|размер|цвет|остаток ЦС|нужно|статус
    check = ("2000000000011|ID1|A|a|M|red|1|5|short\n"
             "2000000000028|ID2|B|b|M|red|-2|3|short\n"
             "2000000000035|ID3|C|c|M|red|9|2|ok\n")
    onec.apply_result_text(db, f"{task.order_id}|ERROR|не хватает|SUPPLY_CHECK", check)
    db.commit()
    return s


def test_fit_to_stock_reduces_short_rows_and_checks_without_new_request(db):
    s = _checked_short(db)
    assert s.status == "draft" and [r.onec_status for r in s.rows] == ["short", "short", "ok"]
    changes = onec.fit_to_stock(db, s)
    db.commit()
    assert {r.supplier_sku: r.qty for r in s.rows} == {"A": 1, "C": 2}       # B: остаток -2 — убрана
    assert len(changes) == 2 and "строка убрана" in changes[1]
    assert s.status == "checked"
    assert db.query(OnecTask).filter(OnecTask.command == "SUPPLY_CHECK").count() == 1   # в 1С не ходили


def test_fit_to_stock_refuses_after_composition_changed(db):
    s = _checked_short(db)
    S.update_quantities(s, {s.rows[2].id: 1})          # правка после проверки
    with pytest.raises(S.SupplyError, match="состав менялся"):
        onec.fit_to_stock(db, s)


def test_fit_to_stock_needs_an_answer(db):
    org = settings.lamoda_org(db)
    s = Supply(number="12598", doc_number="12598", organization_id=org.id, status="draft")
    s.rows = [SupplyRow(position=1, supplier_sku="A", qty=5, ean="2000000000011")]
    db.add(s)
    db.commit()
    with pytest.raises(S.SupplyError, match="нет ответа 1С"):
        onec.fit_to_stock(db, s)


def test_update_quantities_in_one_go(db):
    s = _checked_short(db)
    ids = [r.id for r in s.rows]
    assert S.update_quantities(s, {ids[0]: 1, ids[1]: 0, ids[2]: 2}) == 2   # C не менялась
    assert {r.supplier_sku: r.qty for r in s.rows} == {"A": 1, "C": 2}
    assert S.update_quantities(s, {ids[0]: 1}) == 0
    with pytest.raises(S.SupplyError):
        S.update_quantities(s, {ids[0]: -1})


def test_rows_form_and_fit_button_return_to_rows(client, db):
    s = _checked_short(db)
    page = client.get(f"/supplies/{s.id}").text
    assert 'id="rows"' in page and 'form="rowsForm"' in page and "Уменьшить до остатка 1С" in page
    r = client.post(f"/supplies/{s.id}/rows", data={f"qty_{s.rows[2].id}": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#rows")
    db.expire_all()
    assert db.get(Supply, s.id).rows[2].qty == 1

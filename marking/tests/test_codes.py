"""Коды поставки: заказ в СУЗ, получение, статусы, ввод в оборот. ЧЗ подменён."""
import base64
import json
from datetime import date

import pytest

from markapp import chz_api, chz_auth, codes as C, settings
from markapp.crypto import decrypt_value
from markapp.models import CodeOrder, GtinPair, IntroduceDoc, MarkCode, Supply, SupplyRow

GTIN_A, GTIN_B = "04620180403734", "04630688318072"
GS = "\x1d"


def full(gtin, n):
    return f"01{gtin}21{n:013d}{GS}91EE10{GS}92{'A' * 44}"


@pytest.fixture
def fake(monkeypatch):
    calls = {"orders": [], "gets": [], "cises": [], "docs": []}
    state = {"buffer": "PENDING", "codes": {}, "cises": {}, "doc_status": "CHECKED_OK"}

    def create_order(oms, body, token, sig):
        calls["orders"].append((oms, body, token, sig))
        return f"SUZ-{len(calls['orders'])}"

    def suz_get(path, token, sig):
        calls["gets"].append((path, sig))
        if path.startswith("/order/status"):
            return [{"bufferStatus": state["buffer"], "availableCodes": 5}]
        gtin = path.split("gtin=")[1].split("&")[0]
        qty = int(path.split("quantity=")[1])
        return {"codes": state["codes"].get(gtin, [])[:qty]}

    def cises_info(cis, token):
        calls["cises"].append(list(cis))
        return {c: state["cises"].get(c, "EMITTED") for c in cis}

    def create_document(doc, sig, token):
        calls["docs"].append((doc, sig))
        return "DOC-1"

    monkeypatch.setattr(chz_api, "create_order", create_order)
    monkeypatch.setattr(chz_api, "suz_get", suz_get)
    monkeypatch.setattr(chz_api, "cises_info", cises_info)
    monkeypatch.setattr(chz_api, "create_document", create_document)
    monkeypatch.setattr(chz_api, "document_status", lambda doc_id, token: (state["doc_status"], ""))
    monkeypatch.setattr(C, "request_backup", lambda: calls.setdefault("backups", []).append(1))
    return calls, state


@pytest.fixture
def supply(db):
    org = settings.lamoda_org(db)
    org.oms_id, org.connection_id = "OMS-1", "CONN-1"
    chz_auth.store(db, org, "true_api", "T-TOKEN")
    chz_auth.store(db, org, "suz", "S-TOKEN")
    s = Supply(number="12561", doc_number="12561", organization_id=org.id, status="moved",
               extra_headers=["ТНВЭД"])
    s.rows = [SupplyRow(position=1, supplier_sku="A 1", qty=2, extras=["6205200000"]),
              SupplyRow(position=2, supplier_sku="B 1", qty=1, extras=[""])]
    db.add(s)
    db.add_all([GtinPair(supplier_sku="A 1", gtin=GTIN_A, source="manual"),
                GtinPair(supplier_sku="B 1", gtin=GTIN_B, source="manual")])
    db.commit()
    return s


def test_short_cis_is_parsed_not_cut():
    assert C.short_cis(full(GTIN_A, 7)) == f"01{GTIN_A}21{7:013d}"
    with pytest.raises(C.CodesError):
        C.short_cis(f"01{GTIN_A}21{7:013d}")


def test_orders_are_written_before_sending_and_not_duplicated(db, supply, fake):
    orders = C.prepare_orders(db, supply, "op")
    assert [(o.supplier_sku, o.quantity, o.status) for o in orders] == [("A 1", 2, "new"), ("B 1", 1, "new")]
    body = json.loads(orders[0].body)
    assert body["products"][0] == {"gtin": GTIN_A, "quantity": 2, "serialNumberType": "OPERATOR",
                                   "templateId": 10, "cisType": "UNIT"}
    assert body["attributes"] == {"releaseMethodType": "PRODUCTION"}
    # Повтор до отправки — те же заказы, не новые.
    assert [o.id for o in C.prepare_orders(db, supply, "op")] == [o.id for o in orders]
    assert db.query(CodeOrder).count() == 2


def test_full_cycle_order_codes_statuses_introduce(db, supply, fake):
    calls, state = fake
    for o in C.prepare_orders(db, supply, "op"):
        C.send_order(db, o, "SIG-" + o.supplier_sku)
    # Тело уходит ровно то, что подписано; токен — СУЗ.
    assert calls["orders"][0] == ("OMS-1", db.get(CodeOrder, 1).body, "S-TOKEN", "SIG-A 1")
    assert all(line.ordering and not line.deficit for line in C.plan(db, supply))
    assert C.prepare_orders(db, supply, "op") == []          # всё уже заказано

    step = C.steps(db, supply)[0]
    assert step["action"] == "status" and step["sign"] == "/api/v3" + step["path"]
    C.run_step(db, db.get(CodeOrder, step["order_id"]), step["action"], step["path"], "S")
    assert db.get(CodeOrder, 1).status == "sent"             # PENDING — ждём

    state["buffer"] = "ACTIVE"
    state["codes"] = {GTIN_A: [full(GTIN_A, 1), full(GTIN_A, 2)], GTIN_B: [full(GTIN_B, 3)]}
    for _ in range(2):
        for st in C.steps(db, supply):
            C.run_step(db, db.get(CodeOrder, st["order_id"]), st["action"], st["path"], "S")
    db.commit()
    assert [o.status for o in db.query(CodeOrder).order_by(CodeOrder.id)] == ["done", "done"]
    code = db.query(MarkCode).filter(MarkCode.gtin == GTIN_A).first()
    assert code.cis == f"01{GTIN_A}21{1:013d}" and GS not in code.cis
    assert decrypt_value(code.full_enc) == full(GTIN_A, 1) and "EE10" not in code.full_enc
    # Внеочередная копия запускается маршрутом ПОСЛЕ коммита — test_audit_fixes.
    assert C.steps(db, supply) == []

    # Нанесение ещё не пришло — ввести нечего.
    C.refresh_statuses(db, supply)
    with pytest.raises(C.CodesError, match="Нанесён"):
        C.prepare_introduce(db, supply, "op")

    state["cises"] = {c.cis: "APPLIED" for c in db.query(MarkCode)}
    C.refresh_statuses(db, supply)
    # У B 1 нет данных документа — отказ с перечнем.
    with pytest.raises(C.CodesError, match="B 1"):
        C.prepare_introduce(db, supply, "op")
    C.save_defaults(supply, "6109100000", "CONFORMITY_DECLARATION", "ЕАЭС N RU Д-RU.X.1", "2026-01-15",
                    "2026-09-30")
    doc = C.prepare_introduce(db, supply, "op")
    body = json.loads(base64.b64decode(doc.document))
    assert body["production_type"] == "OWN_PRODUCTION" and body["owner_inn"] == supply.organization.inn
    by_code = {p["uit_code"]: p for p in body["products"]}
    assert by_code[f"01{GTIN_A}21{1:013d}"]["tnved_code"] == "6205200000"     # из файла поставки
    assert by_code[f"01{GTIN_B}21{3:013d}"]["tnved_code"] == "6109100000"     # умолчание поставки
    assert by_code[f"01{GTIN_B}21{3:013d}"]["certificate_document_data"][0]["certificate_type"] \
        == "CONFORMITY_DECLARATION"

    C.send_introduce(db, doc, "ATTACHED\nSIG")
    assert calls["docs"] == [(doc.document, "ATTACHEDSIG")] and doc.status == "sent"
    assert C.ready_codes(db, supply) == []                     # в документе — повторно не уйдут
    C.refresh_documents(db)
    assert doc.status == "CHECKED_OK"
    state["cises"] = {c.cis: "INTRODUCED" for c in db.query(MarkCode)}
    C.refresh_statuses(db, supply)
    lines = C.plan(db, supply)
    assert all(line.state == "в обороте" for line in lines) and C.summary(lines)["done"]


def test_stale_step_is_refused(db, supply, fake):
    for o in C.prepare_orders(db, supply, "op"):
        C.send_order(db, o, "S")
    st = C.steps(db, supply)[0]
    with pytest.raises(C.CodesError, match="устарел"):
        C.run_step(db, db.get(CodeOrder, st["order_id"]), "codes", st["path"], "S")


def test_rejected_order_is_shown(db, supply, fake):
    _, state = fake
    for o in C.prepare_orders(db, supply, "op"):
        C.send_order(db, o, "S")
    state["buffer"] = "REJECTED"
    st = C.steps(db, supply)[0]
    C.run_step(db, db.get(CodeOrder, st["order_id"]), st["action"], st["path"], "S")
    assert db.get(CodeOrder, st["order_id"]).status == "rejected"
    assert C.plan(db, supply)[0].state == "ошибка заказа"


def test_test_supply_and_unmoved_supply_do_not_order(db, supply, fake):
    supply.is_test = True
    with pytest.raises(C.CodesError, match="тестовая"):
        C.prepare_orders(db, supply, "op")
    supply.is_test, supply.status = False, "checked"
    with pytest.raises(C.CodesError, match="после перемещения"):
        C.prepare_orders(db, supply, "op")


def test_without_suz_login_nothing_is_sent(db, supply, fake):
    calls, _ = fake
    org = supply.organization
    chz_auth.forget(org, "suz")
    orders = C.prepare_orders(db, supply, "op")
    with pytest.raises(C.CodesError, match="нужен вход"):
        C.send_order(db, orders[0], "S")
    assert calls["orders"] == []


def test_codes_page_renders(client, db, supply, fake):
    r = client.get(f"/supplies/{supply.id}/codes")
    assert r.status_code == 200
    assert "Итог ввода в оборот" in r.text and "A 1" in r.text and "коды не заказаны" in r.text
    r = client.post(f"/supplies/{supply.id}/codes/order-prepare")
    assert r.status_code == 200 and len(r.json()["orders"]) == 2

"""Аудит этапа 4 (05.10): заказ и получение кодов СУЗ, ввод в оборот, вход в ЧЗ.

Каждый тест — на конкретный путь к дублю заказа, потере кодов, вечно занятым
кодам или документу, утверждающему то, чего человек не видел.
"""
import base64
import json
from datetime import timedelta

import pytest

from markapp import chz_api, chz_auth, codes as C
from markapp.models import CodeOrder, IntroduceDoc, MarkCode, Supply
from markapp.timeutils import now_utc, today_local
from test_audit_fixes import _applied, _to_ready
from test_codes import GTIN_A, GTIN_B, fake, full, supply  # noqa: F401 (фикстуры)

UUID = "1b2c3d4e-0000-4000-8000-00000000000a"


def _step_all(db, supply):
    for st in C.steps(db, supply):
        C.run_step(db, db.get(CodeOrder, st["order_id"]), st["action"], st["path"], "S")


# --- Заказ и получение ---------------------------------------------------------------

def test_journal_is_recovered_on_page_load(client, db, supply, fake):
    """Коды пришли и легли в журнал, а запись в базу не дошла: открытие страницы
    возвращает их, а не оставляет на диске до дозаказа."""
    _, state = fake
    state["codes"] = {GTIN_A: [full(GTIN_A, 1), full(GTIN_A, 2)]}
    _to_ready(db, supply, state)
    order = db.query(CodeOrder).filter(CodeOrder.gtin == GTIN_A).one()
    C._journal(order, {"codes": [full(GTIN_A, 1), full(GTIN_A, 2)]})
    db.commit()
    assert db.query(MarkCode).count() == 0
    r = client.get(f"/supplies/{supply.id}/codes")
    assert r.status_code == 200
    db.expire_all()
    assert db.query(MarkCode).filter(MarkCode.order_id == order.id).count() == 2
    assert db.get(CodeOrder, order.id).status == "done"


def test_exhausted_buffer_with_missing_codes_does_not_free_quantity(db, supply, fake):
    """Буфер закрыт, получено меньше заказанного — `incomplete`, и дефицита нет:
    дозаказ только решением человека."""
    _, state = fake
    state["codes"] = {GTIN_A: [full(GTIN_A, 1)], GTIN_B: [full(GTIN_B, 3)]}
    _to_ready(db, supply, state)
    _step_all(db, supply)                       # A: 1 из 2, B: 1 из 1
    state["buffer"] = "EXHAUSTED"
    _step_all(db, supply)
    order = db.query(CodeOrder).filter(CodeOrder.gtin == GTIN_A).one()
    assert order.status == "incomplete" and "Сверьте с ЛК СУЗ" in order.error
    assert C.prepare_orders(db, supply, "op") == []
    C.resolve_unknown_order(db, order, "")
    assert order.status == "error"
    again = C.prepare_orders(db, supply, "op")
    assert [(o.supplier_sku, o.quantity) for o in again] == [("A 1", 1)]


def test_journal_recovery_waits_before_declaring_incomplete(db, supply, fake):
    _, state = fake
    state["codes"] = {GTIN_A: [full(GTIN_A, 1), full(GTIN_A, 2)], GTIN_B: [full(GTIN_B, 3)]}
    _to_ready(db, supply, state)
    order = db.query(CodeOrder).filter(CodeOrder.gtin == GTIN_A).one()
    C._journal(order, {"codes": [full(GTIN_A, 1), full(GTIN_A, 2)]})
    order.status = "sent"
    db.commit()
    state["buffer"] = "EXHAUSTED"
    st = next(s for s in C.steps(db, supply) if s["order_id"] == order.id)
    C.run_step(db, order, st["action"], st["path"], "S")
    assert order.status == "done" and order.received == 2


def test_recovery_does_not_reopen_a_closed_order(db, supply, fake):
    _, state = fake
    _to_ready(db, supply, state)
    order = db.query(CodeOrder).filter(CodeOrder.gtin == GTIN_A).one()
    C._journal(order, {"codes": [full(GTIN_A, 1)]})
    order.status, order.error = "rejected", "отклонён СУЗ"
    C.recover_journal(db)
    assert order.status == "rejected" and order.error == "отклонён СУЗ"


def test_journal_of_another_order_with_the_same_number_is_ignored(db, supply, fake):
    """После восстановления базы номера заказов выдаются заново — журнал старого
    заказа не ложится на новый с тем же номером."""
    _, state = fake
    _to_ready(db, supply, state)
    order = db.query(CodeOrder).filter(CodeOrder.gtin == GTIN_A).one()
    path = C._journal(order, {"codes": [full(GTIN_A, 1)]})
    rec = json.loads(C.decrypt_value(path.read_text(encoding="ascii")))
    rec["supply_id"] = 999
    path.write_text(C.encrypt_value(json.dumps(rec)), encoding="ascii")
    assert C.recover_journal(db) == 0


def test_claim_stamps_time_so_old_order_is_not_expired_mid_request(db, supply, fake):
    orders = C.prepare_orders(db, supply, "op")
    o = orders[0]
    o.updated_at = now_utc() - timedelta(minutes=30)       # подготовлен давно
    db.commit()
    assert C._claim(db, CodeOrder, o.id, "new", "sending")
    assert C.expire_sending(db) == 0                       # отправка только началась
    db.refresh(o)
    assert o.status == "sending"


def test_resolve_needs_a_real_uuid_not_used_by_another_order(db, supply, fake, monkeypatch):
    monkeypatch.setattr(chz_api, "create_order",
                        lambda *a: (_ for _ in ()).throw(chz_api.ChzApiError("таймаут", None)))
    orders = C.prepare_orders(db, supply, "op")
    for o in orders:
        with pytest.raises(C.CodesError):
            C.send_order(db, o, "S")
    a, b = orders
    with pytest.raises(C.CodesError, match="UUID"):
        C.resolve_unknown_order(db, a, "SUZ-1")
    C.resolve_unknown_order(db, a, UUID)
    with pytest.raises(C.CodesError, match="уже записан"):
        C.resolve_unknown_order(db, b, UUID)


@pytest.mark.parametrize("status", [408, 409])
def test_408_and_409_are_not_proof_that_no_order_exists(db, supply, fake, monkeypatch, status):
    monkeypatch.setattr(chz_api, "create_order",
                        lambda *a: (_ for _ in ()).throw(chz_api.ChzApiError("x", status)))
    o = C.prepare_orders(db, supply, "op")[0]
    with pytest.raises(C.CodesError):
        C.send_order(db, o, "S")
    assert o.status == "unknown"


def test_unparsable_codes_answer_is_journaled_not_treated_as_empty(db, supply, fake, monkeypatch):
    _, state = fake
    _to_ready(db, supply, state)
    monkeypatch.setattr(chz_api, "suz_get", lambda path, token, sig: "<html>что-то пошло не так</html>")
    order = db.query(CodeOrder).filter(CodeOrder.gtin == GTIN_A).one()
    st = next(s for s in C.steps(db, supply) if s["order_id"] == order.id)
    with pytest.raises(C.CodesError, match="не разобран"):
        C.run_step(db, order, st["action"], st["path"], "S")
    assert order.status == "ready"
    assert list(C.journal_dir().glob(f"order{order.id}-*.enc"))


# --- Статусы и документы -------------------------------------------------------------

def test_one_failing_chunk_does_not_stop_other_supplies(db, supply, fake, monkeypatch):
    calls, state = fake
    _applied(db, supply, state)
    other = Supply(number="12562", doc_number="12562", organization_id=supply.organization_id, status="moved")
    db.add(other)
    db.flush()
    db.add(MarkCode(cis=f"01{GTIN_A}21{9:013d}", full_enc="x", gtin=GTIN_A, supply_id=other.id))
    for c in db.query(MarkCode):
        c.status, c.status_at = "", None
    db.commit()
    asked = []

    def cises(cis, token):
        asked.append(list(cis))
        if f"01{GTIN_A}21{9:013d}" not in cis:
            raise chz_api.ChzApiError("HTTP 400", 400)
        return {c: "APPLIED" for c in cis}

    monkeypatch.setattr(chz_api, "cises_info", cises)
    for _ in range(3):
        C.refresh_statuses(db, limit=1)
    assert any(f"01{GTIN_A}21{9:013d}" in a for a in asked)
    assert db.query(MarkCode).filter(MarkCode.supply_id == other.id).one().status == "APPLIED"


def test_latin_c_is_a_certificate_and_unknown_type_from_file_is_a_problem(db, supply, fake):
    assert C.guess_cert_type("ЕАЭС RU C-CN.АЖ40.В.12345/24") == "CONFORMITY_CERTIFICATE"
    assert C.guess_cert_type("ЕАЭС N RU Д-RU.X.1") == "CONFORMITY_DECLARATION"
    supply.extra_headers = ["ТНВЭД", "Номер разрешительного документа", "Дата разрешительного документа"]
    supply.rows[0].extras = ["6205200000", "TC 12345", "01.01.2026"]
    supply.rows[1].extras = ["", "", ""]
    _, problems = C.attrs_by_sku(db, supply)
    assert any("декларация это или сертификат" in p for p in problems)


def test_production_date_is_required_and_not_in_future(db, supply, fake):
    _, state = fake
    _applied(db, supply, state)
    supply.intro_attrs = dict(supply.intro_attrs, production_date="")
    with pytest.raises(C.CodesError, match="дату производства"):
        C.prepare_introduce(db, supply, "op")
    with pytest.raises(C.CodesError, match="в будущем"):
        C.save_defaults(supply, "6109100000", "CONFORMITY_DECLARATION", "N RU Д-1", "2026-01-15",
                        (today_local() + timedelta(days=1)).isoformat())


def test_document_summary_names_what_is_signed(db, supply, fake):
    _, state = fake
    _applied(db, supply, state)
    doc = C.prepare_introduce(db, supply, "op")
    assert "Дата производства: 30.09.2026" in doc.summary
    assert "A 1 — 2 шт.: ТН ВЭД 6205200000 (файл поставки)" in doc.summary
    assert "B 1 — 1 шт.: ТН ВЭД 6109100000 (общее для поставки)" in doc.summary
    body = json.loads(base64.b64decode(doc.document))
    assert len(body["products"]) == 3


def test_refused_document_releases_codes_and_unknown_status_keeps_them(db, supply, fake):
    _, state = fake
    _applied(db, supply, state)
    doc = C.prepare_introduce(db, supply, "op")
    db.commit()
    C.send_introduce(db, doc, "S")
    state["doc_status"] = "SOMETHING_NEW"
    C.refresh_documents(db)
    assert doc.status == "sent" and "незнакомый статус" in doc.error
    assert C.ready_codes(db, supply) == []
    state["doc_status"] = "PROCESSING_ERROR"
    C.refresh_documents(db)
    assert doc.status == "CHECKED_NOT_OK"
    assert len(C.ready_codes(db, supply)) == 3


def test_sent_document_closes_when_codes_are_introduced_or_goes_unknown_after_a_day(db, supply, fake):
    _, state = fake
    _applied(db, supply, state)
    doc = C.prepare_introduce(db, supply, "op")
    db.commit()
    C.send_introduce(db, doc, "S")
    state["doc_status"] = ""                              # ЧЗ документ по номеру не нашёл
    doc.sent_at = now_utc() - timedelta(days=2)
    db.commit()
    C.refresh_documents(db)
    assert doc.status == "unknown"
    for c in db.query(MarkCode):
        c.status = "INTRODUCED"
    db.commit()
    C.refresh_documents(db)
    assert doc.status == "CHECKED_OK"


def test_test_supply_cannot_send_introduction(db, supply, fake):
    _, state = fake
    _applied(db, supply, state)
    doc = C.prepare_introduce(db, supply, "op")
    supply.is_test = True
    db.commit()
    with pytest.raises(C.CodesError, match="тестовая"):
        C.send_introduce(db, doc, "S")


# --- Вход ---------------------------------------------------------------------------

def _jwt(payload: dict) -> str:
    b = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"h.{b}.s"


def test_token_inn_is_read_from_jwt():
    assert chz_auth.token_inn(_jwt({"inn": "910223073620", "exp": 1})) == "910223073620"
    assert chz_auth.token_inn(_jwt({"exp": 1})) is None
    assert chz_auth.token_inn("не jwt") is None


def test_login_refused_when_token_belongs_to_another_inn(client, db, monkeypatch):
    from markapp import settings
    org = settings.lamoda_org(db)
    monkeypatch.setattr(chz_auth, "signer_inn", lambda sig: org.inn)
    monkeypatch.setattr(chz_auth, "sign_in", lambda *a: _jwt({"inn": "123456789012", "exp": 4102444800}))
    r = client.post(f"/organizations/{org.id}/chz-login/token",
                    json={"kind": "true_api", "uuid": "u", "signature": "S", "cert_inn": org.inn})
    assert r.status_code == 400 and "токен не сохранён" in r.json()["error"]
    db.refresh(org)
    assert chz_auth.token(org) is None

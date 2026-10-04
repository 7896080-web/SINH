"""Находки аудита 01.10.2026 — на каждую тест, чтобы она не вернулась."""
import base64
import json

import pytest

from markapp import chz_api, chz_auth, codes as C, onec, settings
from markapp.models import CodeOrder, IntroduceDoc, MarkCode, OnecTask, Supply
from test_codes import GTIN_A, GTIN_B, fake, full, supply  # noqa: F401 (фикстуры)


# --- Заказ: двойной заказ невозможен -------------------------------------------------

def _boom(status):
    def create_order(*a, **k):
        raise chz_api.ChzApiError("сбой", status)
    return create_order


def test_timeout_makes_order_unknown_and_blocks_reorder(db, supply, fake, monkeypatch):
    calls, _ = fake
    monkeypatch.setattr(chz_api, "create_order", _boom(None))
    order = C.prepare_orders(db, supply, "op")[0]
    with pytest.raises(C.CodesError):
        C.send_order(db, order, "S")
    db.commit()
    assert order.status == "unknown"
    # Количество занято неизвестным заказом — новый на него не создаётся.
    assert all(o.supplier_sku != order.supplier_sku for o in C.prepare_orders(db, supply, "op"))
    assert C.plan(db, supply)[0].state.startswith("ошибка заказа: исход неизвестен")
    # Человек нашёл заказ в ЛК СУЗ — продолжаем получать по нему коды.
    C.resolve_unknown_order(db, order, "SUZ-REAL")
    assert (order.status, order.suz_order_id) == ("sent", "SUZ-REAL")


def test_unknown_order_confirmed_absent_frees_quantity(db, supply, fake, monkeypatch):
    monkeypatch.setattr(chz_api, "create_order", _boom(503))
    order = C.prepare_orders(db, supply, "op")[0]
    with pytest.raises(C.CodesError):
        C.send_order(db, order, "S")
    C.resolve_unknown_order(db, order, "")
    assert order.status == "error"
    again = [o for o in C.prepare_orders(db, supply, "op") if o.supplier_sku == order.supplier_sku]
    assert again and again[0].id != order.id


def test_rejected_by_suz_is_error_not_unknown(db, supply, fake, monkeypatch):
    monkeypatch.setattr(chz_api, "create_order", _boom(400))
    order = C.prepare_orders(db, supply, "op")[0]
    with pytest.raises(C.CodesError):
        C.send_order(db, order, "S")
    assert order.status == "error"


def test_second_send_of_same_order_is_refused(db, supply, fake):
    calls, _ = fake
    order = C.prepare_orders(db, supply, "op")[0]
    C.send_order(db, order, "S")
    db.commit()
    order.status = "new"            # вторая вкладка видела старое состояние
    db.expire(order)
    with pytest.raises(C.CodesError, match="уже отправляется"):
        C.send_order(db, db.get(CodeOrder, order.id), "S")
    assert len(calls["orders"]) == 1


# --- Получение кодов: ничего не теряется ------------------------------------------------

def _to_ready(db, supply, state):
    for o in C.prepare_orders(db, supply, "op"):
        C.send_order(db, o, "S")
    state["buffer"] = "ACTIVE"
    for st in C.steps(db, supply):
        C.run_step(db, db.get(CodeOrder, st["order_id"]), st["action"], st["path"], "S")


def test_bad_code_does_not_lose_the_block_and_is_journaled(db, supply, fake, tmp_path, monkeypatch):
    _, state = fake
    monkeypatch.setattr(C, "journal_dir", lambda: tmp_path)
    state["codes"] = {GTIN_A: ["мусор", full(GTIN_B, 9), full(GTIN_A, 1), full(GTIN_A, 2)],
                      GTIN_B: [full(GTIN_B, 3)]}
    _to_ready(db, supply, state)
    real_get = chz_api.suz_get
    # Блок целиком, с мусором внутри — как если бы СУЗ ответил неожиданно.
    monkeypatch.setattr(chz_api, "suz_get", lambda path, t, s: {"codes": state["codes"][GTIN_A]}
                        if GTIN_A in path and path.startswith("/codes") else real_get(path, t, s))
    for st in C.steps(db, supply):
        C.run_step(db, db.get(CodeOrder, st["order_id"]), st["action"], st["path"], "S")
    db.commit()
    a = db.query(CodeOrder).filter(CodeOrder.gtin == GTIN_A).one()
    assert a.received == 2 and a.status == "done"
    assert "не принято кодов: 2" in a.error and "GTIN" in a.error
    # Чужой GTIN не закреплён под артикулом A.
    assert db.query(MarkCode).filter(MarkCode.gtin == GTIN_A).count() == 2
    journals = list(tmp_path.glob("order*.enc"))
    assert journals and "EE10" not in journals[0].read_text()          # зашифровано
    # Повторный разбор журнала ничего не задваивает.
    assert C.recover_journal(db) == 0


def test_recover_journal_restores_codes_lost_after_suz_answer(db, supply, fake, tmp_path, monkeypatch):
    _, state = fake
    monkeypatch.setattr(C, "journal_dir", lambda: tmp_path)
    state["codes"] = {GTIN_A: [full(GTIN_A, 1), full(GTIN_A, 2)], GTIN_B: [full(GTIN_B, 3)]}
    _to_ready(db, supply, state)
    for st in C.steps(db, supply):
        C.run_step(db, db.get(CodeOrder, st["order_id"]), st["action"], st["path"], "S")
    db.rollback()                    # запись в базу не удалась после ответа СУЗ
    assert db.query(MarkCode).count() == 0
    assert C.recover_journal(db) == 3
    db.commit()
    assert db.query(MarkCode).count() == 3


def test_route_starts_backup_after_commit(client, db, supply, fake, monkeypatch):
    _, state = fake
    seen = []
    from markapp.database import SessionLocal

    def check():
        s = SessionLocal()
        try:
            seen.append(s.query(MarkCode).count())     # видит ли копия закоммиченные коды
        finally:
            s.close()
    monkeypatch.setattr(C, "request_backup", check)
    state["codes"] = {GTIN_A: [full(GTIN_A, 1), full(GTIN_A, 2)], GTIN_B: [full(GTIN_B, 3)]}
    _to_ready(db, supply, state)
    db.commit()
    for st in C.steps(db, supply):
        r = client.post(f"/supplies/{supply.id}/codes/step", json={**{k: st[k] for k in ("order_id", "action", "path")},
                                                                    "signature": "S"})
        assert r.status_code == 200, r.text
    assert seen and seen[-1] == 3


# --- Ввод в оборот --------------------------------------------------------------------

def _applied(db, supply, state):
    state["codes"] = {GTIN_A: [full(GTIN_A, 1), full(GTIN_A, 2)], GTIN_B: [full(GTIN_B, 3)]}
    _to_ready(db, supply, state)
    for st in C.steps(db, supply):
        C.run_step(db, db.get(CodeOrder, st["order_id"]), st["action"], st["path"], "S")
    state["cises"] = {c.cis: "APPLIED" for c in db.query(MarkCode)}
    C.refresh_statuses(db, supply)
    C.save_defaults(supply, "6109100000", "CONFORMITY_DECLARATION", "ЕАЭС N RU Д-RU.X.1", "2026-01-15",
                    "2026-09-30")


def test_introduce_timeout_keeps_codes_busy_until_human_decides(db, supply, fake, monkeypatch):
    _, state = fake
    _applied(db, supply, state)
    doc = C.prepare_introduce(db, supply, "op")
    db.commit()
    monkeypatch.setattr(chz_api, "create_document", lambda *a: (_ for _ in ()).throw(chz_api.ChzApiError("t")))
    with pytest.raises(C.CodesError):
        C.send_introduce(db, doc, "S")
    assert doc.status == "unknown" and C.ready_codes(db, supply) == []
    # Все коды стали «в обороте» — документ закрывается сам.
    state["cises"] = {c.cis: "INTRODUCED" for c in db.query(MarkCode)}
    C.refresh_statuses(db, supply)
    C.refresh_documents(db)
    assert doc.status == "CHECKED_OK"


def test_unknown_doc_released_by_human(db, supply, fake, monkeypatch):
    _, state = fake
    _applied(db, supply, state)
    doc = C.prepare_introduce(db, supply, "op")
    db.commit()
    monkeypatch.setattr(chz_api, "create_document", lambda *a: "")       # принят без номера
    C.send_introduce(db, doc, "S")
    assert doc.status == "unknown"
    C.release_unknown_doc(db, doc)
    assert len(C.ready_codes(db, supply)) == 3


def test_document_has_application_date_and_tnved_from_nk(db, supply, fake):
    from markapp.models import NkCard
    _, state = fake
    _applied(db, supply, state)
    db.add(NkCard(gtin=GTIN_B, status="ok", tn_ved="6109902000"))
    db.commit()
    doc = C.prepare_introduce(db, supply, "op")
    body = json.loads(base64.b64decode(doc.document))
    p = {x["uit_code"]: x for x in body["products"]}
    assert p[f"01{GTIN_B}21{3:013d}"]["tnved_code"] == "6109902000"      # НК важнее умолчания
    assert all(x["application_date"] for x in body["products"])


def test_tnved_conflict_between_file_and_nk_is_a_problem(db, supply, fake):
    from markapp.models import NkCard
    _, state = fake
    _applied(db, supply, state)
    db.add(NkCard(gtin=GTIN_A, status="ok", tn_ved="6109100000"))      # в файле у A — 6205200000
    db.commit()
    with pytest.raises(C.CodesError, match="ТН ВЭД в файле 6205200000, в Нацкаталоге 6109100000"):
        C.prepare_introduce(db, supply, "op")


def test_permit_number_from_file_without_date_is_not_mixed_with_defaults(db, supply, fake):
    _, state = fake
    _applied(db, supply, state)
    supply.extra_headers = ["ТНВЭД", "Номер разрешительного документа"]
    for r in supply.rows:
        r.extras = ["6205200000", "ЕАЭС N RU Д-RU.Y.99"] if r.supplier_sku == "A 1" else ["", ""]
    with pytest.raises(C.CodesError, match="номер документа без даты"):
        C.prepare_introduce(db, supply, "op")


def test_txt_only_when_everything_is_introduced(client, db, supply, fake):
    _, state = fake
    _applied(db, supply, state)
    db.commit()
    r = client.post(f"/supplies/{supply.id}/codes/txt", follow_redirects=False)
    assert r.status_code == 303                                         # отказ с сообщением
    for c in db.query(MarkCode):
        c.status = "INTRODUCED"
    db.commit()
    r = client.post(f"/supplies/{supply.id}/codes/txt")
    assert r.status_code == 200 and r.content.count(b"\r\n") == 3 and b"\x1d" in r.content
    r = client.post(f"/supplies/{supply.id}/codes/labels")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


# --- Вход в ЧЗ ------------------------------------------------------------------------

def test_login_without_readable_inn_is_refused(client, db, monkeypatch):
    org = settings.lamoda_org(db)
    monkeypatch.setattr(chz_auth, "signer_inn", lambda sig: None)
    monkeypatch.setattr(chz_auth, "sign_in", lambda *a, **k: "T")
    url = f"/organizations/{org.id}/chz-login/token"
    r = client.post(url, json={"kind": "true_api", "uuid": "u", "signature": "S"})
    assert r.status_code == 400 and "не прочитан" in r.json()["error"]
    r = client.post(url, json={"kind": "true_api", "uuid": "u", "signature": "S", "cert_inn": "910223073099"})
    assert r.status_code == 400
    r = client.post(url, json={"kind": "true_api", "uuid": "u", "signature": "S", "cert_inn": org.inn})
    assert r.status_code == 200


def test_forget_keeps_a_newer_token(db):
    org = settings.lamoda_org(db)
    chz_auth.store(db, org, "true_api", "NEW")
    chz_auth.forget(org, "true_api", "OLD")            # запрос шёл со старым токеном
    assert chz_auth.token(org) == "NEW"
    chz_auth.forget(org, "true_api", "NEW")
    assert chz_auth.token(org) is None


def test_vat_zero_survives_saving(client, db):
    org = settings.lamoda_org(db)
    org.vat_rate = 0
    db.commit()
    assert 'name="vat_rate" value="0"' in client.get(f"/organizations/{org.id}").text


# --- Обмен с 1С -------------------------------------------------------------------------

def _supply_with_task(db, command, status="sent"):
    from markapp.models import SupplyRow
    org = settings.lamoda_org(db)
    s = Supply(number="12571", doc_number="12571", organization_id=org.id, status="draft")
    s.rows = [SupplyRow(position=1, supplier_sku="A 1", qty=1, ean="4600000000001")]
    db.add(s)
    db.flush()
    task = onec.enqueue_check(db, s) if command == "SUPPLY_CHECK" else onec.enqueue_movement(db, s)
    task.status = status
    db.commit()
    return s, task


def test_old_ok_does_not_check_an_edited_supply(db):
    s, task = _supply_with_task(db, "SUPPLY_CHECK")
    s.rows[0].qty = 2                                  # правка после отправки, новой проверки нет
    db.commit()
    onec.apply_result_text(db, f"{task.order_id}|OK||SUPPLY_CHECK",
                           "4600000000001|ok|ID|art|name|S|red|5\n")
    assert s.status == "draft"


def test_late_movement_ok_does_not_roll_back_upd_issued(db):
    s, task = _supply_with_task(db, "SUPPLY_MOVEMENT")
    s.status = "upd_issued"
    db.commit()
    onec.apply_result_text(db, f"{task.order_id}|OK|ДОК-1|SUPPLY_MOVEMENT")
    assert s.status == "upd_issued"


def test_supply_number_is_unique_ignoring_case(db):
    from markapp import supplies as S
    _supply_with_task(db, "SUPPLY_CHECK")
    assert any("уже есть" in e for e in S.validate_numbers(db, "12571", "12571"))
    org = settings.lamoda_org(db)
    db.add(Supply(number="AB1", doc_number="AB1", organization_id=org.id))
    db.commit()
    assert any("уже есть" in e for e in S.validate_numbers(db, "ab1", "ab1"))


def test_background_loop_survives_a_failing_job(monkeypatch):
    from markapp.workers import background
    background._safe(lambda: 1 / 0)                     # не бросает наружу


def test_gtin_written_as_number_with_two_leading_zeros():
    from markapp.gtin import normalize_gtin
    assert normalize_gtin(460018040373) == "00460018040373"


# --- «Поставки FBO»: коды сверяются, а не только итоги ------------------------------------

class _Row:
    def __init__(self, name, kiz):
        self.name, self.kiz = name, kiz


def _short(gtin, n):
    return f"01{gtin}21{n:013d}"


def test_swapped_codes_between_sizes_are_caught(db):
    from markapp.models import GtinPair
    from markapp.upd_service import _kiz_problems
    org = settings.lamoda_org(db)
    s = Supply(number="12580", doc_number="12580", organization_id=org.id)
    db.add(s)
    db.add_all([GtinPair(supplier_sku="A 2XL", gtin=GTIN_A, source="manual"),
                GtinPair(supplier_sku="A 3XL", gtin=GTIN_B, source="manual")])
    db.commit()
    rows = [_Row("A 2XL", _short(GTIN_B, 1)), _Row("A 2XL", _short(GTIN_A, 2)),
            _Row("A 3XL", _short(GTIN_A, 3))]
    problems = _kiz_problems(rows, s, db)
    assert any("разных GTIN" in p for p in problems)
    assert any("в справочнике" in p for p in problems)
    # Без справочника перепутанный размер всё равно виден по второму GTIN.
    assert any("разных GTIN" in p for p in _kiz_problems(rows, s, None))


def test_full_code_or_broken_gtin_in_fbo_is_not_a_short_ki(db):
    from markapp.upd_service import _kiz_problems
    rows = [_Row("A", full(GTIN_A, 1)), _Row("A", "01046201804037352100000000000001")]
    assert len(_kiz_problems(rows, Supply(id=0), None)) == 2


def test_codes_not_attached_to_this_supply_are_caught(db, supply, fake):
    from markapp.upd_service import _kiz_problems
    db.add(MarkCode(cis=_short(GTIN_A, 1), full_enc="x", gtin=GTIN_A, supplier_sku="A 1", supply_id=supply.id))
    db.commit()
    rows = [_Row("A 1", _short(GTIN_A, 1)), _Row("A 1", _short(GTIN_A, 7))]
    assert any("не закреплён" in p for p in _kiz_problems(rows, supply, db))

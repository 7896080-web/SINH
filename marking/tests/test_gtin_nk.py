import io
import json
from pathlib import Path

import openpyxl
import pytest

from conftest import FIXTURES, fixture_bytes
from markapp import gtin as G
from markapp import chz_auth, nk, settings
from markapp.crypto import decrypt_value
from markapp.models import GtinPair, NkCard, NkRequest
from markapp.upd_service import read_fbo

PG_DIR = FIXTURES / "product_gtin"


def _import_all_product_gtin(db):
    total = G.AddResult()
    for f in sorted(PG_DIR.glob("*.xlsx")):
        total.merge(G.import_product_gtin(db, f.read_bytes(), f.name))
    db.commit()
    return total


# --- GTIN -----------------------------------------------------------------------

@pytest.mark.parametrize("g", ["04620180403734", "04630688318072", "04620180407800"])
def test_real_gtins_pass_check_digit(g):
    assert G.check_digit_ok(g)


def test_bad_gtins_are_named():
    assert "14 цифр" in G.validate_gtin("4620180403734x")
    assert "контрольная" in G.validate_gtin("04620180403735")


def test_gtin_from_code():
    assert G.gtin_of_code("0104620180407800215GhgBEO?eUXmV") == "04620180407800"
    assert G.gtin_of_code("мусор") == ""


# --- product_gtin -----------------------------------------------------------------

def test_history_of_product_gtin_gives_1012_one_to_one_pairs(db):
    """Факт из ТЗ (5.3): 10 файлов, 1012 пар строго один к одному."""
    res = _import_all_product_gtin(db)
    assert not res.conflicts and not res.invalid
    assert db.query(GtinPair).count() == 1012
    # Эти пары Lamoda уже получала — в выгрузку они не попадут.
    assert G.pending_export(db) == []


def test_conflicts_are_shown_not_overwritten(db):
    G.add_pairs(db, [("A 1", "04620180403734")], source="manual", source_name="x", exported=False)
    db.commit()
    res = G.add_pairs(db, [("A 1", "04630688318072"), ("B 1", "04620180403734")],
                      source="product_gtin", source_name="файл", exported=True)
    db.commit()
    assert res.added == 0 and len(res.conflicts) == 2
    assert db.query(GtinPair).one().gtin == "04620180403734"


def test_repeat_inside_one_file_does_not_crash(db):
    res = G.add_pairs(db, [("A 1", "04620180403734"), ("A 1", "04620180403734")],
                      source="product_gtin", source_name="f", exported=True)
    db.commit()
    assert (res.added, res.same) == (1, 1)


def test_gtin_as_number_keeps_leading_zero(db):
    res = G.add_pairs(db, [("A 1", 4620180403734)], source="manual", source_name="x", exported=False)
    assert res.added == 1
    db.flush()
    assert db.query(GtinPair).one().gtin == "04620180403734"


# --- Поставки FBO -----------------------------------------------------------------

def test_fbo_12550_gives_80_pairs_54_of_them_new(db):
    """Факт из ТЗ (6.4): 80 пар, 26 совпали с product_gtin, 54 новых."""
    _import_all_product_gtin(db)
    fbo = read_fbo(fixture_bytes("lamoda_postavki_fbo_12550.xlsx"))
    assert len(G.pairs_from_fbo(fbo.rows)) == 80
    res = G.import_fbo_pairs(db, fbo.rows, "fbo.xlsx")
    db.commit()
    assert (res.added, res.same, res.conflicts) == (54, 26, [])
    assert len(G.pending_export(db)) == 54


# --- выгрузка product_gtin ----------------------------------------------------------

def test_export_follows_lamoda_template(db):
    pairs = [(f"SKU {i}", None) for i in range(1203)]
    # Реальных GTIN на 1203 строки нет — берём валидные синтетические с 046.
    def make(i):
        body = f"0460000{i:06d}"
        digits = [int(c) for c in body]
        check = (10 - sum(d * (3 if k % 2 == 0 else 1) for k, d in enumerate(reversed(digits))) % 10) % 10
        return body + str(check)
    rows = [(sku, make(i)) for i, (sku, _) in enumerate(pairs)]
    rows.append(("чужой префикс", "04700000000013"))
    G.add_pairs(db, rows, source="manual", source_name="x", exported=False)
    db.commit()
    pending = G.pending_export(db)
    assert len(pending) == 1203                      # без «чужого префикса»
    files = G.build_product_gtin_files(pending)
    assert len(files) == 2
    wb = openpyxl.load_workbook(io.BytesIO(files[0]))
    assert wb.sheetnames == ["Лист1", "Инструкции по заполнению"]
    ws = wb["Лист1"]
    assert [c.value for c in ws[1]] == ["Supplier SKU", "Gtin"]
    assert ws.max_row == 1001
    assert isinstance(ws["B2"].value, str) and ws["B2"].value.startswith("046")
    assert openpyxl.load_workbook(io.BytesIO(files[1]))["Лист1"].max_row == 204
    G.mark_exported(pending)
    db.commit()
    assert G.pending_export(db) == []


# --- Нацкаталог -------------------------------------------------------------------

ATTRS = [
    {"attr_id": 1, "attr_name": "Цвет", "attr_value": "Коричневый"},
    {"attr_id": 2, "attr_name": "Размер", "attr_value": "56"},
    {"attr_id": 3, "attr_name": "Размер одежды", "attr_value": "4XL"},
    {"attr_id": 4, "attr_name": "Код ТН ВЭД", "attr_value": "6205200000"},
]


def test_attribute_names_are_priority_ordered():
    assert nk.pick_attr(ATTRS, ["Размер одежды", "Размер"]) == "4XL"
    assert nk.pick_attr(ATTRS, ["размер"]) == "56"          # без учёта регистра
    assert nk.pick_attr(ATTRS, ["Нет такого"]) == ""


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


class _FakeClient:
    """Отвечает как НК; записывает, что спросили."""
    calls: list = []
    status = 200
    missing: set = set()

    def __init__(self, key):
        self.key = key

    def product(self, gtins):
        _FakeClient.calls.append((self.key, list(gtins)))
        if _FakeClient.status != 200:
            return _Resp(_FakeClient.status, text="err")
        items = [{"good_id": 1, "good_name": f"Товар {g}", "good_status": "published",
                  "identified_by": [{"type": "gtin", "value": g}], "good_attrs": ATTRS}
                 for g in gtins if g not in _FakeClient.missing]
        return _Resp(200, {"apiversion": 3, "result": items})


@pytest.fixture
def fake_nk(db):
    _FakeClient.calls, _FakeClient.status, _FakeClient.missing = [], 200, set()
    org = settings.lamoda_org(db)
    nk.set_api_key(db, org, "KEY-123")
    chz_auth.store(db, org, "true_api", "TOKEN-123")
    db.commit()
    return _FakeClient


def _pairs(db, n):
    def make(i):
        body = f"0460000{i:06d}"
        digits = [int(c) for c in body]
        check = (10 - sum(d * (3 if k % 2 == 0 else 1) for k, d in enumerate(reversed(digits))) % 10) % 10
        return body + str(check)
    G.add_pairs(db, [(f"S{i}", make(i)) for i in range(n)], source="manual", source_name="x", exported=False)
    db.commit()


def test_key_is_stored_encrypted(db, fake_nk):
    org = settings.lamoda_org(db)
    assert "KEY-123" not in org.nk_api_key_enc
    assert decrypt_value(org.nk_api_key_enc) == "KEY-123"


def test_fetch_batches_by_25_and_stops_at_the_limit(db, fake_nk):
    _pairs(db, 400)
    stats = nk.fetch_due(db, fake_nk)
    # 7 запросов за окно (70% от 10), по 25 GTIN.
    assert stats["requests"] == 7
    assert all(len(g) <= 25 for _, g in fake_nk.calls)
    assert db.query(NkCard).filter(NkCard.status == "ok").count() == 175
    assert db.query(NkRequest).count() == 7
    # Окно исчерпано — второй проход ничего не спрашивает.
    assert nk.fetch_due(db, fake_nk)["requests"] == 0


def test_card_is_parsed_and_raw_is_kept(db, fake_nk):
    _pairs(db, 1)
    nk.fetch_due(db, fake_nk)
    card = db.query(NkCard).one()
    assert (card.color, card.size, card.tn_ved) == ("Коричневый", "4XL", "6205200000")
    assert json.loads(card.raw)["good_name"].startswith("Товар")


def test_missing_card_is_not_found(db, fake_nk):
    _pairs(db, 2)
    fake_nk.missing = {db.query(GtinPair).first().gtin}
    nk.fetch_due(db, fake_nk)
    assert db.query(NkCard).filter(NkCard.status == "not_found").count() == 1


def test_429_pauses_and_keeps_cards_waiting(db, fake_nk):
    _pairs(db, 3)
    fake_nk.status = 429
    stats = nk.fetch_due(db, fake_nk)
    assert stats["requests"] == 1 and "429" in stats["note"]
    assert db.query(NkCard).filter(NkCard.status == "pending").count() == 3
    fake_nk.status = 200
    assert nk.fetch_due(db, fake_nk)["requests"] == 0      # пауза держится


def test_fetch_goes_with_the_login_token(db, fake_nk):
    _pairs(db, 1)
    nk.fetch_due(db, fake_nk)
    assert fake_nk.calls[0][0] == "TOKEN-123"


def test_rejected_token_is_forgotten_and_cards_wait_for_login(db, fake_nk):
    """401 — токен отозван: карточки не «ошибка», а ждут; дальше — новый вход."""
    _pairs(db, 2)
    fake_nk.status = 401
    stats = nk.fetch_due(db, fake_nk)
    assert "нужен вход в ЧЗ" in stats["note"]
    assert db.query(NkCard).filter(NkCard.status == "pending").count() == 2
    assert chz_auth.token(settings.lamoda_org(db)) is None
    assert nk.fetch_due(db, fake_nk)["requests"] == 0


def test_without_login_nothing_is_requested_but_it_is_said(db):
    """Одного API-ключа мало: True API без токена отвечает 401 (30.09.2026)."""
    _pairs(db, 2)
    nk.set_api_key(db, settings.lamoda_org(db), "KEY-123")
    _FakeClient.calls = []
    stats = nk.fetch_due(db, _FakeClient)
    assert _FakeClient.calls == [] and "нужен вход в ЧЗ" in stats["note"]


def test_reparse_after_changing_attribute_names(db, fake_nk):
    _pairs(db, 1)
    nk.fetch_due(db, fake_nk)
    settings.put(db, settings.NK_ATTR_SIZE, "Размер")
    assert nk.reparse_all(db) == 1
    assert db.query(NkCard).one().size == "56"


def test_real_client_never_sends_key_and_token_together(monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, proxies=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return _Resp(200, {"result": []})
    monkeypatch.setattr(nk.requests, "get", fake_get)
    nk.NkClient("T").product(["04620180403734", "04630688318072"])
    assert seen["url"].endswith("/nk/product")
    assert seen["params"] == {"gtins": "04620180403734;04630688318072"}
    assert seen["headers"]["Authorization"] == "Bearer T"

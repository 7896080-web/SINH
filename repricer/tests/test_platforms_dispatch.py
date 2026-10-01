"""Клиенты площадок (разбор и отправка цен без сети) и отправка подтверждённых цен."""
from datetime import timedelta
from decimal import Decimal

import pytest

from priceapp import dispatch, platforms
from priceapp.models import PriceChange, ProductPrice
from priceapp.platforms import PriceItem
from priceapp.timeutils import now_utc
from tests import factories as f


class _Resp:
    def __init__(self, data):
        self._data, self.content = data, b"x"

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _Session:
    def __init__(self, reply):
        self.headers, self.calls, self.reply = {}, [], reply

    def post(self, url, json=None, timeout=0):
        self.calls.append((url, json))
        return _Resp(self.reply(json))


def test_wb_catalog_has_size():
    rows = platforms.parse_wb_cards({"cards": [{"nmID": 5, "vendorCode": "JN", "title": "Джинсы",
                                                "sizes": [{"chrtID": 1, "techSize": "48", "skus": ["b1"]}]}]})
    assert (rows[0].external_id, rows[0].barcode, rows[0].size) == ("5:1", "b1", "48")


def test_wb_one_price_per_card_highest():
    s = _Session(lambda body: {"data": {"id": 1}, "error": False})
    res = platforms.WbClient("t", s).push_prices([PriceItem("b1", 1000, "123:1"), PriceItem("b2", 1100, "123:2"),
                                                  PriceItem("b3", 5, "")])
    url, body = s.calls[0]
    assert url.endswith("/api/v2/upload/task") and body == {"data": [{"nmID": 123, "price": 1100}]}
    assert res["ok"] == ["b1", "b2"] and res["sent_prices"]["b1"] == 1100 and res["errors"][0]["items"] == ["b3"]


def test_ozon_by_offer_id_highest_on_shared_offer():
    s = _Session(lambda body: {"result": [{"offer_id": p["offer_id"], "updated": True} for p in body["prices"]]})
    res = platforms.OzonClient("c", "k", s).push_prices([PriceItem("b1", 1299, article="OF"),
                                                         PriceItem("b2", 1399, article="OF")])
    assert s.calls[0][1] == {"prices": [{"offer_id": "OF", "price": "1399", "currency_code": "RUB"}]}
    assert set(res["ok"]) == {"b1", "b2"}


def test_kit_refuses_without_network():
    s = _Session(lambda body: {})
    res = platforms.KitClient("t", s).push_prices([PriceItem("b1", 1)])
    assert s.calls == [] and res["ok"] == [] and "Kit" in res["errors"][0]["detail"]


def test_build_client_requires_keys():
    with pytest.raises(platforms.PlatformError):
        platforms.build_client("ozon", {"client_id": "x"})


class FakeClient:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def push_prices(self, items):
        self.calls.append(items)
        if self.fail:
            return {"ok": [], "errors": [{"detail": "500"}], "sent_prices": {}}
        return {"ok": [i.barcode for i in items], "errors": [], "sent_prices": {}}


def _setup(db, status="approved", is_test=False, price=3539, cost="16.24"):
    acc = f.account(db, commission=25)
    f.rule(db, acc)
    f.manual_rate(db, "81.5")
    f.sku(db, "u1", "39681", barcodes=["b1"], cost_usd=cost)
    f.item(db, acc, "b1", "39681-L", external_id="123:9")
    ch = PriceChange(item_id="u1", account_id=acc.id, barcode="b1", new_price=price, status=status, is_test=is_test)
    db.add(ch)
    db.commit()
    return acc, ch


def test_approved_is_sent_and_remembered(db):
    acc, ch = _setup(db)
    client = FakeClient()
    dispatch.run_account(db, acc, client)
    assert (client.calls[0][0].price, client.calls[0][0].external_id) == (3539, "123:9")
    db.refresh(ch)
    assert ch.status == "sent" and db.query(ProductPrice).one().last_sent_price == 3539


@pytest.mark.parametrize("status,is_test", [("proposed", False), ("approved", True)])
def test_not_approved_or_test_never_sent(db, status, is_test):
    acc, ch = _setup(db, status, is_test)
    client = FakeClient()
    dispatch.run_account(db, acc, client)
    assert client.calls == []


def test_floor_rechecked_with_current_rate(db):
    """Подтвердили 2400 ₽ при курсе 81,5 (пол 2295), доллар вырос до 90: пол 2534."""
    from priceapp import settings
    acc, ch = _setup(db, price=2400)
    settings.put(db, settings.RATE_MANUAL, "90")
    db.commit()
    client = FakeClient()
    dispatch.run_account(db, acc, client)
    assert client.calls == []
    db.refresh(ch)
    assert ch.status == "blocked" and ch.block_reason == "floor" and "курс 90" in ch.note


def test_failure_retries_then_error(db):
    acc, ch = _setup(db)
    client = FakeClient(fail=True)
    for _ in range(dispatch.MAX_ATTEMPTS):
        ch.next_attempt_at = None
        db.commit()
        dispatch.run_account(db, acc, client)
    db.refresh(ch)
    assert ch.status == "error" and db.query(ProductPrice).count() == 0


def test_retry_waits_for_pause(db):
    acc, ch = _setup(db)
    client = FakeClient(fail=True)
    dispatch.run_account(db, acc, client)
    dispatch.run_account(db, acc, client)
    assert len(client.calls) == 1

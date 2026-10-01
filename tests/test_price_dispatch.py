"""Отправка подтверждённых цен (app/workers/price_dispatch.py) и клиенты площадок.

Закреплено: уходит только approved и только не-тестовое; пол проверяется ещё
раз при отправке (себестоимость могла вырасти); сбой площадки — повтор с
паузой; принятая цена запоминается как «последняя отправленная».
"""
from datetime import timedelta
from decimal import Decimal

from app.models import (Barcode, Platform, PlatformCatalogItem, PriceChange, PriceChangeStatus, PriceRule,
                        Product, ProductPrice)
from app.timeutils import now_utc
from app.workers.platform_clients.base import PricePushItem
from app.workers.platform_clients.kit import KitClient
from app.workers.platform_clients.ozon import OzonClient
from app.workers.platform_clients.wb import WbClient
from app.workers.price_dispatch import MAX_ATTEMPTS, run_price_dispatch
from tests.factories import make_account


class FakeClient:
    name = "fake"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def push_prices(self, items):
        self.calls.append(items)
        if self.fail:
            return {"ok": [], "errors": [{"detail": "500"}], "sent_prices": {}}
        return {"ok": [i.barcode for i in items], "errors": [], "sent_prices": {}}


def _setup(db, cost="500", status=PriceChangeStatus.approved, is_test=False, new_price=1000):
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A", name="Джинсы", cost_price=Decimal(cost)))
    db.add(Barcode(barcode="460", uid_1c="u1"))
    db.add(PlatformCatalogItem(account_id=account.id, external_id="123:9", barcode="460", article="OFR"))
    db.add(PriceRule(account_id=account.id, markup_percent=100, fixed_add=0, round_step=1, round_minus=0,
                     min_margin_percent=30, max_change_percent=20))
    change = PriceChange(uid_1c="u1", account_id=account.id, new_price=new_price, status=status,
                         is_test=is_test)
    db.add(change)
    db.commit()
    return account, change


def test_approved_price_is_sent_and_remembered(db):
    account, change = _setup(db)
    client = FakeClient()

    run_price_dispatch(db, {account.id: client}, [account])

    item = client.calls[0][0]
    assert (item.barcode, item.price, item.external_id, item.article) == ("460", 1000, "123:9", "OFR")
    db.refresh(change)
    assert change.status == PriceChangeStatus.sent
    assert db.query(ProductPrice).one().last_sent_price == 1000


def test_proposed_is_never_sent(db):
    account, change = _setup(db, status=PriceChangeStatus.proposed)
    client = FakeClient()
    run_price_dispatch(db, {account.id: client}, [account])
    assert client.calls == []


def test_test_records_are_never_sent(db):
    account, change = _setup(db, is_test=True)
    client = FakeClient()
    run_price_dispatch(db, {account.id: client}, [account])
    assert client.calls == []
    db.refresh(change)
    assert change.status == PriceChangeStatus.approved


def test_floor_rechecked_at_send_time(db):
    # подтвердили 1000 при себестоимости 500, а 1С прислала 900: пол теперь 1170
    account, change = _setup(db, cost="900")
    client = FakeClient()
    run_price_dispatch(db, {account.id: client}, [account])
    assert client.calls == []
    db.refresh(change)
    assert change.status == PriceChangeStatus.blocked and change.block_reason == "floor"


def test_failure_retries_then_errors(db):
    account, change = _setup(db)
    client = FakeClient(fail=True)

    run_price_dispatch(db, {account.id: client}, [account])
    db.refresh(change)
    assert change.status == PriceChangeStatus.approved and change.next_attempt_at is not None

    run_price_dispatch(db, {account.id: client}, [account])     # пауза не вышла
    assert len(client.calls) == 1

    for _ in range(MAX_ATTEMPTS):
        change.next_attempt_at = now_utc() - timedelta(seconds=1)
        db.commit()
        run_price_dispatch(db, {account.id: client}, [account])
    db.refresh(change)
    assert change.status == PriceChangeStatus.error
    assert db.query(ProductPrice).count() == 0


def test_newer_approved_supersedes_older(db):
    account, older = _setup(db)
    newer = PriceChange(uid_1c="u1", account_id=account.id, new_price=1100, status=PriceChangeStatus.approved)
    db.add(newer)
    db.commit()
    client = FakeClient()

    run_price_dispatch(db, {account.id: client}, [account])

    assert [i.price for i in client.calls[0]] == [1100]
    db.refresh(older)
    assert older.status == PriceChangeStatus.rejected


# ------------------------------------------------------------- клиенты

class _Resp:
    def __init__(self, data):
        self._data = data
        self.content = b"x"

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _Session:
    def __init__(self, reply):
        self.headers = {}
        self.calls = []
        self.reply = reply

    def post(self, url, **k):
        self.calls.append((url, k.get("json")))
        return _Resp(self.reply(k.get("json")))


def test_wb_sends_one_price_per_card_the_highest():
    s = _Session(lambda body: {"data": {"id": 1}, "error": False})
    client = WbClient(token="t", warehouse_id="w", session=s)
    res = client.push_prices([
        PricePushItem(barcode="b1", price=1000, external_id="123:1"),
        PricePushItem(barcode="b2", price=1100, external_id="123:2"),
        PricePushItem(barcode="b3", price=500, external_id=""),
    ])
    url, body = s.calls[0]
    assert "discounts-prices-api.wildberries.ru/api/v2/upload/task" in url
    assert body == {"data": [{"nmID": 123, "price": 1100}]}
    assert res["ok"] == ["b1", "b2"]
    assert res["sent_prices"] == {"b1": 1100, "b2": 1100}
    assert res["errors"][0]["items"] == ["b3"]


def test_wb_error_flag_is_failure():
    s = _Session(lambda body: {"error": True, "errorText": "bad"})
    res = WbClient(token="t", warehouse_id="w", session=s).push_prices(
        [PricePushItem(barcode="b1", price=1000, external_id="123:1")])
    assert res["ok"] == [] and res["errors"][0]["detail"] == "bad"


def test_ozon_sends_by_offer_id():
    s = _Session(lambda body: {"result": [{"offer_id": p["offer_id"], "updated": True} for p in body["prices"]]})
    res = OzonClient(client_id="c", api_key="k", session=s).push_prices(
        [PricePushItem(barcode="b1", price=1299, article="OFR-1")])
    url, body = s.calls[0]
    assert url.endswith("/v1/product/import/prices")
    assert body == {"prices": [{"offer_id": "OFR-1", "price": "1299", "currency_code": "RUB"}]}
    assert res["ok"] == ["b1"]


def test_kit_refuses_without_network():
    s = _Session(lambda body: {})
    res = KitClient(token="t", session=s).push_prices([PricePushItem(barcode="b1", price=1)])
    assert s.calls == [] and res["ok"] == [] and "Kit" in res["errors"][0]["detail"]

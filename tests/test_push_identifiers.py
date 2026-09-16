"""Идентификаторы товара при отправке остатка: у каждой площадки свой ключ
(WB — баркод/sku, Ozon — offer_id/артикул, Kit — variant_id/UUID варианта)."""
import requests

from app.workers.platform_clients.base import StockPushItem
from app.workers.platform_clients.wb import WbClient
from app.workers.platform_clients.ozon import OzonClient
from app.workers.platform_clients.kit import KitClient


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        # Для Ozon: эхо offer_id со статусом updated
        stocks = (self._body or {}).get("stocks", [])
        return {"result": [{"offer_id": s.get("offer_id"), "updated": True} for s in stocks]}


class _CaptureSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def _rec(self, method, url, kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        return _Resp(kwargs.get("json"))

    def put(self, url, **k):
        return self._rec("PUT", url, k)

    def post(self, url, **k):
        return self._rec("POST", url, k)

    def get(self, url, **k):
        return self._rec("GET", url, k)


def test_wb_push_uses_barcode_as_sku():
    s = _CaptureSession()
    client = WbClient(token="t", warehouse_id="wh", session=s)
    res = client.push_stock("WH1", [StockPushItem(barcode="460123", quantity=5, external_id="nm:1", article="ART")])
    _, url, body = s.calls[0]
    assert "/api/v3/stocks/WH1" in url
    assert body["stocks"][0] == {"sku": "460123", "amount": 5}
    assert res["ok"] == ["460123"]


def test_ozon_push_uses_article_as_offer_id_and_ok_in_barcodes():
    s = _CaptureSession()
    client = OzonClient(client_id="c", api_key="k", session=s)
    res = client.push_stock("WH1", [StockPushItem(barcode="460123", quantity=5, external_id="pid-1", article="OFR-1")])
    _, url, body = s.calls[0]
    assert "/v2/products/stocks" in url
    item = body["stocks"][0]
    assert item["offer_id"] == "OFR-1"        # артикул, НЕ баркод
    assert item["stock"] == 5
    assert item["warehouse_id"] == "WH1"
    assert res["ok"] == ["460123"]            # ok маппится обратно в баркод


def test_ozon_push_falls_back_to_barcode_without_article():
    s = _CaptureSession()
    client = OzonClient(client_id="c", api_key="k", session=s)
    client.push_stock("WH1", [StockPushItem(barcode="460123", quantity=1)])
    _, _, body = s.calls[0]
    assert body["stocks"][0]["offer_id"] == "460123"


def test_kit_push_uses_external_id_as_variant_id():
    s = _CaptureSession()
    client = KitClient(token="t", session=s)
    res = client.push_stock("WH1", [StockPushItem(barcode="460123", quantity=5, external_id="uuid-var-1", article="")])
    _, url, body = s.calls[0]
    assert "/v1/variants/stocks/bulk_update" in url
    item = body["items"][0]
    assert item["variant_id"] == "uuid-var-1"  # UUID варианта, НЕ баркод
    assert item["quantity"] == 5
    assert res["ok"] == ["460123"]

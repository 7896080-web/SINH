"""Клиент Kit — статусы заказов сверены с OpenAPI-спекой (skill yandex-kit-cabinet):
приём (WAIT_FOR_CONFIRMATION), подтверждение (переход в доставку), отмена
(CANCELLED/DELIVERY_CANCELLED/FULL_REFUND), частичный возврат (refused_count)."""
from app.workers.platform_clients.kit import KitClient


class _Resp:
    def __init__(self, data):
        self._d = data
        self.content = b"{}"

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class _Session:
    """Маршрутизирует по URL: /v1/variants/<id>, /v1/orders/<id>, /v1/orders."""
    def __init__(self, list_orders=None, order_by_id=None, variants=None):
        self.headers = {}
        self.list_orders = list_orders or []
        self.order_by_id = order_by_id or {}
        self.variants = variants or {}

    def get(self, url, params=None, timeout=None):
        if "/v1/variants/" in url:
            vid = url.rsplit("/", 1)[-1]
            return _Resp(self.variants.get(vid, {}))
        if "/v1/orders/" in url:
            oid = url.rsplit("/", 1)[-1]
            return _Resp(self.order_by_id.get(oid, {}))
        if "/v1/orders" in url:
            page = (params or {}).get("page", 1)
            return _Resp({"orders": self.list_orders if page == 1 else []})
        return _Resp({})

    def post(self, url, json=None, timeout=None):
        return _Resp({})


def _order(oid, status, item_id="i1", variant="v1", qty=2, refused=0):
    return {
        "id": oid, "status": status,
        "delivery_chunks": [{"id": 0, "items": [
            {"id": item_id, "product_variant_id": variant, "quantity": qty, "refused_count": refused},
        ]}],
    }


def test_kit_awaiting_resolves_variant_to_barcode():
    s = _Session(
        list_orders=[_order("o1", "WAIT_FOR_CONFIRMATION", variant="v1", qty=3)],
        variants={"v1": {"barcode": "460000000001"}},
    )
    client = KitClient(token="t", session=s)
    orders = client.get_orders_awaiting_confirmation()
    assert len(orders) == 1
    assert orders[0].barcode == "460000000001"
    assert orders[0].quantity == 3
    assert orders[0].order_id == "o1:0:i1"


def test_kit_confirmed_when_status_advanced_to_delivery():
    s = _Session(order_by_id={"o1": _order("o1", "SETUP_DELIVERY")})
    client = KitClient(token="t", session=s)
    confirmed = client.get_confirmed_orders(["o1:0:i1"])
    assert len(confirmed) == 1
    assert confirmed[0].order_id == "o1:0:i1"
    assert confirmed[0].is_cancellation is False


def test_kit_not_confirmed_while_awaiting():
    s = _Session(order_by_id={"o1": _order("o1", "WAIT_FOR_CONFIRMATION")})
    client = KitClient(token="t", session=s)
    assert client.get_confirmed_orders(["o1:0:i1"]) == []


def test_kit_no_auto_reverse():
    # Учитываем только НАШУ отмену. Kit не отдаёт инициатора отмены, а все статусы
    # отмен — пост-отгрузка (клиентский возврат/отказ), поэтому авто-реверс по Kit
    # отключён: get_cancelled_orders ничего не реверсит (возврат придёт из 1С).
    for st in ("CANCELLED", "DELIVERY_CANCELLED", "FULL_REFUND"):
        s = _Session(order_by_id={"o1": _order("o1", st)})
        client = KitClient(token="t", session=s)
        assert client.get_cancelled_orders(["o1:0:i1"]) == []


def test_kit_partial_refund_not_reversed():
    s = _Session(order_by_id={"o1": _order("o1", "PARTIAL_REFUND", refused=2)})
    client = KitClient(token="t", session=s)
    assert client.get_cancelled_orders(["o1:0:i1"]) == []

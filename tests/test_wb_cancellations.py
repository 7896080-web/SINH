"""WB get_cancelled_orders: реверсим ТОЛЬКО нашу отмену (supplierStatus=cancel).

Клиентские отмены/отказы (wbStatus canceled/canceled_by_client/declined_by_client)
приходят ПОСЛЕ отгрузки — товар уже ушёл, реверсить нельзя (возврат идёт через 1С).
"""

from app.workers.platform_clients.wb import WbClient


def _client_with_status(payload):
    c = WbClient(token="t", warehouse_id="wh")
    c._post = lambda path, body=None, **kw: payload  # без реального похода в сеть
    return c


def test_only_supplier_cancel_is_reversed():
    payload = {"orders": [
        {"id": 1, "supplierStatus": "cancel", "wbStatus": "waiting"},         # НАША отмена → да
        {"id": 2, "supplierStatus": "confirm", "wbStatus": "declined_by_client"},  # отказ клиента → нет
        {"id": 3, "supplierStatus": "complete", "wbStatus": "canceled_by_client"}, # отмена клиента → нет
        {"id": 4, "supplierStatus": "complete", "wbStatus": "canceled"},       # системная отмена → нет
        {"id": 5, "supplierStatus": "new", "wbStatus": "waiting"},             # живой → нет
        {"id": 6, "supplierStatus": "confirm", "wbStatus": "sold"},            # продан → нет
    ]}
    c = _client_with_status(payload)
    res = c.get_cancelled_orders(["1", "2", "3", "4", "5", "6"])
    assert [o.order_id for o in res] == ["1"]
    assert res[0].is_cancellation is True


def test_empty_input_no_call():
    c = WbClient(token="t", warehouse_id="wh")
    # _post не должен вызываться на пустом списке
    def _boom(*a, **k):
        raise AssertionError("network call on empty input")
    c._post = _boom
    assert c.get_cancelled_orders([]) == []


def test_no_supplier_cancel_returns_empty():
    payload = {"orders": [
        {"id": 10, "supplierStatus": "complete", "wbStatus": "declined_by_client"},
        {"id": 11, "supplierStatus": "confirm", "wbStatus": "canceled_by_client"},
    ]}
    c = _client_with_status(payload)
    assert c.get_cancelled_orders(["10", "11"]) == []

"""Ozon get_cancelled_orders: реверсим ТОЛЬКО нашу отмену.

Наша = cancellation_initiator == "Seller" и НЕ после отгрузки (cancelled_after_ship).
Отмены клиента/Ozon/системы и любые после отгрузки — это возврат, не реверсим.
"""

from app.workers.platform_clients.ozon import OzonClient


def _client_with_postings(postings):
    c = OzonClient(client_id="cid", api_key="key")
    c._post = lambda path, body=None: {"result": {"postings": postings}}
    return c


def _posting(number, initiator, after_ship=False):
    return {"posting_number": number,
            "cancellation": {"cancellation_initiator": initiator,
                             "cancelled_after_ship": after_ship}}


def test_only_seller_before_ship_is_reversed():
    postings = [
        _posting("P-seller", "Seller"),                 # наша, до отгрузки → да
        _posting("P-seller-shipped", "Seller", True),   # наша, но после отгрузки → нет
        _posting("P-client", "Client"),                 # клиент → нет
        _posting("P-customer", "Customer"),             # клиент → нет
        _posting("P-ozon", "Ozon"),                     # Ozon → нет
        _posting("P-system", "System"),                 # система → нет
    ]
    c = _client_with_postings(postings)
    ids = ["P-seller:0", "P-seller-shipped:0", "P-client:0", "P-customer:0", "P-ozon:0", "P-system:0"]
    res = c.get_cancelled_orders(ids)
    assert [o.order_id for o in res] == ["P-seller:0"]
    assert res[0].is_cancellation is True


def test_empty_input_no_call():
    c = OzonClient(client_id="cid", api_key="key")
    def _boom(*a, **k):
        raise AssertionError("network call on empty input")
    c._post = _boom
    assert c.get_cancelled_orders([]) == []


def test_missing_cancellation_object_not_reversed():
    postings = [{"posting_number": "P1"}, {"posting_number": "P2", "cancellation": None}]
    c = _client_with_postings(postings)
    assert c.get_cancelled_orders(["P1:0", "P2:0"]) == []

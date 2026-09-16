import requests

from app.workers.platform_clients.wb import WbClient
from app.workers.platform_clients.ozon import OzonClient
from app.workers.platform_clients.kit import KitClient


class FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeSession:
    """Подменяет requests.Session — возвращает заранее заданный ответ,
    без единого реального похода в сеть."""

    def __init__(self, response: FakeResponse):
        self._response = response
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._response

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return self._response

    def update(self, *a, **kw):
        pass  # для headers.update(...) в конструкторах клиентов


def test_wb_test_connection_success():
    session = FakeSession(FakeResponse(200))
    client = WbClient(token="t", warehouse_id="w", session=session)
    client.session.headers = {}

    ok, message = client.test_connection()

    assert ok is True
    assert "ping" in session.calls[0][1]


def test_wb_test_connection_401():
    session = FakeSession(FakeResponse(401))
    client = WbClient(token="bad", warehouse_id="w", session=session)

    ok, message = client.test_connection()

    assert ok is False
    assert "401" in message


def test_ozon_test_connection_success():
    session = FakeSession(FakeResponse(200, {"result": {"items": []}}))
    client = OzonClient(client_id="c", api_key="k", session=session)

    ok, message = client.test_connection()

    assert ok is True
    assert session.calls[0][0] == "POST"
    # актуальный метод — /v3/product/list (/v2 удалён Ozon, отдаёт 404)
    assert "/v3/product/list" in session.calls[0][1]


def test_ozon_test_connection_403():
    session = FakeSession(FakeResponse(403))
    client = OzonClient(client_id="c", api_key="bad", session=session)

    ok, message = client.test_connection()

    assert ok is False
    assert "403" in message


def test_kit_test_connection_success():
    session = FakeSession(FakeResponse(200, {"warehouses": []}))
    client = KitClient(token="t", session=session)

    ok, message = client.test_connection()

    assert ok is True
    # Kit требует обязательный параметр status=ACTIVE (иначе 400 VALIDATION_ERROR)
    assert "/v1/warehouses" in session.calls[0][1]
    assert session.calls[0][2].get("params", {}).get("status") == "ACTIVE"


def test_kit_test_connection_401():
    session = FakeSession(FakeResponse(401))
    client = KitClient(token="bad", session=session)

    ok, message = client.test_connection()

    assert ok is False
    assert "401" in message

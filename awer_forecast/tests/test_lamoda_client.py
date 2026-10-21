"""Клиент Lamoda v2: токен, пагинация, повторы, ошибки, копейки."""
import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from forecast.integrations.lamoda.client import LamodaApiError, LamodaClient, LamodaFilterIgnored
from forecast.integrations.lamoda.money import MoneyFormatError, money_from_api, money_to_api
from factories import FakeLamoda, order


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(fake: FakeLamoda, clock=None, sleeps=None, **kw) -> LamodaClient:
    return LamodaClient("id", "secret", fake.seller_id, http=fake.client(), clock=clock or Clock(),
                        sleep=(sleeps.append if sleeps is not None else (lambda s: None)), **kw)


# ---------------- деньги ----------------

@pytest.mark.parametrize("value, expected", [
    ({"amount": 199900, "currency": "RUB"}, Decimal("1999.00")),
    ({"amount": 1, "currency": "RUB"}, Decimal("0.01")),
    ({"amount": 0, "currency": "RUB"}, Decimal("0.00")),
    ({"amount": -50050, "currency": "RUB"}, Decimal("-500.50")),
    ({"amount": "123456", "currency": "RUB"}, Decimal("1234.56")),
    (99, Decimal("0.99")),
    (None, None),
    ({"amount": None, "currency": "RUB"}, None),
])
def test_money_minor_units_to_rubles(value, expected):
    assert money_from_api(value) == expected


def test_fractional_amount_is_rejected_not_divided():
    with pytest.raises(MoneyFormatError):
        money_from_api({"amount": 1999.5, "currency": "RUB"})


def test_money_round_trip():
    assert money_to_api(Decimal("1999.99")) == {"amount": 199999, "currency": "RUB"}


# ---------------- токен ----------------

def test_token_is_reused_until_close_to_expiry_then_refreshed():
    fake, clock = FakeLamoda(expires_in=900), Clock()
    client = make(fake, clock)
    client.request("GET", "/v2/fbo/stocks")
    clock.now += 800                              # до истечения 100 с — больше запаса 45 с
    client.request("GET", "/v2/fbo/stocks")
    assert fake.tokens_issued == 1
    clock.now += 70                               # осталось 30 с — обновить заранее
    client.request("GET", "/v2/fbo/stocks")
    assert fake.tokens_issued == 2
    auth_body = [c for c in fake.calls if c[1] == "/v2/auth-token"]
    assert len(auth_body) == 2


def test_401_reissues_token_once_and_retries():
    fake = FakeLamoda()
    client = make(fake)
    client.request("GET", "/v2/fbo/stocks")
    fake.valid_tokens.clear()                     # токен отозван на стороне Lamoda
    client.request("GET", "/v2/fbo/stocks")
    assert fake.tokens_issued == 2


def test_persistent_401_raises_after_single_retry():
    fake = FakeLamoda()
    client = make(fake)
    client.request("GET", "/v2/fbo/stocks")
    fake.fail_next = [httpx.Response(401, json={"error": {"code": "UNAUTHORIZED", "message": "no"}})] * 1
    fake.fail_next.append(httpx.Response(201, json={"access_token": "x", "expires_in": 900}))
    fake.fail_next.append(httpx.Response(401, json={"error": {"code": "UNAUTHORIZED", "message": "no"}}))
    with pytest.raises(LamodaApiError) as exc:
        client.request("GET", "/v2/fbo/stocks")
    assert exc.value.http_status == 401


def test_bad_credentials_give_readable_error():
    fake = FakeLamoda()
    client = LamodaClient("id", "wrong", fake.seller_id, http=fake.client())
    with pytest.raises(LamodaApiError, match="invalid_client"):
        client.request("GET", "/v2/fbo/stocks")


def test_auth_body_is_snake_case_and_seller_id_in_query():
    fake = FakeLamoda()
    seen = {}

    def spy(request):
        if request.url.path.endswith("/auth-token"):
            seen["body"] = request.content
        else:
            seen["query"] = dict(request.url.params)
        return fake.handle(request)

    client = LamodaClient("id", "secret", fake.seller_id, http=httpx.Client(transport=httpx.MockTransport(spy)))
    client.request("GET", "/v2/fbo/stocks")
    assert json.loads(seen["body"]) == {"grant_type": "client_credentials", "client_id": "id",
                                        "client_secret": "secret"}
    assert seen["query"]["sellerId"] == fake.seller_id


# ---------------- повторы и ошибки ----------------

def test_429_and_503_are_retried_with_retry_after():
    fake, sleeps = FakeLamoda(), []
    client = make(fake, sleeps=sleeps)
    client.request("GET", "/v2/fbo/stocks")
    fake.fail_next = [httpx.Response(429, headers={"Retry-After": "7"},
                                     json={"error": {"code": "TOO_MANY_REQUESTS", "message": "slow down"}}),
                      httpx.Response(503, json={"error": {"code": "SERVICE_UNAVAILABLE", "message": "x"}})]
    data = client.request("GET", "/v2/fbo/stocks")
    assert "stocks" in data
    assert sleeps == [7.0, 2.0]                   # Retry-After, затем 2^1


def test_retries_are_bounded():
    fake = FakeLamoda()
    client = make(fake, max_retries=2)
    client.request("GET", "/v2/fbo/stocks")
    fake.fail_next = [httpx.Response(503, json={"error": {"code": "SERVICE_UNAVAILABLE", "message": "down"}})] * 5
    with pytest.raises(LamodaApiError) as exc:
        client.request("GET", "/v2/fbo/stocks")
    assert exc.value.code == "SERVICE_UNAVAILABLE"


def test_unified_error_format_and_403_hint():
    fake = FakeLamoda()
    client = LamodaClient("id", "secret", "wrong-seller", http=fake.client())
    with pytest.raises(LamodaApiError) as exc:
        client.request("GET", "/v2/fbo/stocks")
    assert exc.value.http_status == 403 and exc.value.code == "FORBIDDEN"
    assert "sellerId" in str(exc.value)


def test_error_details_are_kept():
    fake = FakeLamoda()
    client = make(fake)
    client.request("GET", "/v2/fbo/stocks")
    fake.fail_next = [httpx.Response(400, json={"error": {"code": "VALIDATION_FAILED", "message": "bad",
                                                          "details": [{"field": "limit", "issue": "> 1000"}]}})]
    with pytest.raises(LamodaApiError) as exc:
        client.request("GET", "/v2/fbo/stocks")
    assert exc.value.details[0]["field"] == "limit" and "limit: > 1000" in str(exc.value)


def test_network_errors_are_retried():
    fake = FakeLamoda()
    state = {"fails": 2}

    def flaky(request):
        if state["fails"]:
            state["fails"] -= 1
            raise httpx.ConnectError("boom", request=request)
        return fake.handle(request)

    client = LamodaClient("id", "secret", fake.seller_id, http=httpx.Client(transport=httpx.MockTransport(flaky)),
                          sleep=lambda s: None)
    assert "stocks" in client.request("GET", "/v2/fbo/stocks")


# ---------------- пагинация ----------------

def test_pagination_follows_meta_total_pages():
    fake = FakeLamoda()
    fake.stocks = [{"sku": f"S{i}", "warehouseCode": "BYKOVO", "quantity": i} for i in range(2500)]
    client = make(fake)
    rows = list(client.iter_fbo_stocks())
    assert len(rows) == 2500
    pages = [c[2]["page"] for c in fake.calls if c[1] == "/v2/fbo/stocks"]
    assert pages == ["1", "2", "3"]
    assert all(c[2]["limit"] == "1000" and c[2]["withZeroQuantity"] == "true"
               for c in fake.calls if c[1] == "/v2/fbo/stocks")


def test_nomenclatures_use_page_size_25_and_country():
    fake = FakeLamoda()
    fake.nomenclatures = [{"sku": f"N{i}", "barcode": str(i)} for i in range(60)]
    client = make(fake)
    assert len(list(client.iter_nomenclatures("RU"))) == 60
    calls = [c for c in fake.calls if c[1] == "/v2/nomenclatures"]
    assert len(calls) == 3 and all(c[2]["limit"] == "25" and c[2]["country"] == "RU" for c in calls)


def test_empty_list_stops_pagination():
    fake = FakeLamoda()
    client = make(fake)
    assert list(client.iter_fbo_shipments()) == []


def test_missing_list_key_is_an_error():
    fake = FakeLamoda()
    client = make(fake)
    client.request("GET", "/v2/fbo/stocks")
    fake.fail_next = [httpx.Response(200, json={"data": [], "meta": {"totalPages": 1}})]
    with pytest.raises(LamodaApiError, match="stocks"):
        list(client.iter_fbo_stocks())


def test_orders_filter_ignored_is_detected():
    """API молча игнорирует незнакомые параметры: если в ответе заказ старше updatedAtFrom,
    фильтр не применился — останавливаемся, а не тянем всю историю."""
    fake = FakeLamoda()
    fake.ignore_updated_filter = True
    fake.orders = [order("1", "2026-01-01T00:00:00.000Z", []), order("2", "2026-10-20T00:00:00.000Z", [])]
    client = make(fake)
    with pytest.raises(LamodaFilterIgnored):
        list(client.iter_orders(datetime(2026, 10, 1, tzinfo=timezone.utc)))


def test_orders_request_uses_rfc3339_utc_filters():
    fake = FakeLamoda()
    client = make(fake)
    list(client.iter_orders(datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc),
                            datetime(2026, 10, 2, tzinfo=timezone.utc)))
    query = [c for c in fake.calls if c[1] == "/v2/orders"][0][2]
    assert query["updatedAtFrom"] == "2026-10-01T09:30:00.000Z"
    assert query["updatedAtTo"] == "2026-10-02T00:00:00.000Z"
    assert query["sort"] == "updatedAt"

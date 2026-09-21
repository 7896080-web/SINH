"""Лента заказов начинается с начала МЕСТНЫХ суток, а не UTC-шных.

Базовая дата расчёта — календарное число, которое называет человек и на которое
отвечает 1С: `ВыгрузитьОстаткиЦСНаДату` берёт остаток на `КонецДня(ДатаСреза)`
по часам своей машины. Дальше расчёт обязан поднять продажи «с той же даты».

Все три клиента превращали эту дату в момент одинаково неверно — `datetime(год,
месяц, число, tzinfo=timezone.utc)`, то есть 03:00 по Москве. Заказы первых трёх
часов базовых суток оставались за границей запроса. Площадка при этом не
ошибается и не молчит — она честно отвечает без них, `problems` остаётся пустым,
`catch_up_product` ставит «актуализирован», ворота открываются, и наружу уходит
остаток, завышенный ровно на эти продажи. Это оверселл, и ровно тот же по
механике, что 18.09: лента ответила не всё, а выглядело как «продаж не было».

У Kit та же дыра приходила с другой стороны — сравнением дат: `created_at`
заказа в 01:00 МСК даёт UTC-шное ЧИСЛО предыдущего дня, и такой заказ
отбрасывался как «раньше базовой даты».

Второе следствие того же — дата перемещения в 1С. `order_date` уезжает
`movement_date`, а 1С живёт по местным часам: у заказа до трёх ночи документ
вставал вчерашним числом, при расчёте — раньше базовой даты, на которую снят
остаток.

Тесты гоняют часовой пояс Москвы явно: на CI машина стоит в UTC, где местная
полночь и UTC-шная совпадают, и разницы не видно ни одному поведенческому тесту.
"""
import os
import time
from datetime import date, datetime, timezone

import pytest

from app.workers.platform_clients.kit import KitClient
from app.workers.platform_clients.ozon import OzonClient
from app.workers.platform_clients.wb import WbClient

BASE_DAY = date(2026, 9, 10)
# Начало местных суток 10.09 в Москве: 21:00 UTC девятого.
LOCAL_MIDNIGHT = datetime(2026, 9, 9, 21, 0, tzinfo=timezone.utc)
# Заказ в 01:30 МСК десятого — внутри базовых суток, но раньше UTC-полуночи.
EARLY = "2026-09-09T22:30:00Z"
# Заказ в 23:00 МСК ДЕВЯТОГО — за пределами базовых суток, его брать нельзя.
BEFORE = "2026-09-09T20:00:00Z"


@pytest.fixture
def moscow():
    """Часы машины по Москве — как на боевом сервере."""
    was = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Moscow"
    time.tzset()
    yield
    if was is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = was
    time.tzset()


# ------------------------------------------------------------------ WB

def _wb(orders):
    asked = []

    def fake_get(path, params=None, **kw):
        asked.append(params.get("dateFrom"))
        if len(asked) > 1:                 # второе окно — пусто, хватит одного
            return {"orders": [], "next": 0}
        return {"orders": orders, "next": 0}

    c = WbClient(token="t", warehouse_id="wh")
    c._get = fake_get
    return c, asked


def test_wb_asks_from_the_start_of_the_local_day(moscow):
    c, asked = _wb([])

    c.get_orders_since(BASE_DAY)

    assert asked[0] == int(LOCAL_MIDNIGHT.timestamp())


def test_wb_order_date_is_the_local_one(moscow):
    c, _ = _wb([{"id": 1, "skus": ["4600000000001"], "createdAt": EARLY}])

    orders = c.get_orders_since(BASE_DAY)

    assert orders[0].order_date == BASE_DAY


# ---------------------------------------------------------------- Ozon

def _ozon():
    asked = []

    def fake_post(path, body=None, **kw):
        asked.append(body)
        if len(asked) > 1:
            return {"result": {"postings": []}}
        return {"result": {"postings": [{
            "posting_number": "p-1", "status": "awaiting_deliver", "in_process_at": EARLY,
            "products": [{"barcode": "4600000000001", "quantity": 1, "sku": 7}],
        }]}}

    c = OzonClient(client_id="c", api_key="k")
    c._post = fake_post
    return c, asked


def test_ozon_asks_since_the_start_of_the_local_day(moscow):
    c, asked = _ozon()

    c.get_orders_since(BASE_DAY)

    assert asked[0]["filter"]["since"] == "2026-09-09T21:00:00.000Z"


def test_ozon_order_date_is_the_local_one(moscow):
    c, _ = _ozon()

    orders = c.get_orders_since(BASE_DAY)

    assert orders[0].order_date == BASE_DAY


# ----------------------------------------------------------------- Kit

class _Resp:
    def __init__(self, data):
        self._d, self.content = data, b"{}"

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class _Session:
    def __init__(self, orders):
        self.headers = {}
        self.orders = orders

    def get(self, url, params=None, timeout=None):
        if "/v1/variants/" in url:
            return _Resp({"barcode": "4600000000001"})
        if "/v1/orders" in url:
            page = (params or {}).get("page", 1)
            return _Resp({"orders": self.orders if page == 1 else [], "total_count": len(self.orders)})
        return _Resp({})

    def post(self, url, json=None, timeout=None):
        return _Resp({})


def _kit_order(created):
    return {"id": "o1", "status": "WAIT_FOR_CONFIRMATION", "created_at": created,
            "delivery_chunks": [{"id": 0, "items": [
                {"id": "i1", "product_variant_id": "v1", "quantity": 1, "refused_count": 0}]}]}


def test_kit_keeps_an_order_from_the_first_hours_of_the_base_day(moscow):
    c = KitClient(token="t", session=_Session([_kit_order(EARLY)]))

    orders = c.get_orders_since(BASE_DAY)

    assert [o.order_id for o in orders] == ["o1:0:i1"]
    assert orders[0].order_date == BASE_DAY


def test_kit_still_drops_an_order_from_the_previous_local_day(moscow):
    """Граница не размазана: вечер предыдущего дня по-прежнему за ней."""
    c = KitClient(token="t", session=_Session([_kit_order(BEFORE)]))

    assert c.get_orders_since(BASE_DAY) == []

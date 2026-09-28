"""Отказы отправки остатков WB: повтор или нет — вот единственный вопрос.

Спека `/api/v3/stocks/{warehouseId}` документирует для 409 и 406 несколько
кодов, и различать их обязательно, потому что они требуют РАЗНОГО.

Одни называют виновную позицию поимённо и при повторе ответят тем же: склад
кабинета не подходит для типа груза, категория недоступна для этого типа
доставки, количество вне предела. Такую позицию надо вынуть и закрыть сразу, а
остальную пачку дослать — иначе она жжёт пять попыток и уносит с собой девяносто
девять здоровых позиций, а в отчёте это выглядит как «рассылка не доехала», то
есть «чините связь», хотя чинить надо настройки кабинета.

Другие повтор переживут: склад обновляется, бан поставщика, неназванные позиции.
Метить их терминальными нельзя — остаток по ним не уехал бы уже никогда.
Особенно важен бан: пока он не снят, остатки по кабинету не обновляются вовсе, и
прятать это за «не отправлено за 5 попыток» значит искать причину не там.
"""
import requests

from app.workers.platform_clients.base import StockPushItem
from app.workers.platform_clients.wb import WbClient


class _Resp:
    def __init__(self, status_code=204, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("нет тела")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)


class _Session:
    """Отвечает по очереди: первый ответ — отказ, дальше успех."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.bodies = []
        self.headers = {}

    def put(self, url, **kwargs):
        self.bodies.append(kwargs.get("json"))
        return self._responses.pop(0) if self._responses else _Resp(204)


def _client(*responses):
    return WbClient("token", "wh-1", session=_Session(*responses))


def _items():
    return [StockPushItem(barcode="b-bad", quantity=5),
            StockPushItem(barcode="b-good", quantity=7)]


def _refusal(code, skus=None):
    entry = {"code": code, "message": "..."}
    if skus is not None:
        entry["data"] = [{"sku": s, "amount": 1} for s in skus]
    return _Resp(409, text="x", payload=[entry])


# ----------------------------------------- названа поимённо → вынуть и дослать

def test_a_warehouse_restriction_drops_only_the_named_item():
    client = _client(_refusal("CargoWarehouseRestrictionMGT", ["b-bad"]))

    result = client.push_stock("wh-1", _items())
    ok, errors = result.get("ok", []), result.get("errors", [])

    assert ok == ["b-good"], "здоровая позиция обязана уехать"
    assert [e["sku"] for e in errors] == ["b-bad"]
    assert errors[0]["terminal"] is True
    assert "типа груза" in errors[0]["detail"]


def test_a_delivery_restriction_is_terminal_too():
    client = _client(_refusal("DeliveryTypeRestriction", ["b-bad"]))

    result = client.push_stock("wh-1", _items())
    ok, errors = result.get("ok", []), result.get("errors", [])

    assert ok == ["b-good"]
    assert errors[0]["terminal"] is True


def test_an_amount_over_the_limit_is_terminal():
    """Повтор уйдёт с тем же числом и получит тот же ответ."""
    client = _client(_refusal("UploadDataLimit", ["b-bad"]))

    result = client.push_stock("wh-1", _items())
    ok, errors = result.get("ok", []), result.get("errors", [])

    assert ok == ["b-good"]
    assert errors[0]["terminal"] is True


# ----------------------------------------- повтор переживут → не терминально

def test_a_supplier_ban_is_not_terminal_and_says_so():
    """Бан снимают, поэтому позиция не закрывается. Но причина обязана быть
    названа: связь тут ни при чём, и по ВСЕМУ кабинету остаток не уедет."""
    client = _client(_Resp(406, text="x", payload=[{"code": "StatusNotAcceptable",
                                                   "message": "..."}]))

    result = client.push_stock("wh-1", _items())
    ok, errors = result.get("ok", []), result.get("errors", [])

    assert ok == []
    assert all("terminal" not in e for e in errors)
    assert "бан поставщика" in errors[0]["detail"]


def test_a_processing_store_is_not_terminal():
    """Спека прямо говорит «повторите через несколько секунд»."""
    client = _client(_refusal("StoreIsProcessing"))

    result = client.push_stock("wh-1", _items())
    ok, errors = result.get("ok", []), result.get("errors", [])

    assert ok == []
    assert all("terminal" not in e for e in errors)
    assert "обновляется" in errors[0]["detail"]


def test_an_unnamed_conflict_does_not_bury_the_batch():
    """`ProductPropertyConflict` виновных не называет. Выкинуть всю пачку значит
    похоронить здоровые позиции из-за одной неизвестной."""
    client = _client(_refusal("ProductPropertyConflict"))

    result = client.push_stock("wh-1", _items())
    ok, errors = result.get("ok", []), result.get("errors", [])

    assert all("terminal" not in e for e in errors)
    assert "схеме поставки" in errors[0]["detail"]


# ----------------------------------------- прежнее поведение не задето

def test_not_found_still_works():
    client = _client(_refusal("NotFound", ["b-bad"]))

    result = client.push_stock("wh-1", _items())
    ok, errors = result.get("ok", []), result.get("errors", [])

    assert ok == ["b-good"]
    assert errors[0]["terminal"] is True
    assert "не знает этот sku" in errors[0]["detail"]


def test_an_unknown_code_is_passed_through_as_before():
    """Разбирать за площадку то, чего не понимаем, мы не беремся."""
    client = _client(_refusal("SomethingBrandNew", ["b-bad"]))

    result = client.push_stock("wh-1", _items())
    ok, errors = result.get("ok", []), result.get("errors", [])

    assert ok == []
    assert all("terminal" not in e for e in errors)

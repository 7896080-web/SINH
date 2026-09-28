"""Kit: баркод строки заказа берётся из своей базы, а неудача не выдаёт себя за ноль.

Разбор 18.09. Расчёт по товару отчитался «заказов с КИТ — 0», товар получил
«актуализирован», и на этом основании можно было включить трансляцию. Проба
живого API показала, почему нулю верить нельзя.

`get_orders_since` спрашивал баркод отдельным `GET /v1/variants/{id}` на КАЖДУЮ
строку КАЖДОГО заказа: на одной странице их больше сотни, страниц до двухсот, и
весь проход повторялся заново для каждого товара. Kit отвечает на такое 429. А
429, переживший все повторы, попадал в `except requests.HTTPError: return None`,
и строка заказа исчезала: ни исключения наверх, ни записи в проблемы. «Продаж не
было» и «мы не увидели продажи» выглядели одинаково.

Чинится с двух сторон: соответствие variant_id → баркод у нас уже лежит в
снимке каталога кабинета, и спрашивать площадку обычно не нужно вовсе; а если
спросить всё-таки пришлось и не вышло — это считается потерей, а не нулём.
"""
import pytest
import requests

from app.workers.platform_clients.kit import KitClient
from tests.test_kit_orders import _Resp, _Session, _order


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Паузы между повторами здесь проверять нечего, а ждать их — 6 секунд на тест."""
    monkeypatch.setattr("app.workers.http_retry.time.sleep", lambda _s: None)


class _FailingVariants(_Session):
    """Площадка отдаёт заказы, но на запрос варианта отвечает 429."""

    def get(self, url, params=None, timeout=None):
        if "/v1/variants/" in url:
            resp = _Resp({})
            err = requests.HTTPError("429")
            err.response = type("R", (), {"status_code": 429, "headers": {}})()
            def boom():
                raise err
            resp.raise_for_status = boom
            return resp
        return super().get(url, params=params, timeout=timeout)


def test_barcode_comes_from_our_own_catalogue_without_asking_the_platform():
    """Главная причина 429: тысячи запросов за тем, что у нас уже есть."""
    s = _Session(list_orders=[_order("o1", "NEW", variant="v1", qty=2)], variants={})
    asked = []
    original = s.get

    def counting_get(url, params=None, timeout=None):
        if "/v1/variants/" in url:
            asked.append(url)
        return original(url, params=params, timeout=timeout)

    s.get = counting_get
    client = KitClient(token="t", session=s,
                       variant_map_loader=lambda: {"v1": "460000000001"})

    orders = client.get_orders_since(__import__("datetime").date(2026, 8, 7))

    assert [o.barcode for o in orders] == ["460000000001"]
    assert asked == [], "баркод был в своей базе — ходить на площадку незачем"
    assert client.last_unresolved == 0


def test_the_platform_is_asked_only_for_variants_we_do_not_know():
    s = _Session(list_orders=[_order("o1", "NEW", variant="v2", qty=1)],
                 variants={"v2": {"barcode": "460000000002"}})
    client = KitClient(token="t", session=s,
                       variant_map_loader=lambda: {"v1": "460000000001"})

    orders = client.get_orders_since(__import__("datetime").date(2026, 8, 7))

    assert [o.barcode for o in orders] == ["460000000002"]
    assert client.last_unresolved == 0


def test_a_rate_limited_line_is_counted_as_lost_not_as_absent():
    """То самое место: заказ есть, но опознать его не удалось."""
    s = _FailingVariants(list_orders=[_order("o1", "NEW", variant="v1", qty=1)])
    client = KitClient(token="t", session=s)

    orders = client.get_orders_since(__import__("datetime").date(2026, 8, 7))

    assert orders == []
    assert client.last_unresolved == 1, "потеря обязана быть видна вызывающему"


def test_a_variant_without_a_barcode_is_not_a_loss():
    """Ответ получен и он пустой — такой товар просто не наш. Считать это потерей
    значило бы навсегда запретить «актуализирован» всем, у кого в кабинете есть
    хоть один вариант без баркода."""
    s = _Session(list_orders=[_order("o1", "NEW", variant="v1", qty=1)],
                 variants={"v1": {}})
    client = KitClient(token="t", session=s)

    orders = client.get_orders_since(__import__("datetime").date(2026, 8, 7))

    assert orders == []
    assert client.last_unresolved == 0


def test_the_counter_starts_from_zero_on_every_call():
    s = _FailingVariants(list_orders=[_order("o1", "NEW", variant="v1", qty=1)])
    client = KitClient(token="t", session=s)

    client.get_orders_since(__import__("datetime").date(2026, 8, 7))
    client.get_orders_since(__import__("datetime").date(2026, 8, 7))

    assert client.last_unresolved == 1, "иначе потери прошлого товара припишутся следующему"


def test_one_failing_variant_is_asked_once_per_call():
    """Долбить площадку, которая уже ответила 429, — верный способ увязнуть."""
    s = _FailingVariants(list_orders=[
        _order("o1", "NEW", item_id="i1", variant="v1", qty=1),
        _order("o2", "NEW", item_id="i2", variant="v1", qty=1),
    ])
    calls = []
    original = s.get

    def counting_get(url, params=None, timeout=None):
        if "/v1/variants/" in url:
            calls.append(url)
        return original(url, params=params, timeout=timeout)

    s.get = counting_get
    client = KitClient(token="t", session=s)

    client.get_orders_since(__import__("datetime").date(2026, 8, 7))

    # Три попытки with_retry на ОДИН вариант, второй заказ берётся из кэша.
    assert len(calls) == 3
    assert client.last_unresolved == 2, "выброшены обе строки, обе должны считаться"


def test_awaiting_confirmation_uses_the_same_resolution():
    """Живой опрос ходит тем же путём — экономия и учёт потерь нужны и там."""
    s = _Session(list_orders=[_order("o1", "WAIT_FOR_CONFIRMATION", variant="v1", qty=3)],
                 variants={})
    client = KitClient(token="t", session=s,
                       variant_map_loader=lambda: {"v1": "460000000001"})

    orders = client.get_orders_awaiting_confirmation()

    assert [o.barcode for o in orders] == ["460000000001"]


def test_a_broken_local_map_falls_back_to_the_platform():
    """Своя база недоступна — это не повод терять заказы."""
    s = _Session(list_orders=[_order("o1", "NEW", variant="v1", qty=1)],
                 variants={"v1": {"barcode": "460000000001"}})

    def boom():
        raise RuntimeError("база недоступна")

    client = KitClient(token="t", session=s, variant_map_loader=boom)

    orders = client.get_orders_since(__import__("datetime").date(2026, 8, 7))

    assert [o.barcode for o in orders] == ["460000000001"]

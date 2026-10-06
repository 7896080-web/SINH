"""Пачка запросов подряд упирается в лимит площадки — значит нужна ПАУЗА.

06.10 на бою: 02:29:04 предохранитель погасил ТРИ кабинета WB одной секундой
(`Read timed out (read timeout=30)` по `/api/v3/orders/new`), кабинеты включили,
а в 02:57:38–02:59:03 в логе восемь строк `429 от площадки, попытка 1/3`. То
есть площадка не падала — мы выбрали её лимит.

Спека WB (`/api/v3/orders/new`) называет его дословно: «300 запросов в минуту на
аккаунт продавца, интервал 200 мс, всплеск 20 запросов. Один запрос с кодами
ответов 4XX учитывается как 10 запросов». Последнее предложение и делает отказ
спиралью: 429 -> три повтора `with_retry` = тридцать «запросов» -> лимит глубже
-> площадка перестаёт отвечать вовсе -> таймаут -> предохранитель.

Реактивного `with_retry` недостаточно, и у Kit это записано тем же словом: он
ловит 429 «медленно и с риском не добрать часть данных». Здесь цена выше — не
добрать часть ленты заказов значит не провести продажи и оставить остаток
завышенным, то есть отправить наружу число больше того, что лежит на складе.

Проверяем СЛЕДСТВИЕ, а не наличие константы: у каждого сетевого вызова обязана
быть своя пауза не короче интервала из спеки. Константа на месте, а `sleep`
убран из одного из пяти путей — и правка молча перестаёт работать ровно там.
"""
import pytest
import requests

from app.workers.platform_clients import kit as kit_mod
from app.workers.platform_clients import wb as wb_mod
from app.workers.platform_clients.base import StockPushItem
from app.workers.platform_clients.kit import KitClient
from app.workers.platform_clients.wb import WbClient

# Интервал из спеки WB: 300 запросов в минуту — это 200 мс на запрос. Пауза
# короче него лимит не соблюдает, то есть дросселя как бы и нет.
WB_SPEC_INTERVAL = 0.2


class _Resp:
    """Ответ площадки ровно в том объёме, в каком его читают клиенты."""

    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status
        self.text = ""
        self.content = b"{}"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self._payload


class _CountingSession:
    """Считает сетевые вызовы. Заголовки клиент ставит сам — пусть ставит."""

    def __init__(self, payload=None):
        self.headers = {}
        self.calls = []
        self._payload = payload or {}

    def _hit(self, verb, url):
        self.calls.append((verb, url))
        return _Resp(self._payload)

    def get(self, url, **kw):
        return self._hit("get", url)

    def post(self, url, **kw):
        return self._hit("post", url)

    def put(self, url, **kw):
        return self._hit("put", url)

    def patch(self, url, **kw):
        return self._hit("patch", url)


@pytest.fixture()
def pauses(monkeypatch):
    """Перехватывает паузы ОБОИХ клиентов и возвращает список их длительностей.

    Подменяем именно `time.sleep` в модуле клиента: настоящая пауза на боевых
    объёмах (двести страниц каталога) растянула бы набор на минуты, а предмет
    проверки — что пауза запрошена перед каждым запросом, а не что процесс
    действительно стоял.
    """
    seen: list[float] = []

    def fake_sleep(seconds):
        seen.append(seconds)

    monkeypatch.setattr(wb_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(kit_mod.time, "sleep", fake_sleep)
    return seen


def _assert_throttled(session, seen, minimum=WB_SPEC_INTERVAL):
    assert session.calls, "тест ничего не спросил у площадки — проверять нечего"
    assert len(seen) >= len(session.calls), (
        f"запросов {len(session.calls)}, пауз {len(seen)} — "
        "значит часть запросов уходит вплотную, пачкой")
    assert min(seen) >= minimum, (
        f"самая короткая пауза {min(seen)} с короче интервала площадки "
        f"{minimum} с — лимит это не соблюдает")


# ---------------------------------------------------------------- WB

def test_the_orders_feed_pauses_between_requests(pauses):
    """Лента заказов — самый длинный путь: окна по 29 дней плюс курсор.

    Именно она и выбрала лимит 06.10: один прогон по трёхмесячной истории это
    несколько окон, каждое со своим листанием, и без паузы они уходят подряд.
    """
    session = _CountingSession({"orders": [], "next": 0})
    client = WbClient(token="t", warehouse_id="wh", session=session)

    from app.timeutils import today_local
    from datetime import timedelta
    client.get_orders_since(today_local() - timedelta(days=70))

    assert len(session.calls) >= 3, "окон меньше трёх — путь взят не тот"
    _assert_throttled(session, pauses)


def test_awaiting_confirmation_pauses(pauses):
    """`/api/v3/orders/new` — ровно та ручка, на которой всё и легло."""
    session = _CountingSession({"orders": []})
    client = WbClient(token="t", warehouse_id="wh", session=session)

    client.get_orders_awaiting_confirmation()

    _assert_throttled(session, pauses)


def test_pushing_stock_pauses(pauses):
    """Отправка остатков идёт пачками, и пачек после массовой переотправки много."""
    session = _CountingSession({})
    client = WbClient(token="t", warehouse_id="wh", session=session)

    client.push_stock("wh", [StockPushItem(barcode="2000932309163", quantity=5,
                                          external_id="12345:67890")])

    _assert_throttled(session, pauses)


def test_reading_stock_pauses(pauses):
    """Сверка спрашивает остатки раз в полчаса по всем отправкам за сутки."""
    session = _CountingSession({"stocks": []})
    client = WbClient(token="t", warehouse_id="wh", session=session)

    client.get_stocks("wh", [StockPushItem(barcode="2000932309163", quantity=5,
                                          external_id="12345:67890")])

    _assert_throttled(session, pauses)


def test_the_catalog_pauses(pauses):
    """Каталог — до двухсот страниц по сто карточек на контентном хосте."""
    session = _CountingSession({"cards": [], "cursor": {}})
    client = WbClient(token="t", warehouse_id="wh", session=session)

    client.get_catalog_items()

    _assert_throttled(session, pauses)


def test_asking_cancellations_pauses(pauses):
    """Опрос отмен идёт пачками по списку ОТКРЫТЫХ заказов кабинета.

    Заказы WB не закрываются никогда (площадка не отдаёт подтверждений), так
    что список растёт со скоростью продаж, и пачек со временем становится всё
    больше — то есть этот путь выбирает лимит сам по себе, без каталога.
    """
    session = _CountingSession({"orders": []})
    client = WbClient(token="t", warehouse_id="wh", session=session)

    client.get_cancelled_orders([str(5583015069 + i) for i in range(2500)])

    assert len(session.calls) >= 2, "пачек меньше двух — путь взят не тот"
    _assert_throttled(session, pauses)


# ---------------------------------------------------------------- Kit

def test_kit_keeps_its_own_throttle(pauses):
    """У Kit дроссель был с самого начала — и не был закрыт ничем.

    Лимит магазина по спеке (`github.com/yandex/kit-skills`) — 10 запросов в
    секунду, а именно у Kit 429 уже стоил потерянных строк заказов: баркод
    строки узнаётся отдельным запросом варианта, и 429 после всех повторов
    раньше просто выбрасывал заказ.
    """
    session = _CountingSession({"variants": [], "total_count": 0})
    client = KitClient(token="t", session=session)

    client.get_catalog_items()

    _assert_throttled(session, pauses, minimum=kit_mod._THROTTLE_SECONDS)


def test_kit_publishing_a_card_pauses(pauses):
    """Публикация скрытых карточек идёт по ОДНОЙ на каждый ключ пачки.

    Распроданная карточка Kit уходит в `HIDDEN` и сама не возвращается, так что
    после массовой переотправки таких ключей бывают сотни — и каждый это свой
    `PATCH`. Без паузы это ровно та пачка, которой лимит и выбирается.
    """
    session = _CountingSession({})
    client = KitClient(token="t", session=session)

    for key in ("v-1", "v-2", "v-3"):
        client.publish_stock_key(key)

    assert len(session.calls) == 3
    _assert_throttled(session, pauses, minimum=kit_mod._THROTTLE_SECONDS)


def test_kit_pushing_stock_pauses(pauses):
    """Отправка остатков Kit — `bulk_update` пачками по сто позиций.

    После массовой переотправки таких пачек столько же, сколько сотен в
    каталоге; и именно у Kit 429 уже оборачивался отказом ЦЕЛОГО запроса, то
    есть терялась не одна позиция, а вся пачка.
    """
    session = _CountingSession({"results": []})
    client = KitClient(token="t", session=session)

    client.push_stock("wh", [StockPushItem(barcode="2000932309163", quantity=5,
                                           external_id="v-1")])

    _assert_throttled(session, pauses, minimum=kit_mod._THROTTLE_SECONDS)


def test_kit_confirming_orders_pauses(pauses):
    """Подтверждение — ПО ЗАПРОСУ НА ЗАКАЗ, то есть пачка по построению.

    Этот путь паузы не делал вовсе, как и `bulk_update` выше: дроссель у Kit
    стоял только в `_get` и в патче карточки. Самый частый путь — опрос новых
    заказов каждые 45 секунд с подтверждением каждого — уходил пачкой.

    (Метод `_post` у Kit при этом не зовёт никто — он мёртвый, и мутацией его
    паузы ничего не поймать. Это не пробел проверки.)
    """
    session = _CountingSession({})
    client = KitClient(token="t", session=session)

    for oid in ("o-1:1", "o-2:1", "o-3:1"):
        client.confirm_order(oid)

    assert len(session.calls) == 3
    _assert_throttled(session, pauses, minimum=kit_mod._THROTTLE_SECONDS)

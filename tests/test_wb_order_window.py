"""WB отдаёт ОКНО от dateFrom, а не «все заказы с этой даты».

Разбор 18.09.2026 на живом кабинете. Оператор заметил, что WB показывает заказ
5777066244 от 15.09, а у нас его нет. Проба по кабинету ИП ЯВОРСКАЯ:

    dateFrom=07.08 -> 520 заказов, даты 07.08 .. 05.09, и всё
    dateFrom=01.09 -> 334 заказа, среди них искомый
    dateFrom=01.07 -> НОЛЬ заказов (при 244 от 07.08 в другом кабинете)

Ноль на более раннюю дату доказывает окно: будь это обычное «с этой даты»,
раннее окно вернуло бы БОЛЬШЕ. Одним запросом от базовой даты мы теряли всё,
что новее её плюс месяц: 253 заказа по одному кабинету. Расчёт при этом
рапортовал «проведено 0, проблем нет» и ставил товару «актуализирован» —
то есть разрешал транслировать остаток, из которого не вычтены продажи.
"""
from datetime import date, datetime, timedelta, timezone

from app.workers.platform_clients.wb import ORDERS_WINDOW_DAYS, WbClient


def _order(oid, day, sku="2000932309163"):
    return {"id": oid, "skus": [sku], "createdAt": day + "T10:00:00Z",
            "supplierStatus": "complete"}


class _Wb:
    """Площадка, отдающая ровно окно в ORDERS_WINDOW_DAYS дней от dateFrom."""

    def __init__(self, orders, window=ORDERS_WINDOW_DAYS, page=1000):
        self.orders = orders          # {"2026-09-15": [order, ...]}
        self.window = window
        self.page = page
        self.asked = []               # какие dateFrom спрашивали

    def __call__(self, path, params=None, **kw):
        ts = (params or {}).get("dateFrom")
        start = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        self.asked.append(start)
        end = start + timedelta(days=self.window)
        out = []
        for day, items in sorted(self.orders.items()):
            d = date.fromisoformat(day)
            if start <= d <= end:
                out.extend(items)
        cursor = (params or {}).get("next") or 0
        chunk = out[cursor:cursor + self.page]
        nxt = cursor + len(chunk) if cursor + len(chunk) < len(out) else 0
        return {"orders": chunk, "next": nxt}


def _client(fake):
    c = WbClient(token="t", warehouse_id="wh")
    c._get = fake
    return c


def _today():
    return datetime.now(timezone.utc).date()


# ------------------------------------------------ окно

def test_an_order_beyond_the_first_window_is_still_found():
    """Тот самый заказ: базовая дата 07.08, заказ 15.09 — за краем первого окна."""
    start = _today() - timedelta(days=45)
    far = _today() - timedelta(days=3)
    fake = _Wb({
        start.isoformat(): [_order("5467811441", start.isoformat())],
        far.isoformat():   [_order("5777066244", far.isoformat())],
    })

    orders = _client(fake).get_orders_since(start)

    assert sorted(o.order_id for o in orders) == ["5467811441", "5777066244"]


def test_one_request_per_window_up_to_today():
    """Окна должны дойти до сегодняшнего дня, иначе свежие продажи невидимы."""
    start = _today() - timedelta(days=70)
    fake = _Wb({})

    _client(fake).get_orders_since(start)

    assert fake.asked[0] == start
    assert fake.asked[-1] == _today(), "последнее окно обязано упираться в сегодня"
    assert all(b > a for a, b in zip(fake.asked, fake.asked[1:])), "окна должны идти вперёд"


def test_overlapping_windows_do_not_double_count():
    """Окна перекрываются краем: один заказ не должен провестись дважды."""
    start = _today() - timedelta(days=ORDERS_WINDOW_DAYS)
    fake = _Wb({start.isoformat(): [_order("1", start.isoformat())],
                _today().isoformat(): [_order("2", _today().isoformat())]})

    orders = _client(fake).get_orders_since(start)

    assert [o.order_id for o in orders] == ["1", "2"]


def test_a_single_day_range_still_asks_once():
    fake = _Wb({_today().isoformat(): [_order("1", _today().isoformat())]})

    orders = _client(fake).get_orders_since(_today())

    assert [o.order_id for o in orders] == ["1"]
    assert fake.asked == [_today()]


# ------------------------------------------------ листание страниц

def test_pagination_follows_the_cursor_not_the_page_size():
    """Соседний get_catalog_items эту грабку уже прошёл: WB отдаёт меньше лимита
    за страницу, но данные при этом не кончились."""
    day = _today().isoformat()
    fake = _Wb({day: [_order(str(i), day) for i in range(5)]}, page=2)

    orders = _client(fake).get_orders_since(_today())

    assert sorted(o.order_id for o in orders) == ["0", "1", "2", "3", "4"]


def test_a_stuck_cursor_does_not_loop_forever():
    calls = []

    def fake(path, params=None, **kw):
        calls.append(1)
        return {"orders": [_order("1", _today().isoformat())], "next": 777}

    c = WbClient(token="t", warehouse_id="wh")
    c._get = fake
    c.get_orders_since(_today())

    assert len(calls) == 2, "второй ответ с тем же курсором обязан остановить листание"


def test_hitting_the_page_guard_marks_the_feed_incomplete():
    """Оборвались по пределу, а не по концу данных — картина неполная, и расчёт
    обязан узнать об этом, а не поставить «актуализирован»."""
    n = [0]

    def fake(path, params=None, **kw):
        n[0] += 1
        return {"orders": [_order(str(n[0]), _today().isoformat())], "next": n[0]}

    c = WbClient(token="t", warehouse_id="wh")
    c._get = fake
    c.get_orders_since(_today())

    assert c.last_truncated is True


def test_a_clean_walk_leaves_the_feed_complete():
    day = _today().isoformat()
    fake = _Wb({day: [_order("1", day)]})
    c = _client(fake)

    c.get_orders_since(_today())

    assert c.last_truncated is False


# --------------------------------------------- статусы заказов режутся на пачки

def test_status_request_is_split_into_batches():
    """`/api/v3/orders/status` принимает не больше STATUS_BATCH_IDS за раз.
    Заказы WB не закрываются никогда (площадка не отдаёт подтверждения), поэтому
    список открытых растёт со скоростью продаж: без деления на пачки площадка
    рано или поздно отвергла бы запрос ЦЕЛИКОМ, и отмены перестали бы
    отслеживаться сразу по всему кабинету."""
    from app.workers.platform_clients.wb import STATUS_BATCH_IDS

    sizes = []

    c = WbClient(token="t", warehouse_id="wh")

    def fake_post(path, body=None, **kw):
        sizes.append(len(body["orders"]))
        return {"orders": [{"id": i, "supplierStatus": "cancel"} for i in body["orders"]]}

    c._post = fake_post
    ids = [str(i) for i in range(STATUS_BATCH_IDS * 2 + 7)]

    res = c.get_cancelled_orders(ids)

    assert sizes == [STATUS_BATCH_IDS, STATUS_BATCH_IDS, 7]
    assert len(res) == len(ids)          # ни один заказ не потерян при делении


def test_a_small_status_request_stays_one_call():
    c = WbClient(token="t", warehouse_id="wh")
    calls = []
    c._post = lambda path, body=None, **kw: (calls.append(len(body["orders"])),
                                             {"orders": []})[1]

    c.get_cancelled_orders(["1", "2", "3"])

    assert calls == [3]

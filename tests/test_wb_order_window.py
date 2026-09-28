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

from app.timeutils import local_date_of, today_local
from app.workers.platform_clients.base import StockPushItem
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
        # Как и площадка, отметку времени превращаем в МЕСТНОЕ число: клиент
        # спрашивает ленту от начала местных суток, и под Москвой это 21:00 UTC
        # предыдущего дня (см. test_orders_window_local_day).
        start = local_date_of(datetime.fromtimestamp(int(ts), tz=timezone.utc))
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
    return today_local()


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


# ------------------------------------- отправка остатков: ответ больше не глотаем

def test_a_successful_push_reports_every_sku():
    """Штатный случай: 204 с пустым телом — приняты все позиции."""
    from app.workers.platform_clients.base import StockPushItem

    c = WbClient(token="t", warehouse_id="wh")

    class Resp:
        status_code = 204
        text = ""

    c.session.put = lambda *a, **kw: Resp()
    Resp.raise_for_status = lambda self: None

    res = c.push_stock("wh", [StockPushItem(barcode="111", quantity=5),
                              StockPushItem(barcode="222", quantity=7)])

    assert res == {"ok": ["111", "222"], "errors": []}


def test_a_success_code_with_a_body_is_not_silently_accepted():
    """Успех у WB — 204 с ПУСТЫМ телом (проверено на живом кабинете 19.09).
    Успешный код с телом означает что-то, чего мы не ждали; записать такое в
    «отправлено всё» значит соврать себе о том, что лежит на площадке. Схему
    ошибок внутри 2xx мы не знаем, поэтому отдаём текст наверх как есть."""
    from app.workers.platform_clients.base import StockPushItem

    c = WbClient(token="t", warehouse_id="wh")

    class Resp:
        status_code = 200
        text = '{"errors": ["sku 111 не найден"]}'

        def raise_for_status(self):
            return None

    c.session.put = lambda *a, **kw: Resp()

    res = c.push_stock("wh", [StockPushItem(barcode="111", quantity=5)])

    assert res["ok"] == []
    assert "не найден" in res["errors"][0]["detail"]


# ---------------------------------------- чтение остатков: тем же ключом

# Сверка обязана спрашивать ТЕМ ЖЕ ключом, которым отправляли. До этой правки
# запрос уходил телом `{"skus": [...]}` — и работает по сей день, площадка его
# принимает. Но в спеке параметр называется `chrtIds`, ответ приходит с полем
# `chrtId`, и там же сказано: имена параметров WB НЕ ВАЛИДИРУЕТ, неизвестное имя
# даёт ответ без ошибки. То есть в день, когда `skus` перестанет пониматься,
# отказа не будет вовсе: ответ придёт пустым, и сверка объявит ВСЕ отправки
# кабинета неизвестными площадке — выглядит как разом сломавшийся мэппинг.


class _StockResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _reader(answer):
    """Клиент WB с подставной сессией. `answer(body) -> payload`."""
    c = WbClient(token="t", warehouse_id="wh")
    asked = []

    def fake_post(url, json=None, **kw):
        asked.append(json)
        return _StockResp(answer(json))

    c.session.post = fake_post
    return c, asked


def test_reading_stocks_back_asks_by_chrt_id():
    """chrtId берётся из каталога тем же правилом, что и при отправке
    (`_chrt_id`), а ответ переводится обратно на баркод — сверке он и нужен."""
    def answer(body):
        return {"stocks": [{"chrtId": chrt, "amount": 4} for chrt in body["chrtIds"]]}

    c, asked = _reader(answer)

    held = c.get_stocks("wh", [
        StockPushItem(barcode="111", quantity=0, external_id="777:901"),
        StockPushItem(barcode="222", quantity=0, external_id="777:902"),
    ])

    assert asked == [{"chrtIds": [901, 902]}], "спрашиваем chrtId, а не баркод"
    assert held == {"111": 4, "222": 4}, "а отвечаем баркодами"


def test_a_position_without_chrt_id_is_still_asked_by_barcode():
    """Каталог кабинета не загружен или протух — позиция всё равно проверяема:
    отказаться от баркодного запроса значило бы перестать проверять то, что до
    сих пор проверялось. Ключи в одном теле не смешиваем — спека знает один."""
    def answer(body):
        if "chrtIds" in body:
            return {"stocks": [{"chrtId": 901, "amount": 4}]}
        return {"stocks": [{"sku": s, "amount": 7} for s in body["skus"]]}

    c, asked = _reader(answer)

    held = c.get_stocks("wh", [
        StockPushItem(barcode="111", quantity=0, external_id="777:901"),
        StockPushItem(barcode="222", quantity=0, external_id=""),
    ])

    assert asked == [{"chrtIds": [901]}, {"skus": ["222"]}]
    assert held == {"111": 4, "222": 7}


def test_two_products_on_one_card_both_get_the_answer():
    """Два товара 1С могут вести на одну карточку площадки. Ответ приходит один,
    а отнести его надо к обоим — иначе второй сойдёт за неизвестный площадке."""
    def answer(body):
        return {"stocks": [{"chrtId": 901, "amount": 3}]}

    c, _ = _reader(answer)

    held = c.get_stocks("wh", [
        StockPushItem(barcode="111", quantity=0, external_id="777:901"),
        StockPushItem(barcode="222", quantity=0, external_id="777:901"),
    ])

    assert held == {"111": 3, "222": 3}


def test_reading_stocks_back_splits_into_batches_and_keeps_zero_apart_from_missing():
    """Ноль и отсутствие позиции — разные вещи: ноль это «карточка есть, пусто»,
    отсутствие — «такой позиции тут нет вовсе»."""
    from app.workers.platform_clients.wb import STOCKS_BATCH_SKUS

    def answer(body):
        # площадка отвечает не по всем: 903 она не знает
        return {"stocks": [{"chrtId": chrt, "amount": 0 if chrt == 901 else 4}
                           for chrt in body["chrtIds"] if chrt != 903]}

    c, asked = _reader(answer)

    held = c.get_stocks("wh", [
        StockPushItem(barcode="111", quantity=0, external_id="777:901"),
        StockPushItem(barcode="222", quantity=0, external_id="777:902"),
        StockPushItem(barcode="333", quantity=0, external_id="777:903"),
    ])

    assert held == {"111": 0, "222": 4}     # 333 отсутствует, а не ноль
    assert len(asked) == 1
    assert STOCKS_BATCH_SKUS == 1000


def test_a_silent_platform_returns_none_not_an_empty_dict():
    """Пустой словарь означал бы «на площадке ничего нет» и объявил бы все наши
    отправки расхождением. Молчание площадки — это отсутствие ответа."""
    import requests

    c = WbClient(token="t", warehouse_id="wh")

    def boom(*a, **kw):
        raise requests.ConnectionError("нет сети")

    c.session.post = boom

    assert c.get_stocks("wh", [StockPushItem(barcode="111", quantity=0)]) is None


def test_silence_in_one_batch_leaves_the_whole_check_undone():
    """Половина ответа опаснее отсутствующего: сверка считает позицию, про
    которую площадка промолчала, неизвестной ей — то есть сетевой сбой по одной
    пачке выглядел бы как «карточки на складе нет» по всей второй."""
    import requests

    def answer(body):
        if "skus" in body:
            raise requests.ConnectionError("нет сети")
        return {"stocks": [{"chrtId": 901, "amount": 4}]}

    c, _ = _reader(answer)

    held = c.get_stocks("wh", [
        StockPushItem(barcode="111", quantity=0, external_id="777:901"),
        StockPushItem(barcode="222", quantity=0, external_id=""),
    ])

    assert held is None

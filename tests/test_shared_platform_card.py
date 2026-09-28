"""Один физический товар под двумя артикулами 1С — остатки СКЛАДЫВАЮТСЯ.

Случай редкий, но живой: 26.09.2026 на бою `4033 Неро Джемпер 50/50 SIYAH` и
`4052 (4033) b 3XL SIYAH` оказались одной вещью, и обе строки вели на один
размер карточки WB. Писали они по очереди, и выигрывал написавший последним:
у одного уходило 10, у второго 18, на площадке лежало то одно, то другое.

Снаружи это выглядит как отказ площадки («отправили 18, держит 10»), и
разобрать по одной строке нельзя никогда: каждая про свой товар и каждая
по-своему права. Следствие двустороннее — лежит меньшее число, значит часть
товара не продаётся; лежит большее, значит продаётся то, чего по этой строке
нет.

Отдельно проверяется WB, и не для порядка: там ключ отправки — БАРКОД, а в
теле уезжает chrtId, и выбирает его сам клиент. Два разных баркода на одном
размере карточки по ключу отправки выглядят как разные ячейки — сложение не
сработало бы ровно там, ради чего затевалось.
"""
from app.models import (Barcode, DispatchQueueItem, DispatchStatus, Platform,
                        PlatformCatalogItem, Product, SyncSetting)
from app.timeutils import now_utc
from app.workers.dispatch import run_dispatch_cycle
from tests.factories import make_account


class _Wb:
    """Клиент WB: ключ отправки — баркод, ответ приходит по нему же."""
    stock_key = "barcode"

    def __init__(self):
        self.sent = []

    def push_stock(self, warehouse_id, items):
        self.sent.append([(i.barcode, i.external_id, i.quantity) for i in items])
        return {"ok": [i.barcode for i in items], "errors": []}


def _row(db, account, uid, barcode, chrt, stock, *, enabled=True,
         broadcast=True, queued=True):
    db.add(Product(uid_1c=uid, article=uid, name="Джемпер", stock_on_hand=stock,
                   broadcast_enabled=broadcast, recalc_account_ids=str(account.id)))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=enabled))
    db.add(PlatformCatalogItem(account_id=account.id, external_id=chrt,
                               barcode=barcode, article="WB-4033", name="Карточка"))
    if queued:
        db.add(DispatchQueueItem(uid_1c=uid, account_id=account.id,
                                 quantity=stock, reason="order"))


def test_two_barcodes_on_one_wb_size_are_summed(db):
    """По `sent_sku` эти два ключа разные — складывать надо по chrtId."""
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-4033", "2000000000011", "177:4242", 10)
    _row(db, account, "u-4052", "2000000000022", "177:4242", 18)
    db.commit()

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    assert len(client.sent) == 1
    assert len(client.sent[0]) == 1, "карточка адресуется один раз"
    assert client.sent[0][0][2] == 28, "на размер карточки ушла сумма"
    rows = {r.uid_1c: r for r in db.query(DispatchQueueItem).all()}
    assert all(r.status == DispatchStatus.sent for r in rows.values())
    assert rows["u-4033"].sent_quantity == 28
    assert rows["u-4052"].sent_quantity == 28


def test_different_sizes_of_one_card_are_not_summed(db):
    """chrtId — это РАЗМЕР, а не карточка. Сложи мы по nmID, на каждый размер
    уехал бы остаток всего ряда, и это прямой оверселл."""
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-l", "2000000000011", "177:4242", 10)
    _row(db, account, "u-xl", "2000000000022", "177:9999", 18)
    db.commit()

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    sent = {barcode: qty for barcode, _, qty in client.sent[0]}
    assert sent == {"2000000000011": 10, "2000000000022": 18}


def test_a_lone_product_is_not_touched_by_the_summing(db):
    """Одиночных товаров — весь каталог. Их путь обязан остаться прежним."""
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-1", "2000000000011", "177:4242", 10)
    db.commit()

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    item = db.query(DispatchQueueItem).one()
    assert item.sent_quantity == 10
    assert item.last_error is None, "оговорки про сумму у одиночки быть не должно"


def test_a_zero_is_withdrawn_when_the_neighbour_was_transmitted(db):
    """Отзыв спрашивают про ЯЧЕЙКУ, а не про одну пару.

    Ноль по паре, куда мы ни разу не писали, не отзывает наш остаток, а
    обнуляет чужие продажи — авария 18.09. Но если непустой остаток на эту
    карточку уходил по СОСЕДУ, то на площадке лежит НАШЕ число, и ноль по ней —
    законный отзыв. Спроси мы только про одну пару, карточка осталась бы
    торговать остатком, которого уже нет.
    """
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    # Обе половины под выключателем: трансляция выключена у обеих.
    _row(db, account, "u-a", "2000000000011", "177:4242", 10, broadcast=False)
    _row(db, account, "u-b", "2000000000022", "177:4242", 18, broadcast=False,
         queued=False)
    db.commit()
    neighbour = db.query(SyncSetting).filter(SyncSetting.uid_1c == "u-b").one()
    neighbour.last_nonzero_sent_at = now_utc()
    db.commit()

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.sent and client.sent[0][0][2] == 0, "ноль уехал отзывом"


def test_a_zero_on_an_untouched_card_is_still_not_sent(db):
    """Обратная половина того же правила: по ячейке не писали ни разу — молчим."""
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-a", "2000000000011", "177:4242", 10, broadcast=False)
    _row(db, account, "u-b", "2000000000022", "177:4242", 18, broadcast=False,
         queued=False)
    db.commit()

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.sent == []
    item = db.query(DispatchQueueItem).one()
    assert item.sent_quantity is None
    assert "отзывать нечего" in item.last_error


def test_the_products_page_says_the_number_is_shared(logged_in_client, web_db):
    """Строка показывает вклад ОДНОГО товара, а на витрину уедет сумма.

    Молчать об этом нельзя: человек сверяет строку с кабинетом, видит «10» у
    себя и 28 на площадке и решает, что система врёт или что площадка не
    приняла отправку. Ровно на этом 26.09 ушёл вечер разбора.
    """
    account = make_account(web_db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(web_db, account, "u-4033", "2000000000011", "177:4242", 10, queued=False)
    _row(web_db, account, "u-4052", "2000000000022", "177:4242", 18, queued=False)
    web_db.commit()

    body = logged_in_client.get("/products").text
    assert "на карточке ещё" in body, body[:400]
    assert "u-4052" in body and "u-4033" in body
    assert "уходит 28" in body


def test_a_lone_product_gets_no_shared_card_note(logged_in_client, web_db):
    """Лишняя пометка на обычной строке — шум, который приучает не читать."""
    account = make_account(web_db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(web_db, account, "u-1", "2000000000011", "177:4242", 10, queued=False)
    web_db.commit()

    assert "на карточке ещё" not in logged_in_client.get("/products").text


def test_only_barcodes_that_the_card_itself_carries_are_summed(db):
    """Сосед — это ТОЛЬКО тот, чей баркод лежит в этой же карточке кабинета.

    Ни похожий артикул, ни то же наименование, ни соседство в «Сопоставлении
    площадок» соседом не делают: сложить остатки двух РАЗНЫХ вещей значит
    отправить на витрину больше, чем лежит на складе, — прямой оверселл. Здесь
    у второго товара баркод есть, а строки каталога под этой карточкой у него
    нет, и в сумму он идти не должен.
    """
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-4033", "2000000000011", "177:4242", 10)
    db.add(Product(uid_1c="u-чужой", article="4033", name="Джемпер",
                   stock_on_hand=18, broadcast_enabled=True,
                   recalc_account_ids=str(account.id)))
    db.add(Barcode(barcode="2000000000022", uid_1c="u-чужой"))
    db.add(SyncSetting(uid_1c="u-чужой", account_id=account.id, enabled=True))
    db.commit()

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    item = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.uid_1c == "u-4033").one()
    assert item.sent_quantity == 10, "чужая вещь в сумму не идёт"
    assert item.last_error is None


def test_a_neighbour_outside_the_queue_is_remembered_as_transmitted(db):
    """Остаток соседа уехал внутри суммы — значит на его пару мы ПИСАЛИ.

    Память об этом живёт на паре (`SyncSetting.last_nonzero_sent_at`), и
    записи в очереди у соседа может не быть вовсе. Не пометь мы её — система
    забудет, что писала на эту карточку, и снятие галочки по соседу потом
    ничего не отзовёт: площадка продолжит продавать по нашему числу.
    """
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-4033", "2000000000011", "177:4242", 10)
    _row(db, account, "u-4052", "2000000000022", "177:4242", 18, queued=False)
    db.commit()

    run_dispatch_cycle(db, {account.id: _Wb()}, [account])

    pairs = {s.uid_1c: s.last_nonzero_sent_at
             for s in db.query(SyncSetting).all()}
    assert pairs["u-4033"] is not None
    assert pairs["u-4052"] is not None, "сосед вне очереди тоже получил остаток"


def test_a_neighbour_that_added_nothing_is_not_remembered(db):
    """Вклад нулевой — на карточку его остаток не уезжал, и метить нечего:
    иначе снятие галочки по нему отозвало бы то, чего мы не отправляли."""
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-4033", "2000000000011", "177:4242", 10)
    _row(db, account, "u-4052", "2000000000022", "177:4242", 18, queued=False,
         broadcast=False)
    db.commit()

    run_dispatch_cycle(db, {account.id: _Wb()}, [account])

    pairs = {s.uid_1c: s.last_nonzero_sent_at for s in db.query(SyncSetting).all()}
    assert pairs["u-4033"] is not None
    assert pairs["u-4052"] is None

"""Лента заказов кабинета берётся ОДИН раз на прогон, а не на каждый товар.

Расчёт по тысяче позиций при пяти отмеченных кабинетах — это было пять тысяч
полных выкачек ленты, и каждая у WB идёт окнами по 29 дней с листанием по
курсору, то есть десятки запросов. Отсюда «расчёт идёт минутами» и упор в лимиты
площадок — тот самый, из-за которого у Kit терялись строки заказов.

Кэш живёт пять минут намеренно: расчёт больших пачек растягивается на десятки
тиков, и вечная лента означала бы, что заказы, пришедшие за это время, расчёт не
увидит, а отметку «актуализирован» поставит.
"""
from datetime import date, timedelta

from app.models import Barcode, Platform, Product, SyncSetting
from app.recalc import _orders_cache, clear_orders_cache, collect_orders
from app.workers.platform_clients.base import PlatformOrder
from tests.factories import make_account

DAY = date(2026, 8, 7)


class CountingClient:
    """Считает, сколько раз у него спросили ленту."""

    def __init__(self, orders=(), unresolved=0, truncated=False):
        self.calls = 0
        self._orders = list(orders)
        self.last_unresolved = unresolved
        self.last_truncated = truncated

    def get_orders_since(self, since):
        self.calls += 1
        return list(self._orders)


def _product(db, uid, barcode):
    product = Product(uid_1c=uid, article=f"A-{uid}", name="Товар", stock_on_hand=5,
                      offset_base_date=DAY)
    db.add(product)
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.commit()
    return product


def _two_products_one_account(db):
    account = make_account(db, Platform.wb)
    first = _product(db, "u1", "b1")
    second = _product(db, "u2", "b2")
    for uid in ("u1", "u2"):
        db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))
    db.commit()
    return account, first, second


def test_the_feed_is_fetched_once_for_two_products(db):
    account, first, second = _two_products_one_account(db)
    client = CountingClient()

    collect_orders(db, first, DAY, lambda d, aid: client)
    collect_orders(db, second, DAY, lambda d, aid: client)

    assert client.calls == 1, "второй товар обязан взять ленту из кэша"


def test_a_lost_line_is_remembered_with_the_feed(db):
    """Признаки неполноты кэшируются ВМЕСТЕ с лентой. Иначе второй товар получил
    бы ленту без них и отметку «актуализирован» по картине, которую первый товар
    справедливо счёл неполной — то есть по продажам, которых расчёт не видел."""
    account, first, second = _two_products_one_account(db)
    client = CountingClient(unresolved=3)

    _, problems_first, _ = collect_orders(db, first, DAY, lambda d, aid: client)
    client.last_unresolved = 0            # клиент «забыл», лента взята из кэша
    _, problems_second, _ = collect_orders(db, second, DAY, lambda d, aid: client)

    assert problems_first and problems_second, "пробел обязан дожить до второго товара"


def test_a_truncated_feed_is_remembered_too(db):
    account, first, second = _two_products_one_account(db)
    client = CountingClient(truncated=True)

    _, problems_first, _ = collect_orders(db, first, DAY, lambda d, aid: client)
    client.last_truncated = False
    _, problems_second, _ = collect_orders(db, second, DAY, lambda d, aid: client)

    assert problems_first and problems_second


def test_a_stale_entry_is_refetched(db):
    """Пять минут — предел: дальше лента берётся заново, иначе расчёт больших
    пачек ставил бы «актуализирован» по получасовой давности картине."""
    account, first, second = _two_products_one_account(db)
    client = CountingClient()

    collect_orders(db, first, DAY, lambda d, aid: client)
    key, (when, orders, lost, truncated) = next(iter(_orders_cache.items()))
    _orders_cache[key] = (when - timedelta(minutes=6), orders, lost, truncated)

    collect_orders(db, second, DAY, lambda d, aid: client)

    assert client.calls == 2


def test_a_different_date_is_a_different_feed(db):
    """Ключ — кабинет И дата: расчёт от другого числа поднимает другой период."""
    account, first, second = _two_products_one_account(db)
    client = CountingClient()

    collect_orders(db, first, DAY, lambda d, aid: client)
    collect_orders(db, second, DAY - timedelta(days=30), lambda d, aid: client)

    assert client.calls == 2


def test_clearing_forgets_everything(db):
    account, first, second = _two_products_one_account(db)
    client = CountingClient()

    collect_orders(db, first, DAY, lambda d, aid: client)
    clear_orders_cache()
    collect_orders(db, second, DAY, lambda d, aid: client)

    assert client.calls == 2

"""Дата расчёта старше памяти ленты — это ПРОБЛЕМА, а не пустой результат.

Спека `/api/v3/orders` у WB: метод возвращает сборочные задания, созданные не
более трёх месяцев назад. Запрос от более старой даты не падает и не жалуется —
он честно отвечает пустотой. Замер 18.09 это уже нащупал, только принял за
особенность окна: `dateFrom=01.07` вернул НОЛЬ заказов, тогда как от 07.08 их
было 244.

Без проверки расчёт по такой дате рапортует «проведено 0, проблем нет» и ставит
товару «актуализирован» — то есть открывает трансляцию полного остатка по
продажам, которых никто не видел. Ровно исход инцидента 18.09, когда по одному
кабинету недосчитались 253 заказа.
"""
from datetime import timedelta

from app.models import Barcode, Platform, Product, SyncSetting
from app.recalc import collect_orders
from app.timeutils import today_local
from app.workers.platform_clients.wb import ORDERS_HISTORY_DAYS
from tests.factories import make_account


class WbLikeClient:
    """Клиент с подтверждённым пределом истории — как настоящий WB."""
    orders_history_days = ORDERS_HISTORY_DAYS
    last_unresolved = 0
    last_truncated = False

    def __init__(self):
        self.asked = []

    def get_orders_since(self, since):
        self.asked.append(since)
        return []


class SilentClient(WbLikeClient):
    """Клиент БЕЗ подтверждённого предела — как Ozon и Kit."""
    orders_history_days = None


def _product(db, day):
    account = make_account(db, Platform.wb)
    db.add(Product(uid_1c="u1", article="A", name="Товар", stock_on_hand=5,
                   offset_base_date=day))
    db.add(Barcode(barcode="b1", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    return db.query(Product).filter(Product.uid_1c == "u1").one()


def test_a_recent_date_is_fine(db):
    day = today_local() - timedelta(days=30)
    product = _product(db, day)
    client = WbLikeClient()

    _, problems, covered = collect_orders(db, product, day, lambda d, aid: client)

    assert problems == []
    assert client.asked, "ленту обязаны спросить"


def test_a_date_older_than_the_horizon_is_a_problem(db):
    day = today_local() - timedelta(days=ORDERS_HISTORY_DAYS + 5)
    product = _product(db, day)
    client = WbLikeClient()

    _, problems, covered = collect_orders(db, product, day, lambda d, aid: client)

    assert problems, "пустой ответ ленты не должен сойти за «продаж не было»"
    assert str(ORDERS_HISTORY_DAYS) in problems[0]


def test_such_an_account_is_not_counted_as_covered(db):
    """Главное последствие: без покрытия не будет и отметки «актуализирован», а
    значит ворота трансляции останутся закрытыми."""
    day = today_local() - timedelta(days=ORDERS_HISTORY_DAYS + 5)
    product = _product(db, day)

    _, _, covered = collect_orders(db, product, day, lambda d, aid: WbLikeClient())

    assert covered == []


def test_the_feed_is_not_even_asked(db):
    """Спрашивать бессмысленно: ответ заведомо пуст, а запрос стоит лимита."""
    day = today_local() - timedelta(days=ORDERS_HISTORY_DAYS + 5)
    product = _product(db, day)
    client = WbLikeClient()

    collect_orders(db, product, day, lambda d, aid: client)

    assert client.asked == []


def test_a_client_without_a_known_horizon_is_left_alone(db):
    """У Ozon и Kit подтверждённого предела у нас нет. Выдумать его значило бы
    поставить ложную проблему и не дать включить трансляцию там, где всё в
    порядке."""
    day = today_local() - timedelta(days=ORDERS_HISTORY_DAYS + 5)
    product = _product(db, day)
    client = SilentClient()

    _, problems, _ = collect_orders(db, product, day, lambda d, aid: client)

    assert problems == []
    assert client.asked, "ленту спрашиваем как раньше"

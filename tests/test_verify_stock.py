"""Сверка отправленного с тем, что площадка держит на самом деле.

Поведение, найденное на бою 19.09.2026: мы отправили 68/40/110 на WB, получили
`204`, числа применились — и через три минуты там снова лежало чужое (0/36/108).
В кабинет писала вторая система, та, с которой идёт переход. Отправка об этом не
знает: она видит только момент записи.

Здесь проверяется, что сверка такое ловит, и — не менее важно — что она НЕ
объявляет расхождением то, чем оно не является: сетевой сбой, ещё не осевшую
отправку, устаревшую запись, sku, которого площадка не знает.
"""

from datetime import timedelta

from app.models import (DispatchQueueItem, DispatchStatus, Platform, Product)
from app.timeutils import now_utc
from app.workers.verify_stock import (LOOKBACK, SETTLE_DELAY, rows_to_verify,
                                      verify_account, verify_all)
from tests.factories import make_account


class FakePlatform:
    """Площадка, которая держит то, что ей велели держать."""

    def __init__(self, held=None):
        self._held = held          # None — «отдавать остатки не умеем/не ответила»
        self.asked = []

    def get_stocks(self, warehouse_id, items):
        # На вход идут те же `StockPushItem`, что и в отправку: ключ запроса
        # выбирает клиент площадки, а не сверка. Ключ ответа — баркод.
        self.asked.append((warehouse_id, [i.barcode for i in items]))
        if self._held is None:
            return None
        return {i.barcode: self._held[i.barcode]
                for i in items if i.barcode in self._held}


def _sent(db, account, uid="u1", sku="111", sent=10, minutes_ago=10, **kw):
    db.add(Product(uid_1c=uid, article="A1", name="Товар", stock_on_hand=sent))
    item = DispatchQueueItem(
        uid_1c=uid, account_id=account.id, quantity=sent, sent_quantity=sent,
        sent_sku=sku, reason="order", status=DispatchStatus.sent,
        sent_at=now_utc() - timedelta(minutes=minutes_ago), **kw)
    db.add(item)
    db.commit()
    return item


# ------------------------------------------------------------ ловит чужое

def test_a_platform_holding_less_than_we_sent_is_recorded(db):
    """Ровно случай 19.09: отправили 68, площадка держит 0."""
    account = make_account(db, name="ИП ЯВОРСКАЯ", warehouse_id="wh-1")
    item = _sent(db, account, sku="2004896744404", sent=68)

    stats = verify_account(db, FakePlatform({"2004896744404": 0}), account)

    assert stats == {"checked": 1, "match": 0, "diverged": 1,
                     "unknown_sku": 0, "skipped": 0}
    assert item.verified_quantity == 0
    assert item.verified_at is not None


def test_a_matching_platform_is_recorded_as_checked_not_diverged(db):
    account = make_account(db, warehouse_id="wh-1")
    item = _sent(db, account, sku="111", sent=7)

    stats = verify_account(db, FakePlatform({"111": 7}), account)

    assert stats["match"] == 1 and stats["diverged"] == 0
    assert item.verified_quantity == 7


def test_the_platform_is_asked_with_the_sku_we_actually_sent(db):
    """Не с баркодом товара «вообще», а с тем, которым ушло число: у товара их
    бывает несколько, и выбор делает рассылка в момент отправки."""
    account = make_account(db, warehouse_id="wh-1")
    _sent(db, account, sku="второй-баркод", sent=3)

    platform = FakePlatform({"второй-баркод": 3})
    verify_account(db, platform, account)

    assert platform.asked == [("wh-1", ["второй-баркод"])]


# ------------------------------------------- не выдумывает расхождений

def test_a_silent_platform_leaves_rows_unverified(db):
    """Площадка не ответила — это НЕ расхождение и НЕ совпадение, это отсутствие
    проверки. Записать сюда что-нибудь значило бы позвать человека разбирать
    сетевой сбой как потерю остатка."""
    account = make_account(db, warehouse_id="wh-1")
    item = _sent(db, account, sent=5)

    stats = verify_account(db, FakePlatform(None), account)

    assert stats["skipped"] == 1 and stats["diverged"] == 0
    assert item.verified_at is None and item.verified_quantity is None


def test_an_unknown_sku_is_not_recorded_as_zero(db):
    """Площадка такого sku на складе не знает. Ноль означал бы «карточка есть и
    пуста» — другое утверждение, и разбирают их по-разному."""
    account = make_account(db, warehouse_id="wh-1")
    item = _sent(db, account, sku="нет-такого", sent=5)

    stats = verify_account(db, FakePlatform({"другой": 1}), account)

    assert stats["unknown_sku"] == 1 and stats["diverged"] == 0
    assert item.verified_at is not None          # спрашивали
    assert item.verified_quantity is None        # но числа нет


def test_a_fresh_send_is_not_checked_yet(db):
    """Спросить сразу после отправки значит поймать ещё не применённое значение
    и объявить его расхождением."""
    account = make_account(db, warehouse_id="wh-1")
    _sent(db, account, minutes_ago=0)

    assert rows_to_verify(db, account.id) == []


def test_a_stale_send_is_not_checked(db):
    """По отправке недельной давности расхождение ничего не значит: с тех пор
    остаток менялся десять раз и площадка законно держит другое."""
    account = make_account(db, warehouse_id="wh-1")
    _sent(db, account, minutes_ago=int(LOOKBACK.total_seconds() // 60) + 60)

    assert rows_to_verify(db, account.id) == []


def test_only_the_latest_send_per_product_is_checked(db):
    """За сутки по товару ушло два числа — площадка обязана держать последнее.
    Сравнивать с предыдущим значит выдумать расхождение на ровном месте."""
    account = make_account(db, warehouse_id="wh-1")
    old = _sent(db, account, uid="u1", sku="111", sent=5,
                minutes_ago=int(SETTLE_DELAY.total_seconds() // 60) + 120)
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=9, sent_quantity=9,
        sent_sku="111", reason="order", status=DispatchStatus.sent,
        sent_at=now_utc() - SETTLE_DELAY - timedelta(minutes=1)))
    db.commit()

    rows = rows_to_verify(db, account.id)

    assert [r.sent_quantity for r in rows] == [9]
    assert old.id not in [r.id for r in rows]


def test_a_test_row_is_never_verified(db):
    """Симуляция на площадку не уходила — спрашивать по ней нечего. Та же
    граница `is_test`, что и везде."""
    account = make_account(db, warehouse_id="wh-1")
    _sent(db, account, is_test=True)

    assert rows_to_verify(db, account.id) == []


def test_a_row_without_a_recorded_sku_is_skipped(db):
    """Записи, сделанные до появления колонки `sent_sku`, проверить не по чему.
    Молча взять любой баркод товара нельзя: отправляли, возможно, не им."""
    account = make_account(db, warehouse_id="wh-1")
    item = _sent(db, account, sent=5)
    item.sent_sku = None
    db.commit()

    assert rows_to_verify(db, account.id) == []


# -------------------------------------------------- сверка ничего не чинит

def test_verification_does_not_touch_anything_but_its_own_columns(db):
    """Автоматическая переотправка при живой второй системе превратилась бы в
    гонку двух писателей. Сверка только записывает наблюдение."""
    account = make_account(db, warehouse_id="wh-1")
    item = _sent(db, account, sku="111", sent=68)

    verify_account(db, FakePlatform({"111": 0}), account)

    assert item.status == DispatchStatus.sent      # не переоткрыта
    assert item.sent_quantity == 68                # не переписана
    assert item.attempts == 0                      # новой попытки нет
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 68


# ------------------------------------------------- один кабинет не роняет все

def test_a_failing_cabinet_does_not_stop_the_others(db):
    """Площадки независимы: молчание WB не повод не проверить Kit."""
    bad = make_account(db, Platform.wb, name="Плохой", warehouse_id="wh-1")
    good = make_account(db, Platform.kit, name="Хороший", warehouse_id="wh-2")
    _sent(db, bad, uid="u1", sku="111", sent=5)
    _sent(db, good, uid="u2", sku="222", sent=8)

    def build(db_, account_id):
        if account_id == bad.id:
            raise RuntimeError("ключи протухли")
        return FakePlatform({"222": 3})

    total = verify_all(db, build, [bad, good])

    assert total["errors"] == 1
    assert total["diverged"] == 1                  # Kit всё равно проверили


# --------------------------------------------- сверка действительно ходит сама

def test_the_verify_job_is_registered_and_asks_for_an_early_first_run(web_db):
    """`interval` отсчитывает первый прогон от момента добавления задания, а
    воркер перезапускается чаще — так суточная выгрузка каталога не отработала
    ни разу (19.09)."""
    from datetime import datetime, timezone

    from app.workers.scheduler import build_scheduler

    sched = build_scheduler()
    job = sched.get_job("verify_stock")

    assert job is not None
    assert (job.next_run_time - datetime.now(timezone.utc)).total_seconds() < 300


def test_the_verifier_is_watched_by_health():
    """Молча переставшая ходить сверка выглядит точно так же, как сверка,
    которой нечего сказать."""
    from app.routers.health import EXPECTED_INTERVAL_SECONDS, REQUIRED_WORKERS

    assert "verify_stock" in REQUIRED_WORKERS
    assert EXPECTED_INTERVAL_SECONDS["verify_stock"] > 1800

"""Массовая актуализация остатков — кнопка «Расчёт».

Смысл шага: между базовой датой и сегодняшним днём товар продавался на
маркетплейсах, а в 1С эти отгрузки не отражены. Пока их не провести, остаток ЦС
завышен, и включённая трансляция отправит на площадки числа больше реальных —
оверселл.

Главное требование заказчика к этому шагу: **до включения трансляции на площадки
не уходит ничего** — ни остаток, ни ноль. С площадок только читаем заказы.
"""
from datetime import date

from app.models import (Barcode, DispatchQueueItem, FtpTask, Platform, PlatformAccount,
                        ProcessedOrder, Product, RecalcItem, RecalcJob, RecalcStatus,
                        SyncSetting)
from app.recalc import active_job, catch_up_product, create_job, run_tick
from app.workers.platform_clients.base import PlatformOrder
from app.workers.scheduler import PENDING_WAREHOUSE_NAME
from tests.factories import make_account

DAY = date(2026, 8, 7)


class FakeClient:
    """Площадка отдаёт исторические заказы и ЗАПОМИНАЕТ любые попытки записи."""

    def __init__(self, orders=()):
        self.orders = list(orders)
        self.pushed = []

    def get_orders_since(self, since):
        return [o for o in self.orders if o.order_date is None or o.order_date >= since]

    def push_stock(self, warehouse_id, items):
        self.pushed.append(items)
        return {"ok": [i.barcode for i in items], "errors": []}


def _product(db, uid="u1", stock=20, broadcast=False, day=DAY, fact=None):
    p = Product(uid_1c=uid, article="A-1", name="Товар", stock_on_hand=stock, reserve=0,
                broadcast_enabled=broadcast, offset_base_date=day,
                offset_base_stock=stock, fact_at_date=fact)
    db.add(p)
    db.add(Barcode(barcode=f"bc-{uid}", uid_1c=uid))
    db.commit()
    return p


def _wh(platform):
    return PENDING_WAREHOUSE_NAME.get(platform, "Ожидает")


def _order(oid, uid="u1", qty=1, day=date(2026, 8, 11)):
    return PlatformOrder(order_id=oid, barcode=f"bc-{uid}", quantity=qty,
                         raw_status="new", order_date=day)


# ------------------------------------------------ проведение заказов

def test_orders_since_the_date_are_applied(db):
    account = make_account(db, Platform.wb)
    product = _product(db)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    client = FakeClient([_order("o1", qty=2), _order("o2", qty=3)])

    stats = catch_up_product(db, product, lambda d, aid: client, _wh)

    assert stats["applied"] == 2
    db.refresh(product)
    assert product.stock_on_hand == 15          # 20 − 2 − 3


def test_movements_for_1c_are_created_with_the_real_order_date(db):
    """Ради этого всё и делается: в 1С должны появиться перемещения, и датами
    самих отгрузок, а не сегодняшним числом."""
    account = make_account(db, Platform.wb)
    product = _product(db)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    client = FakeClient([_order("o1", day=date(2026, 8, 11))])

    catch_up_product(db, product, lambda d, aid: client, _wh)

    task = db.query(FtpTask).filter(FtpTask.order_id == "o1").first()
    assert task is not None
    assert task.movement_date == date(2026, 8, 11)


def test_nothing_is_transmitted_to_the_platforms(db):
    """Требование заказчика дословно: до включения трансляции на площадку не
    уходит ни ноль, ни какой-либо остаток."""
    account = make_account(db, Platform.wb)
    other = make_account(db, Platform.ozon, name="Другой")
    product = _product(db, broadcast=False)
    for a in (account, other):
        db.add(SyncSetting(uid_1c="u1", account_id=a.id, enabled=True))
    db.commit()
    client = FakeClient([_order("o1", qty=2)])

    catch_up_product(db, product, lambda d, aid: client, _wh)

    assert db.query(DispatchQueueItem).count() == 0
    assert client.pushed == []


def test_running_it_twice_does_not_double_the_orders(db):
    """Повторный запуск безопасен: идемпотентность по ProcessedOrder."""
    account = make_account(db, Platform.wb)
    product = _product(db)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    client = FakeClient([_order("o1", qty=2)])

    catch_up_product(db, product, lambda d, aid: client, _wh)
    second = catch_up_product(db, product, lambda d, aid: client, _wh)

    assert second["applied"] == 0 and second["skipped"] == 1
    db.refresh(product)
    assert product.stock_on_hand == 18          # списано один раз


def test_moving_the_date_back_pulls_older_orders(db):
    """Подтверждено заказчиком как правильное поведение: сдвинули дату назад —
    подтянулись более старые заказы, перемещения на них создались."""
    account = make_account(db, Platform.wb)
    product = _product(db, day=date(2026, 8, 10))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    client = FakeClient([_order("старый", day=date(2026, 7, 1)),
                         _order("новый", day=date(2026, 8, 11))])

    first = catch_up_product(db, product, lambda d, aid: client, _wh)
    assert first["applied"] == 1                # старый не попал в окно

    product.offset_base_date = date(2026, 6, 1)
    db.commit()
    second = catch_up_product(db, product, lambda d, aid: client, _wh)

    assert second["applied"] == 1               # догнали старый
    assert db.query(ProcessedOrder).count() == 2


# ------------------------------------------------ отметка «актуализирован»

def test_a_clean_run_marks_the_product(db):
    account = make_account(db, Platform.wb)
    product = _product(db)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    catch_up_product(db, product, lambda d, aid: FakeClient([_order("o1")]), _wh)

    assert product.recalc_done_at is not None


def test_no_orders_found_still_counts_as_up_to_date(db):
    """Заказов за период не было — значит остаток и так актуален."""
    account = make_account(db, Platform.wb)
    product = _product(db)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    catch_up_product(db, product, lambda d, aid: FakeClient([]), _wh)

    assert product.recalc_done_at is not None


def test_a_platform_error_leaves_the_product_unmarked(db):
    """Площадка не ответила — посмотреть заказы мы не смогли. Называть такой
    товар актуализированным нельзя: оператор включит трансляцию вслепую."""
    account = make_account(db, Platform.wb)
    product = _product(db)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    class Broken:
        def get_orders_since(self, since):
            raise RuntimeError("таймаут")

    stats = catch_up_product(db, product, lambda d, aid: Broken(), _wh)

    assert product.recalc_done_at is None
    assert stats["problems"]


def test_a_product_without_barcodes_is_not_marked(db):
    product = Product(uid_1c="u2", article="A", name="Без баркода", stock_on_hand=5,
                      offset_base_date=DAY, offset_base_stock=5)
    db.add(product)
    db.commit()

    stats = catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)

    assert product.recalc_done_at is None
    assert "баркод" in "; ".join(stats["problems"])


def test_a_product_without_a_date_is_refused(db):
    product = Product(uid_1c="u3", article="A", name="Без даты", stock_on_hand=5)
    db.add(product)
    db.commit()

    stats = catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)

    assert "дата" in "; ".join(stats["problems"])


# ------------------------------------------------ задание и порции

def test_the_job_walks_through_everything_in_chunks(db):
    """Задание идёт порциями: база — SQLite, в которую в это же время пишут опрос
    заказов и рассылка, и держать её занятой минутами нельзя."""
    account = make_account(db, Platform.wb)
    products = []
    for i in range(7):
        p = _product(db, uid=f"u{i}")
        db.add(SyncSetting(uid_1c=p.uid_1c, account_id=account.id, enabled=True))
        products.append(p)
    db.commit()
    create_job(db, products, "admin")
    db.commit()

    first = run_tick(db, lambda d, aid: FakeClient(), _wh, limit=5)
    assert first["processed"] == 5

    run_tick(db, lambda d, aid: FakeClient(), _wh, limit=5)
    job = db.query(RecalcJob).first()
    assert job.processed == 7

    run_tick(db, lambda d, aid: FakeClient(), _wh, limit=5)   # закрывающий проход
    db.refresh(job)
    assert job.status == RecalcStatus.done
    assert job.finished_at is not None


def test_no_job_means_no_work(db):
    assert run_tick(db, lambda d, aid: FakeClient(), _wh) == {"job": None}


def test_a_failed_product_is_counted_but_does_not_stop_the_job(db):
    account = make_account(db, Platform.wb)
    good = _product(db, uid="good")
    bad = Product(uid_1c="bad", article="A", name="Без баркода", stock_on_hand=5,
                  offset_base_date=DAY, offset_base_stock=5)
    db.add(bad)
    db.add(SyncSetting(uid_1c="good", account_id=account.id, enabled=True))
    db.commit()
    create_job(db, [bad, good], "admin")
    db.commit()

    run_tick(db, lambda d, aid: FakeClient(), _wh, limit=5)

    job = db.query(RecalcJob).first()
    assert job.processed == 2
    assert job.failed_items == 1
    assert db.query(Product).filter(Product.uid_1c == "good").first().recalc_done_at is not None


def test_the_selection_is_frozen_when_the_job_is_created(db):
    """Отбор фиксируется в момент создания: пока задание стоит в очереди, фильтр
    мог бы начать подходить другим товарам, и обработалось бы не то, что человек
    видел на экране."""
    a, b = _product(db, uid="u1"), _product(db, uid="u2")
    create_job(db, [a], "admin")
    db.commit()

    uids = [i.uid_1c for i in db.query(RecalcItem).all()]
    assert uids == ["u1"]


def test_only_one_job_runs_at_a_time(db):
    product = _product(db)
    create_job(db, [product], "admin")
    db.commit()

    assert active_job(db) is not None

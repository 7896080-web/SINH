"""Ответ 1С на выгрузку по дате достаётся товарам, которые его ждали.

Между «оператор задал дату» и «стало известно, сколько было» проходит до десяти
минут: обработка 1С запускается своим расписанием. Всё это время товар живёт с
пустым остатком на дату, и порог посчитать не из чего.

Подставить остаток обязан тот, кто принимает файл ответа. Иначе оператор задал
бы дату, ответ пришёл бы, и ничего не произошло — до тех пор, пока строку не
тронут руками. На каталоге в 152 тысячи SKU это означало бы, что массовая
простановка даты не работает вовсе.
"""
from datetime import date

from app.models import (DispatchQueueItem, Platform, Product, StockDateRow, StockDateSnapshot,
                        StockDateStatus, SyncSetting)
from app.offset_base import fill_waiting_products, set_base_date, stock_at_date
from tests.factories import make_account

DAY = date(2026, 8, 7)


def _snapshot(db, day=DAY, status=StockDateStatus.done, rows=()):
    snap = StockDateSnapshot(snapshot_date=day, status=status, rows_count=len(rows))
    db.add(snap)
    db.commit()
    db.refresh(snap)
    for uid, qty in rows:
        db.add(StockDateRow(snapshot_id=snap.id, uid_1c=uid, quantity=qty))
    db.commit()
    return snap


def _product(db, uid="u1", **kw):
    base = dict(uid_1c=uid, article="ART", name="Товар", stock_on_hand=10, reserve=0,
                broadcast_enabled=True)
    base.update(kw)
    p = Product(**base)
    db.add(p)
    db.commit()
    return p


# ------------------------------------------------ остаток на дату

def test_stock_at_date_reads_the_snapshot(db):
    _snapshot(db, rows=[("u1", 10), ("u2", 3)])

    assert stock_at_date(db, "u1", DAY) == 10


def test_product_absent_from_the_snapshot_counts_as_zero(db):
    """Решение заказчика: товара нет в выгрузке — значит на эту дату его на
    складе не было, считаем нулём и даём включить трансляцию."""
    _snapshot(db, rows=[("u2", 3)])

    assert stock_at_date(db, "u1", DAY) == 0


def test_no_finished_snapshot_is_not_a_zero(db):
    """Разница, ради которой всё это существует: «ещё не пришло» и «было ноль» —
    разные вещи. Спутать их значит посчитать порог по несуществующему ответу."""
    _snapshot(db, status=StockDateStatus.sent, rows=[("u1", 10)])

    assert stock_at_date(db, "u1", DAY) is None


def test_newer_snapshot_for_the_same_day_wins(db):
    """Выгрузку на одно число заказали дважды — верна свежая: между ответами в
    1С могли провести документы задним числом."""
    _snapshot(db, rows=[("u1", 10)])
    _snapshot(db, rows=[("u1", 6)])

    assert stock_at_date(db, "u1", DAY) == 6


# ------------------------------------------------ дата задана заранее

def test_setting_a_date_before_the_answer_leaves_the_product_waiting(db):
    p = _product(db)

    set_base_date(db, p, DAY)

    assert p.offset_base_date == DAY
    assert p.offset_base_stock is None
    assert p.broadcast_offset is None          # считать не из чего — и не считаем


def test_setting_a_date_after_the_answer_computes_at_once(db):
    _snapshot(db, rows=[("u1", 10)])
    p = _product(db, reserve=2, fact_at_date=8)

    set_base_date(db, p, DAY)

    assert p.offset_base_stock == 10
    assert p.broadcast_offset == 4


def test_clearing_the_date_drops_the_fact_but_keeps_the_threshold(db):
    """Факт привязан к конкретной дате — без неё он бессмыслен. А порог стирать
    нельзя: это молча вернуло бы на площадки полный остаток."""
    _snapshot(db, rows=[("u1", 10)])
    p = _product(db, reserve=2, fact_at_date=8)
    set_base_date(db, p, DAY)

    set_base_date(db, p, None)

    assert p.offset_base_date is None
    assert p.fact_at_date is None
    assert p.broadcast_offset == 4


# ------------------------------------------------ ответ пришёл — доделываем

def test_arriving_answer_fills_everyone_who_waited(db):
    waiting = _product(db, uid="u1", reserve=2, fact_at_date=8)
    set_base_date(db, waiting, DAY)
    snap = _snapshot(db, rows=[("u1", 10)])

    stats = fill_waiting_products(db, snap)

    assert stats["filled"] == 1
    assert waiting.offset_base_stock == 10
    assert waiting.broadcast_offset == 4


def test_a_product_missing_from_the_answer_gets_zero(db):
    waiting = _product(db, uid="u1", reserve=3)
    set_base_date(db, waiting, DAY)
    snap = _snapshot(db, rows=[("u2", 5)])

    fill_waiting_products(db, snap)

    assert waiting.offset_base_stock == 0
    assert waiting.broadcast_offset == 3       # порог равен брони — как в авторежиме


def test_products_waiting_for_another_day_are_left_alone(db):
    other = _product(db, uid="u1")
    set_base_date(db, other, date(2026, 7, 1))
    snap = _snapshot(db, rows=[("u1", 10)])

    stats = fill_waiting_products(db, snap)

    assert stats["filled"] == 0
    assert other.offset_base_stock is None


def test_an_already_filled_product_is_not_overwritten(db):
    """У кого остаток уже подставлен — тот получил своё число раньше. Переписать
    его свежим снимком значит поменять порог под оператором без его ведома."""
    _snapshot(db, rows=[("u1", 10)])
    p = _product(db, uid="u1", reserve=2, fact_at_date=8)
    set_base_date(db, p, DAY)
    assert p.broadcast_offset == 4

    later = _snapshot(db, rows=[("u1", 99)])
    stats = fill_waiting_products(db, later)

    assert stats["filled"] == 0
    assert p.offset_base_stock == 10
    assert p.broadcast_offset == 4


# ------------------------------------------------ новое число должно уехать

def test_changed_threshold_goes_into_the_dispatch_queue(db):
    """Иначе новый порог остался бы только на экране, а на площадке висело бы
    старое число — то есть расчёт был бы косметикой."""
    account = make_account(db, Platform.wb)
    p = _product(db, uid="u1", reserve=2, fact_at_date=8)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    set_base_date(db, p, DAY)
    snap = _snapshot(db, rows=[("u1", 10)])

    stats = fill_waiting_products(db, snap)
    db.commit()

    assert stats["queued"] == 1
    queued = db.query(DispatchQueueItem).all()
    assert len(queued) == 1
    assert queued[0].account_id == account.id


def test_unchecked_account_gets_nothing(db):
    account = make_account(db, Platform.wb)
    p = _product(db, uid="u1", reserve=2, fact_at_date=8)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=False))
    db.commit()
    set_base_date(db, p, DAY)
    snap = _snapshot(db, rows=[("u1", 10)])

    stats = fill_waiting_products(db, snap)
    db.commit()

    assert stats["queued"] == 0
    assert db.query(DispatchQueueItem).count() == 0

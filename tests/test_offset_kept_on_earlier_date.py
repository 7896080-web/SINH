"""Сдвиг даты НАЗАД сохраняет уже установленный порог.

Дата расчёта служит двум разным целям сразу: от неё считается порог (дата +
остаток ЦС на дату + факт) и от неё же расчёт поднимает заказы площадок. Чтобы
догнать продажи за более ранний период, дату приходится двигать назад — и вместе
с заказами терялся порог: факт на прежнюю дату справедливо стирается, а без
факта формула даёт просто бронь. То есть работа, сделанная СВЕЖИМ физическим
пересчётом склада, пропадала ради того, чтобы поднять старые заказы.
"""
from datetime import date

import pytest

from app.models import (Product, StockDateRow, StockDateSnapshot, StockDateStatus)
from app.offset_base import fill_waiting_products, set_base_date
from app.transmit import offset_from_base


def _snapshot(db, day, rows):
    snap = StockDateSnapshot(snapshot_date=day, status=StockDateStatus.done,
                             rows_count=len(rows))
    db.add(snap)
    db.flush()
    for uid, qty in rows:
        db.add(StockDateRow(snapshot_id=snap.id, uid_1c=uid, quantity=qty))
    db.commit()
    db.refresh(snap)
    return snap


def _product(db, **kw):
    p = Product(uid_1c="u1", article="A-1", name="Товар", stock_on_hand=10,
                broadcast_enabled=True, **kw)
    db.add(p)
    db.commit()
    return p


# --------------------------------------------------------------------------
# Случай оператора, ради которого всё и делалось
# --------------------------------------------------------------------------

def test_the_threshold_survives_a_step_back_in_time(db):
    """Сценарий с боя, в его собственных числах.

    Расчёт на 10.09: 1С показала 10, физически пересчитали 5 — порог 5. Теперь
    нужен расчёт с 10.08, чтобы поднять заказы за месяц, и порог 5 обязан
    остаться: он получен настоящим пересчётом склада, и более точного числа у
    нас нет и не будет — склад в прошлом не пересчитать.
    """
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db)

    set_base_date(db, product, date(2026, 9, 10))
    product.fact_at_date = 5
    from app.transmit import recompute_offset
    recompute_offset(product)
    db.commit()
    assert product.broadcast_offset == 5, "порог по пересчёту 10.09"

    set_base_date(db, product, date(2026, 8, 10))
    db.commit()

    assert product.offset_base_date == date(2026, 8, 10)
    assert product.broadcast_offset == 5, "порог обязан пережить сдвиг назад"
    # Факт подобран под новую дату: при том же расхождении в 5 единиц на 10.08
    # при учётных 30 на складе лежало бы 25.
    assert product.fact_at_date == 25
    assert offset_from_base(product) == 5, "связка снова согласована"


def test_the_recalc_mark_still_goes_away(db):
    """Заказы за добавившийся период обязаны быть подняты заново.

    Сохранение порога не отменяет главного: расчёт проводил заказы ОТ ПРЕЖНЕЙ
    даты, и к новому периоду его вывод не относится. Оставь мы отметку — остаток
    уехал бы наружу завышенным ровно на непроведённые продажи.
    """
    from app.timeutils import now_utc

    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db, recalc_done_at=now_utc(), recalc_account_ids="1")

    set_base_date(db, product, date(2026, 9, 10))
    product.fact_at_date = 5
    product.recalc_done_at = now_utc()
    product.recalc_account_ids = "1"
    db.commit()

    set_base_date(db, product, date(2026, 8, 10))
    db.commit()

    assert product.recalc_done_at is None
    assert product.recalc_account_ids == ""


def test_a_reserve_change_still_moves_the_threshold(db):
    """Главная причина подбирать ФАКТ, а не просто «запретить пересчёт».

    Запрети мы пересчёт — три исходных числа перестали бы соответствовать
    сохранённому порогу, и первая же правка брони вернула бы его к броне молча.
    Бронь меняют чаще всего остального, так что ждать пришлось бы недолго.
    """
    from app.transmit import recompute_offset

    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db, reserve=0)

    set_base_date(db, product, date(2026, 9, 10))
    product.fact_at_date = 5
    recompute_offset(product)
    set_base_date(db, product, date(2026, 8, 10))
    db.commit()
    assert product.broadcast_offset == 5

    product.reserve = 3
    recompute_offset(product)
    db.commit()

    assert product.broadcast_offset == 8, "порог сдвинулся ровно на изменение брони"


# --------------------------------------------------------------------------
# Границы
# --------------------------------------------------------------------------

def test_moving_forward_still_clears_the_fact(db):
    """Вперёд дату двигают ПОСЛЕ нового пересчёта склада.

    Там верно прежнее правило: факт всегда «на дату», старое число к новому
    отношения не имеет. Сохранять порог здесь значило бы отменить работу,
    которую оператор как раз и пришёл сделать.
    """
    from app.transmit import recompute_offset

    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    product = _product(db)

    set_base_date(db, product, date(2026, 8, 10))
    product.fact_at_date = 25
    recompute_offset(product)
    db.commit()
    assert product.broadcast_offset == 5

    set_base_date(db, product, date(2026, 9, 10))
    db.commit()

    assert product.fact_at_date is None
    assert product.broadcast_offset == 0, "10 − (10 − 0): сводится к брони"


def test_nothing_to_keep_when_the_warehouse_was_never_counted(db):
    """Порог без факта равен просто броне — удерживать нечего.

    Формально он «установлен», но поставил его не человек, а формула при
    отсутствии факта. Подставить такой строке выведенный факт значило бы создать
    видимость физического пересчёта, которого не было, — а на новой дате порог и
    так выйдет тем же самым.
    """
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db)

    set_base_date(db, product, date(2026, 9, 10))
    db.commit()
    set_base_date(db, product, date(2026, 8, 10))
    db.commit()

    assert product.fact_at_date is None
    assert product.offset_pinned is None


# --------------------------------------------------------------------------
# Отложенный случай: снимка на новую дату ещё нет
# --------------------------------------------------------------------------

def test_the_threshold_is_kept_even_when_1c_has_not_answered_yet(db):
    """Самый частый случай на бою: срез на старую дату ещё не заказан.

    Порог переживает саму смену даты сам собой (без остатка формула не
    считается), а вот через час, когда придёт ответ 1С, он тихо сменился бы на
    бронь — то есть уже после того, как оператор увидел, что всё в порядке.
    """
    from app.transmit import recompute_offset

    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    product = _product(db)
    set_base_date(db, product, date(2026, 9, 10))
    product.fact_at_date = 5
    recompute_offset(product)
    db.commit()
    assert product.broadcast_offset == 5

    # Снимка на 10.08 ещё нет — строка встаёт в ожидание.
    set_base_date(db, product, date(2026, 8, 10))
    db.commit()
    assert product.offset_base_stock is None
    assert product.offset_pinned == 5, "намерение записано"
    assert product.broadcast_offset == 5

    snap = _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    stats = fill_waiting_products(db, snap)

    assert stats["offsets_kept"] == 1
    assert product.broadcast_offset == 5
    assert product.fact_at_date == 25
    assert product.offset_pinned is None, "намерение снято, второй раз не сработает"


def test_an_impossible_threshold_is_reported_not_swallowed(db):
    """Подобранный факт был бы отрицательным — прежний порог на эту дату невозможен.

    Молча оставить порог нельзя (он перестал бы соответствовать трём числам), и
    молча сменить тоже: оператор просил сохранить. Поэтому такие строки
    считаются отдельно.
    """
    from app.transmit import recompute_offset

    _snapshot(db, date(2026, 9, 10), [("u1", 100)])
    product = _product(db, reserve=0)
    set_base_date(db, product, date(2026, 9, 10))
    product.fact_at_date = 0
    recompute_offset(product)
    db.commit()
    assert product.broadcast_offset == 100

    set_base_date(db, product, date(2026, 8, 10))
    db.commit()
    snap = _snapshot(db, date(2026, 8, 10), [("u1", 1)])
    stats = fill_waiting_products(db, snap)

    assert stats["offsets_lost"] == 1
    assert stats["offsets_kept"] == 0
    assert product.offset_pinned is None, "не должно звенеть при каждом снимке"

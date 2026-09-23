"""Порог переживает ЛЮБУЮ смену даты расчёта, потому что держится не на ней.

Дата расчёта служит двум разным целям сразу: от неё расчёт поднимает заказы
площадок, и от неё же когда-то считался порог. Отсюда и была вся беда: чтобы
догнать продажи за более ранний период, дату двигают назад, факт на прежнюю
дату справедливо стирается — и порог схлопывался до брони, то есть работа,
сделанная физическим пересчётом склада, пропадала ради старых заказов. 23.09 на
бою так схлопнулись пороги у 62 товаров, и наружу поехало на 418 штук больше,
чем есть.

Лечили это подбором ФАКТА под сохранённый порог (`pin_offset`), и лекарство
оказалось частью болезни: подобранное число оператор видит как измерение,
которого не делал, и «поправляет» на учётное — а факт, равный учёту, означает
«расхождения нет».

Теперь носитель — не дата, а сам товар: `Product.stock_discrepancy`, `учёт 1С −
сколько лежит на самом деле`. Порог = расхождение + бронь, дата к нему
отношения не имеет.
"""
from datetime import date

from app.models import Product, StockDateRow, StockDateSnapshot, StockDateStatus
from app.offset_base import apply_fact, fill_waiting_products, set_base_date
from app.transmit import offset_from_base, recompute_offset


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


def _measure(db, product, day, fact):
    """Штатный путь оператора: задать дату и вписать пересчитанный склад."""
    set_base_date(db, product, day)
    apply_fact(db, product, fact)
    recompute_offset(product)
    db.commit()


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

    _measure(db, product, date(2026, 9, 10), 5)
    assert product.broadcast_offset == 5, "порог по пересчёту 10.09"
    assert product.stock_discrepancy == 5

    set_base_date(db, product, date(2026, 8, 10))
    db.commit()

    assert product.offset_base_date == date(2026, 8, 10)
    assert product.broadcast_offset == 5, "порог обязан пережить сдвиг назад"
    assert product.stock_discrepancy == 5, "расхождение — свойство товара, не даты"
    # Факт стирается, как и прежде: он всегда «факт НА ДАТУ», и пересчитанное
    # 10.09 количество ничего не говорит о складе на 10.08. Подставлять сюда
    # выведенное число нельзя — оператор примет его за измерение.
    assert product.fact_at_date is None
    assert offset_from_base(product) == 5


def test_moving_the_date_forward_keeps_it_too(db):
    """Вперёд — то же самое, и это ТРЕБОВАНИЕ, а не побочный эффект.

    «Сохраняется на любую дату расчёта» — дословно. Вперёд дату двигают после
    нового пересчёта склада, и новое измерение расхождение перебьёт; но пока его
    не сделали, держать последнее измеренное безопаснее, чем обнулять: порог
    выше — наружу уходит меньше.
    """
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    product = _product(db)

    _measure(db, product, date(2026, 8, 10), 25)
    assert product.broadcast_offset == 5

    set_base_date(db, product, date(2026, 9, 10))
    db.commit()

    assert product.fact_at_date is None
    assert product.broadcast_offset == 5


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

    _measure(db, product, date(2026, 9, 10), 5)
    product.recalc_done_at = now_utc()
    product.recalc_account_ids = "1"
    db.commit()

    set_base_date(db, product, date(2026, 8, 10))
    db.commit()

    assert product.recalc_done_at is None
    assert product.recalc_account_ids == ""


def test_a_reserve_change_still_moves_the_threshold(db):
    """Бронь двигает порог ровно на себя — и после смены даты тоже.

    Это и есть смысл формулы `порог = расхождение + бронь`: сначала не отдаём
    то, чего на складе нет, потом не отдаём то, что держим у себя. Запрети мы
    пересчёт ради сохранения порога — первая же правка брони вернула бы его к
    броне молча, а бронь меняют чаще всего остального.
    """
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db, reserve=0)

    _measure(db, product, date(2026, 9, 10), 5)
    set_base_date(db, product, date(2026, 8, 10))
    db.commit()
    assert product.broadcast_offset == 5

    product.reserve = 3
    recompute_offset(product)
    db.commit()

    assert product.broadcast_offset == 8, "порог сдвинулся ровно на изменение брони"


# --------------------------------------------------------------------------
# Что именно перезаписывает расхождение
# --------------------------------------------------------------------------

def test_a_fact_equal_to_the_1c_number_does_not_erase_the_discrepancy(db):
    """Главное правило всего механизма, и стоило оно 62 товаров.

    Оператор сдвинул дату назад, увидел пустое поле факта и вписал туда учётное
    число — другого он на прошлую дату не знает. Прежним поведением это значило
    «расхождения нет»: порог схлопывался до брони, и наружу уходил остаток,
    завышенный ровно на расхождение.

    Факт, РАВНЫЙ учёту, — это «мне нечего возразить цифре 1С», а не «я пересчитал
    склад и он сошёлся». Второе говорится отдельно и вслух: расхождение 0.
    """
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db)

    _measure(db, product, date(2026, 9, 10), 5)
    set_base_date(db, product, date(2026, 8, 10))
    db.commit()

    apply_fact(db, product, 30)          # учёт на 10.08 — ровно 30
    recompute_offset(product)
    db.commit()

    assert product.fact_at_date == 30, "факт записан: человек его ввёл"
    assert product.stock_discrepancy == 5, "а расхождение НЕ тронуто"
    assert product.broadcast_offset == 5


def test_a_new_measurement_replaces_the_old_one(db):
    """Новый пересчёт склада перебивает прежнее расхождение — на то он и новый."""
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 9, 12), [("u1", 50)])
    product = _product(db)

    _measure(db, product, date(2026, 9, 10), 5)
    assert product.stock_discrepancy == 5

    _measure(db, product, date(2026, 9, 12), 45)

    assert product.stock_discrepancy == 5, "50 − 45 = 5"
    assert product.broadcast_offset == 5

    _measure(db, product, date(2026, 9, 12), 44)
    assert product.stock_discrepancy == 6, "новое измерение заменило прежнее"
    assert product.broadcast_offset == 6


def test_the_discrepancy_survives_a_negative_sign(db):
    """На складе БОЛЬШЕ, чем знает 1С. На бою таких товаров 53, до −213.

    Отвергать знак нельзя: это законное состояние склада, и порог по такому
    товару отрицательный, то есть наружу уходит больше учётного остатка —
    осознанно, потому что физически товар есть.
    """
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db)

    _measure(db, product, date(2026, 9, 10), 14)
    assert product.stock_discrepancy == -4
    assert product.broadcast_offset == -4

    set_base_date(db, product, date(2026, 8, 10))
    db.commit()
    assert product.broadcast_offset == -4, "знак переживает смену даты"


# --------------------------------------------------------------------------
# Отложенный случай: снимка на новую дату ещё нет
# --------------------------------------------------------------------------

def test_the_threshold_is_kept_even_when_1c_has_not_answered_yet(db):
    """Самый частый случай на бою: срез на старую дату ещё не заказан.

    Раньше здесь была отдельная колонка намерения (`offset_pinned`): порог
    держался до ответа 1С, а в момент ответа под него подбирался факт. Теперь
    держать нечего — расхождение уже на товаре, и ответ 1С ничего в нём не
    меняет. Проверяем именно это: и до ответа, и после порог тот же.
    """
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    product = _product(db)
    _measure(db, product, date(2026, 9, 10), 5)
    assert product.broadcast_offset == 5

    # Снимка на 10.08 ещё нет — строка встаёт в ожидание.
    set_base_date(db, product, date(2026, 8, 10))
    db.commit()
    assert product.offset_base_stock is None
    assert product.broadcast_offset == 5

    snap = _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    stats = fill_waiting_products(db, snap)

    assert stats["filled"] == 1
    assert product.offset_base_stock == 30
    assert product.broadcast_offset == 5, "ответ 1С порог не сдвинул"
    assert product.stock_discrepancy == 5


def test_nothing_is_invented_when_the_warehouse_was_never_counted(db):
    """Склад не пересчитывали — расхождения нет, и выдумывать его нечем.

    Это строки, настроенные до появления колонки, и просто строки без факта:
    порог у них равен броне, и на новой дате он выйдет таким же. Главное, чтобы
    смена даты не записала им ноль: ноль — утверждение «склад сошёлся», а мы
    про этот склад не знаем ничего.
    """
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db)

    set_base_date(db, product, date(2026, 9, 10))
    db.commit()
    set_base_date(db, product, date(2026, 8, 10))
    db.commit()

    assert product.fact_at_date is None
    assert product.stock_discrepancy is None, "«не измеряли» — это не ноль"


# --------------------------------------------------------------------------
# История: число уходит на площадки, и его происхождение обязано быть видно
# --------------------------------------------------------------------------

def test_every_change_leaves_a_trace(db):
    """Откуда здесь это число — вопрос, который однажды зададут.

    23.09 на него отвечать было нечем: подобранный факт выглядел как измерение,
    и разобрать, кто его поставил, можно было только по времени в журнале
    действий — куда массовые пути построчно не пишут вовсе.
    """
    from app.models import DiscrepancySource, StockDiscrepancyLog

    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    product = _product(db)
    _measure(db, product, date(2026, 9, 10), 5)

    rows = db.query(StockDiscrepancyLog).filter(
        StockDiscrepancyLog.uid_1c == "u1").all()
    assert len(rows) == 1
    assert rows[0].old_value is None and rows[0].new_value == 5
    assert rows[0].source == DiscrepancySource.fact
    # Контекст измерения — без него «стало 5» через месяц не перепроверить.
    assert rows[0].base_date == date(2026, 9, 10)
    assert rows[0].base_stock == 10
    assert rows[0].fact == 5


def test_an_unchanged_value_writes_no_history(db):
    """Повторный импорт того же файла и правка брони идут пачками.

    Запись «было 5, стало 5» на каждую из них утопила бы настоящие правки — то
    есть история перестала бы отвечать на вопрос, ради которого заведена.
    """
    from app.models import StockDiscrepancyLog

    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    product = _product(db)
    _measure(db, product, date(2026, 9, 10), 5)
    _measure(db, product, date(2026, 9, 10), 5)
    apply_fact(db, product, 5)
    db.commit()

    assert db.query(StockDiscrepancyLog).filter(
        StockDiscrepancyLog.uid_1c == "u1").count() == 1

"""Расхождение учёта со складом: примеры в разных вариантах.

Один файл — один вопрос: «что произойдёт с порогом, если сделать вот так».
Механику проверяют соседние файлы; здесь собраны СЛУЧАИ, каждый со своими
числами, потому что именно на числах ловятся ошибки в правилах. Все они пришли
с боя или из разбора 23.09.

Правило, из которого всё выводится:

    расхождение = учёт 1С − сколько лежит на самом деле   (хранится на товаре)
    порог       = расхождение + бронь                     (уходит в отправку)
    наружу      = max(0, остаток ЦС − порог)

Расхождение постоянно по смыслу: учёт врёт на одну и ту же величину, пока склад
не пересчитали заново. Поэтому дата к нему отношения не имеет, и меняют его
ровно три события — новое измерение (факт, ОТЛИЧНЫЙ от учёта), прямая правка
числа (строка, файл) и задание порога (обратным счётом). Всё остальное его не
трогает.
"""
from datetime import date

import pytest

from app.models import (DiscrepancySource, Product, StockDateRow,
                        StockDateSnapshot, StockDateStatus, StockDiscrepancyLog)
from app.offset_base import (apply_fact, apply_offset, clear_offset,
                             set_base_date, set_discrepancy)
from app.transmit import recompute_offset, sku_quantity

SEP = date(2026, 9, 10)
AUG = date(2026, 8, 9)
LATER = date(2026, 9, 12)


def _snapshot(db, day, rows):
    snap = StockDateSnapshot(snapshot_date=day, status=StockDateStatus.done,
                             rows_count=len(rows))
    db.add(snap)
    db.flush()
    for uid, qty in rows:
        db.add(StockDateRow(snapshot_id=snap.id, uid_1c=uid, quantity=qty))
    db.commit()
    return snap


def _product(db, uid="u1", stock=22, reserve=0):
    p = Product(uid_1c=uid, article="27643", name="Свитшот", size="L",
                color="LACIVERT/RED", stock_on_hand=stock, reserve=reserve,
                broadcast_enabled=True)
    db.add(p)
    db.commit()
    return p


def _measure(db, product, day, fact):
    """Штатный путь: задать дату расчёта и вписать пересчитанный склад."""
    set_base_date(db, product, day)
    apply_fact(db, product, fact, username="operator")
    recompute_offset(product)
    db.commit()


# ===========================================================================
# 1. Пример оператора целиком, в его собственных числах (27643 LACIVERT/RED)
# ===========================================================================

def test_the_operators_own_example_end_to_end(db):
    """10.09 пересчёт: в 1С 43, на складе 32. Потом нужны заказы с 09.08.
    Потом 12.09 нашли ещё 5 штук.

    Шаг за шагом, ровно как описал оператор:

      10.09  учёт 43, факт 32  → расхождение 11, порог 11
      09.08  дата назад        → расхождение 11 (не шелохнулось), порог 11
      09.08  факт 43 = учёту   → расхождение 11 (ввод не является измерением)
      12.09  учёт 50, факт 45  → расхождение 5, порог 5
    """
    _snapshot(db, SEP, [("u1", 43)])
    _snapshot(db, AUG, [("u1", 43)])
    _snapshot(db, LATER, [("u1", 50)])
    product = _product(db, stock=22)

    _measure(db, product, SEP, 32)
    assert (product.stock_discrepancy, product.broadcast_offset) == (11, 11)
    assert sku_quantity(product) == 11, "наружу 22 − 11"

    set_base_date(db, product, AUG)
    db.commit()
    assert (product.stock_discrepancy, product.broadcast_offset) == (11, 11), \
        "дату двигали ради заказов, а не ради склада"

    # Склада на 09.08 оператор не знает и вписывает учётное число.
    apply_fact(db, product, 43, username="operator")
    recompute_offset(product)
    db.commit()
    assert (product.stock_discrepancy, product.broadcast_offset) == (11, 11), \
        "«нечего возразить цифре 1С» — это не «склад сошёлся»"

    _measure(db, product, LATER, 45)
    assert (product.stock_discrepancy, product.broadcast_offset) == (5, 5), \
        "новый пересчёт перебил прежнее измерение"


# ===========================================================================
# 2. Что МЕНЯЕТ расхождение и что НЕ меняет — по одному варианту на событие
# ===========================================================================

@pytest.mark.parametrize("name,base,fact,expected", [
    # Учёт больше склада — самый частый случай: недостача, пересортица.
    ("недостача",            43, 32,  11),
    # Учёт меньше склада. На бою таких 53 товара, до −213: товар физически есть,
    # а в 1С его нет, и наружу по ним осознанно уходит БОЛЬШЕ учётного остатка.
    ("излишек",              43, 50,  -7),
    # Склада нет вовсе, а в 1С он числится: расхождение равно всему учёту, порог
    # его съедает целиком, наружу не уходит ничего.
    ("на складе пусто",      43,  0,  43),
    # Учёт нулевой, а товар лежит: расхождение отрицательное.
    ("в 1С пусто",            0, 12, -12),
])
def test_a_measurement_writes_the_discrepancy(db, name, base, fact, expected):
    """Факт, ОТЛИЧНЫЙ от учёта, — это измерение, и оно записывается."""
    _snapshot(db, SEP, [("u1", base)])
    product = _product(db)

    _measure(db, product, SEP, fact)

    assert product.stock_discrepancy == expected, name
    assert product.broadcast_offset == expected, "бронь ноль — порог равен расхождению"


@pytest.mark.parametrize("name,action", [
    ("факт равен учёту",   lambda db, p: apply_fact(db, p, p.offset_base_stock)),
    ("факт сняли",         lambda db, p: apply_fact(db, p, None)),
    ("дату двинули назад", lambda db, p: set_base_date(db, p, AUG)),
    ("дату двинули вперёд", lambda db, p: set_base_date(db, p, LATER)),
    ("дату сняли вовсе",   lambda db, p: set_base_date(db, p, None)),
    ("бронь изменили",     lambda db, p: (setattr(p, "reserve", 4),
                                          recompute_offset(p))),
])
def test_what_does_not_touch_the_discrepancy(db, name, action):
    """Всё это к складу отношения не имеет — значит и к расхождению тоже.

    Каждая строка здесь когда-то стирала бы порог: смена даты стирала факт, а
    вместе с ним и выведенное из него расхождение. 23.09 так схлопнулись пороги
    у 62 товаров, и наружу поехало на 418 штук больше, чем есть.
    """
    _snapshot(db, SEP, [("u1", 43)])
    _snapshot(db, AUG, [("u1", 40)])
    _snapshot(db, LATER, [("u1", 50)])
    product = _product(db)
    _measure(db, product, SEP, 32)
    assert product.stock_discrepancy == 11

    action(db, product)
    recompute_offset(product)
    db.commit()

    assert product.stock_discrepancy == 11, name
    assert product.broadcast_offset == 11 + (product.reserve or 0)


@pytest.mark.parametrize("name,write", [
    ("правка числа руками", lambda db, p: set_discrepancy(
        db, p, 6, source=DiscrepancySource.manual, username="operator")),
    # Задание порога — тот же акт, только обратным счётом: порог 6 при брони 0
    # означает расхождение 6.
    ("задание порога",      lambda db, p: apply_offset(db, p, 6, username="operator")),
])
def test_an_explicit_edit_replaces_it(db, name, write):
    """Сказанное вслух сильнее выведенного: человек знает про склад больше нас."""
    _snapshot(db, SEP, [("u1", 43)])
    product = _product(db)
    _measure(db, product, SEP, 32)

    write(db, product)
    recompute_offset(product)
    db.commit()

    assert product.stock_discrepancy == 6, name
    assert product.broadcast_offset == 6


# ===========================================================================
# 3. Ноль и «не измеряли» — разные вещи, и разница дорогая
# ===========================================================================

def test_zero_is_a_statement_and_only_a_human_makes_it(db):
    """Ноль значит «склад сошёлся с учётом»: порог сводится к брони, и наружу
    уходит всё, что числится в 1С. Сказать это может только человек — потому
    ввод факта, равного учёту, ноль и НЕ ставит."""
    _snapshot(db, SEP, [("u1", 43)])
    product = _product(db, reserve=2)
    _measure(db, product, SEP, 32)
    assert product.broadcast_offset == 13          # 11 + бронь 2

    set_discrepancy(db, product, 0, source=DiscrepancySource.manual,
                    username="operator")
    recompute_offset(product)
    db.commit()

    assert product.stock_discrepancy == 0
    assert product.broadcast_offset == 2, "остаётся только бронь"


def test_never_measured_is_not_zero(db):
    """NULL — «про этот склад мы не знаем ничего».

    Разница видна не в пороге (он в обоих случаях равен брони), а в том, что с
    такой строкой делать: ноль разбирать не надо, NULL ждёт пересчёта. И кнопка
    «Записать остаток ЦС на дату» их различает — ноль она вправе подтвердить,
    измеренное расхождение затирать не вправе.
    """
    from app.offset_base import offset_is_established

    _snapshot(db, SEP, [("u1", 43)])
    product = _product(db)
    set_base_date(db, product, SEP)
    recompute_offset(product)
    db.commit()

    assert product.stock_discrepancy is None
    assert product.broadcast_offset == 0
    assert offset_is_established(product) is False

    set_discrepancy(db, product, 0, source=DiscrepancySource.manual)
    db.commit()
    assert offset_is_established(product) is False, "ноль спорить не с чем"

    set_discrepancy(db, product, 11, source=DiscrepancySource.manual)
    db.commit()
    assert offset_is_established(product) is True


def test_only_the_reset_button_takes_the_measurement_away(db):
    """«Сбросить порог» снимает расхождение вместе с порогом — иначе ближайший
    пересчёт вернул бы порог по формуле, и кнопка выглядела бы сломанной."""
    _snapshot(db, SEP, [("u1", 43)])
    product = _product(db, reserve=2)
    _measure(db, product, SEP, 32)
    assert product.broadcast_offset == 13

    clear_offset(db, product, username="operator")
    recompute_offset(product)
    db.commit()

    assert product.stock_discrepancy is None
    assert product.broadcast_offset is None
    assert product.offset_base_date is None
    assert sku_quantity(product) == 20, "22 − бронь 2: автоматический режим"


# ===========================================================================
# 4. Порог и бронь: кто на что влияет
# ===========================================================================

@pytest.mark.parametrize("gap,reserve,stock,out", [
    (11, 0, 22, 11),      # обычная недостача
    (11, 2, 22,  9),      # бронь вычитается сверх расхождения
    (0,  2, 22, 20),      # склад сошёлся — остаётся только бронь
    (-7, 0, 22, 29),      # излишек: наружу больше учётного остатка
    (43, 0, 22,  0),      # расхождение больше остатка — не уходит ничего
])
def test_what_actually_goes_out(db, gap, reserve, stock, out):
    """Итог — то, ради чего всё считается: сколько уедет на площадку."""
    product = _product(db, stock=stock, reserve=reserve)
    product.offset_base_date = SEP
    product.offset_base_stock = 43
    set_discrepancy(db, product, gap, source=DiscrepancySource.manual)
    recompute_offset(product)
    db.commit()

    assert product.broadcast_offset == gap + reserve
    assert sku_quantity(product) == out


def test_a_reserve_edit_moves_the_threshold_but_not_the_measurement(db):
    """Бронь — «сколько держим у себя», расхождение — «насколько врёт учёт».

    Смешивать их нельзя: 23.09 на бою оператор поставил факт равным учёту
    (расхождение 0) и компенсировал бронью 11. Наружу пошло то же число, но
    собранное из другого, и при следующей правке брони эти состояния разошлись
    бы — порог поехал бы, а склад нет.
    """
    _snapshot(db, SEP, [("u1", 43)])
    product = _product(db, reserve=0)
    _measure(db, product, SEP, 32)
    assert product.broadcast_offset == 11

    product.reserve = 5
    recompute_offset(product)
    db.commit()
    assert product.broadcast_offset == 16, "порог сдвинулся ровно на бронь"
    assert product.stock_discrepancy == 11, "склад от этого не изменился"

    product.reserve = 0
    recompute_offset(product)
    db.commit()
    assert product.broadcast_offset == 11, "и вернулся ровно назад"


# ===========================================================================
# 5. История: число уходит на площадки, и его происхождение обязано быть видно
# ===========================================================================

def test_the_history_tells_where_each_number_came_from(db):
    """Вопрос «почему здесь 11» задают через месяц, а не в день правки.

    23.09 отвечать на него было нечем: подобранный под порог факт выглядел как
    измерение, а массовые пути (кнопки отбора, импорт Excel) в журнал действий
    построчно не пишут вовсе.
    """
    _snapshot(db, SEP, [("u1", 43)])
    _snapshot(db, LATER, [("u1", 50)])
    product = _product(db)

    _measure(db, product, SEP, 32)                       # измерение
    _measure(db, product, LATER, 45)                     # новое измерение
    set_discrepancy(db, product, 6, source=DiscrepancySource.manual,
                    username="operator", note="пересчитали ещё раз")
    clear_offset(db, product, username="operator")       # сброс
    db.commit()

    rows = db.query(StockDiscrepancyLog).filter(
        StockDiscrepancyLog.uid_1c == "u1").order_by(StockDiscrepancyLog.id).all()

    assert [(r.old_value, r.new_value, r.source) for r in rows] == [
        (None, 11, DiscrepancySource.fact),
        (11,    5, DiscrepancySource.fact),
        (5,     6, DiscrepancySource.manual),
        (6,  None, DiscrepancySource.reset),
    ]
    # Контекст измерения — иначе «стало 11» через месяц не перепроверить.
    assert (rows[0].base_date, rows[0].base_stock, rows[0].fact) == (SEP, 43, 32)
    assert rows[2].username == "operator" and rows[2].note == "пересчитали ещё раз"


# ===========================================================================
# 6. Ворота трансляции спрашивают «подтвердил ли человек цифру»
# ===========================================================================

def test_the_gate_accepts_a_stored_discrepancy_instead_of_a_fact(db):
    """Сдвиг даты назад не должен закрывать ворота трансляции.

    `calc_status` спрашивал один факт, а факт при смене даты стирается — значит
    каждый сдвиг назад (а его делают, чтобы догнать заказы за более ранний
    период) возвращал бы строку в «нужен факт» по товару, у которого с порогом
    всё в порядке. Вписать факт на прошлое число оператору неоткуда: склад в
    прошлом не пересчитать. Хранимое расхождение — ТА САМАЯ подтверждённая
    цифра, только снятая на другую дату.
    """
    from app.broadcast_gate import calc_status
    from app.timeutils import now_utc

    _snapshot(db, SEP, [("u1", 43)])
    _snapshot(db, AUG, [("u1", 43)])
    product = _product(db)
    _measure(db, product, SEP, 32)
    product.recalc_done_at = now_utc()
    product.recalc_account_ids = "1"
    db.commit()
    assert calc_status(product, True, {1})[0] == "ready"

    set_base_date(db, product, AUG)
    product.recalc_done_at = now_utc()      # расчёт по новому периоду прошёл
    product.recalc_account_ids = "1"
    db.commit()

    assert product.fact_at_date is None
    assert calc_status(product, True, {1})[0] == "ready", \
        "расхождение измерено — цифру человек подтвердил"


def test_the_gate_still_demands_a_fact_when_nothing_was_ever_measured(db):
    """Обратная сторона: без единого измерения строка не «обработана».

    Порог у неё сводится к брони, и включённая трансляция отправила бы полный
    учётный остаток — при том что учёт как раз и врёт."""
    from app.broadcast_gate import calc_status
    from app.timeutils import now_utc

    _snapshot(db, SEP, [("u1", 43)])
    product = _product(db)
    set_base_date(db, product, SEP)
    product.recalc_done_at = now_utc()
    product.recalc_account_ids = "1"
    db.commit()

    assert calc_status(product, True, {1})[0] == "need_fact"

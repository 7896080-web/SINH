"""Порог считается из даты, а не вводится руками.

Оператор задаёт дату, 1С отдаёт остаток ЦС на неё, оператор вписывает, сколько
лежало на складе НА САМОМ ДЕЛЕ, и отдельно — сколько держим у себя (бронь):

    порог = остаток ЦС на дату − (факт на дату − бронь)

Дальше на площадки всегда уходит «остаток ЦС сейчас − порог». Порог описывает
ПОСТОЯННОЕ расхождение учёта 1С с реальным складом, поэтому он не дрейфует:
заказы площадок, приходы, расходы, перемещения, списания и пересортица двигают
остаток, а не порог. Отдельно вычитать движения нельзя — часовая выгрузка 1С
приходит уже со всеми ними внутри, и второе вычитание занизило бы отправку.
"""
from datetime import date

from app.models import Product
from app.transmit import offset_from_base, recompute_offset, sku_quantity


def _product(**kw) -> Product:
    base = dict(uid_1c="u1", article="ART", name="Товар", stock_on_hand=10, reserve=0,
                broadcast_enabled=True)
    base.update(kw)
    return Product(**base)


# ------------------------------------------------ пример из постановки

def test_worked_example_from_the_spec():
    """На 07.08 учёт 1С показал 10, реально нашли 8, из них 2 держим у себя.
    Доступно было 6 — значит порог 4."""
    p = _product(offset_base_date=date(2026, 8, 7), offset_base_stock=10,
                 fact_at_date=8, reserve=2)

    assert offset_from_base(p) == 4

    recompute_offset(p)
    assert sku_quantity(p) == 6          # 10 − 4


def test_threshold_holds_while_stock_moves():
    """Главное свойство порога: остаток изменился — порог тот же."""
    p = _product(offset_base_date=date(2026, 8, 7), offset_base_stock=10,
                 fact_at_date=8, reserve=2)
    recompute_offset(p)

    p.stock_on_hand = 11                 # пришёл приход, прошли заказы, пересортица
    assert sku_quantity(p) == 7
    p.stock_on_hand = 3
    assert sku_quantity(p) == 0          # 3 − 4 ниже нуля, отдаём 0, а не минус


# ------------------------------------------------ факт не введён

def test_fact_defaults_to_what_1c_showed():
    """Оператор ничего не вписал — считаем, что учёт 1С не врёт, и порог
    сводится к одной брони."""
    p = _product(offset_base_date=date(2026, 8, 7), offset_base_stock=10,
                 fact_at_date=None, reserve=2)

    assert offset_from_base(p) == 2

    recompute_offset(p)
    assert sku_quantity(p) == 8          # 10 − 2


def test_fact_zero_is_not_the_same_as_not_entered():
    """Ноль — это утверждение «на складе пусто», а не «не вводил». Разница
    существенная: пустая строка оставила бы порог равным брони, а ноль означает,
    что весь учётный остаток 1С — расхождение."""
    p = _product(offset_base_date=date(2026, 8, 7), offset_base_stock=10,
                 fact_at_date=0, reserve=0)

    assert offset_from_base(p) == 10

    recompute_offset(p)
    assert sku_quantity(p) == 0


# ------------------------------------------------ товара не было на дату

def test_missing_on_that_date_behaves_exactly_like_today():
    """Товара не было в выгрузке на дату — остаток ЦС на дату 0, порог равен
    брони, и уходит «остаток − бронь». Это ровно автоматический режим, который
    работает сегодня: включать трансляцию таким товарам безопасно."""
    p = _product(offset_base_date=date(2026, 8, 7), offset_base_stock=0,
                 fact_at_date=None, reserve=3, stock_on_hand=12)
    recompute_offset(p)

    assert p.broadcast_offset == 3
    assert sku_quantity(p) == 9

    auto = _product(reserve=3, stock_on_hand=12)      # без даты вовсе
    assert sku_quantity(auto) == sku_quantity(p)


# ------------------------------------------------ факт больше учёта

def test_fact_above_1c_gives_a_negative_threshold():
    """На складе реально больше, чем в базе. Порог уходит в минус, и на площадку
    уходит БОЛЬШЕ текущего остатка ЦС — это осознанно, так оператор описывает
    недостачу в учёте, а не на складе."""
    p = _product(offset_base_date=date(2026, 8, 7), offset_base_stock=10,
                 fact_at_date=14, reserve=0)
    recompute_offset(p)

    assert p.broadcast_offset == -4
    assert sku_quantity(p) == 14         # 10 − (−4)


# ------------------------------------------------ бронь пересчитывает порог

def test_changing_the_reserve_recomputes_the_threshold():
    """Ради этого три числа и хранятся. Если бы порог не пересчитывался, новая
    бронь молча ни на что не влияла бы: в пороге сидела бы старая."""
    p = _product(offset_base_date=date(2026, 8, 7), offset_base_stock=10,
                 fact_at_date=8, reserve=2)
    recompute_offset(p)
    assert p.broadcast_offset == 4

    p.reserve = 5                        # оператор решил придержать больше
    assert recompute_offset(p) is True
    assert p.broadcast_offset == 7
    assert sku_quantity(p) == 3          # 10 − 7


def test_recompute_reports_when_nothing_changed():
    p = _product(offset_base_date=date(2026, 8, 7), offset_base_stock=10,
                 fact_at_date=8, reserve=2)
    recompute_offset(p)

    assert recompute_offset(p) is False


# ------------------------------------------------ когда считать нельзя

def test_no_date_leaves_a_hand_typed_threshold_alone():
    """Товары, настроенные до этой правки, продолжают работать: там порог введён
    руками, пересчитывать его не из чего и стирать нельзя."""
    p = _product(broadcast_offset=7)

    assert offset_from_base(p) is None
    assert recompute_offset(p) is False
    assert p.broadcast_offset == 7


def test_waiting_for_1c_does_not_touch_the_threshold():
    """Дата задана, ответа 1С ещё нет. Обнулить порог здесь значило бы тихо
    отправить на площадки полный остаток."""
    p = _product(offset_base_date=date(2026, 8, 7), offset_base_stock=None,
                 broadcast_offset=7)

    assert offset_from_base(p) is None
    assert recompute_offset(p) is False
    assert p.broadcast_offset == 7


def test_threshold_supersedes_the_legacy_manual_value():
    """Порог и устаревший ручной остаток не должны спорить за приоритет."""
    p = _product(offset_base_date=date(2026, 8, 7), offset_base_stock=10,
                 fact_at_date=8, reserve=2, transmit_override=99)
    recompute_offset(p)

    assert p.transmit_override is None
    assert sku_quantity(p) == 6

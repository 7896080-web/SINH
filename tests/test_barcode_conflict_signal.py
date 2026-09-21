"""Разные остатки по баркодам одного товара — это сигнал, а не мелочь.

Несколько штрихкодов одного SKU держат ОДИН физический остаток, и 1С отдаёт по
ним одно и то же число. Разные числа означают, что один из баркодов привязан к
чужому товару. Сверка берёт по товару максимум — то есть ЧУЖОЙ остаток, — и на
площадки уходит больше, чем лежит на складе: оверселл, который сам не пройдёт.

Формулу это не меняет: минимум и сумма врут так же, просто в другую сторону, а
починка тут одна — разобрать привязку на «Мэппинге». Но молчать нельзя:
предохранитель, сработавший молча, — половина предохранителя.
"""
from app.models import Barcode, Product
from app.workers.reconciliation import run_reconciliation


def _product(db, uid, stock, barcodes):
    db.add(Product(uid_1c=uid, article=f"A-{uid}", name="Товар", stock_on_hand=stock))
    for code in barcodes:
        db.add(Barcode(barcode=code, uid_1c=uid))
    db.commit()


def test_matching_numbers_are_not_a_conflict(db):
    """Обычный случай: у товара два штрихкода, 1С отдаёт по ним одно число."""
    _product(db, "u1", 5, ["b1", "b2"])

    stats = run_reconciliation(db, {"b1": 5, "b2": 5})

    assert stats["barcode_conflicts"] == 0


def test_diverging_numbers_are_counted(db):
    _product(db, "u1", 5, ["b1", "b2"])

    stats = run_reconciliation(db, {"b1": 2, "b2": 50})

    assert stats["barcode_conflicts"] == 1


def test_the_stock_still_takes_the_maximum(db):
    """Поведение намеренно не меняется: сигнал — да, тихая смена формулы — нет.
    Выбери мы здесь минимум, товар с честными дублями штрихкода занижался бы, а
    это остановка продаж по живой карточке."""
    _product(db, "u1", 0, ["b1", "b2"])

    run_reconciliation(db, {"b1": 2, "b2": 50})

    assert db.query(Product).filter(Product.uid_1c == "u1").one().stock_on_hand == 50


def test_each_product_counts_once(db):
    """Считаем ТОВАРЫ, а не расхождения: три разошедшихся баркода на одном
    товаре — это один разбор, а не два. Иначе число в предупреждении не сходится
    с тем, сколько строк человеку предстоит открыть."""
    _product(db, "u1", 5, ["b1", "b2", "b3"])

    stats = run_reconciliation(db, {"b1": 1, "b2": 2, "b3": 3})

    assert stats["barcode_conflicts"] == 1

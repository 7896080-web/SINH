"""Порог трансляции (broadcast_offset): трансляция = max(0, текущий ЦС − порог).

Порог фиксирован, может быть ±, приоритетнее transmit_override, floor в 0.
"""

from app.models import Product, SyncSetting
from app.workers.dispatch import _quantity_to_send
from tests.factories import make_account


def _p(db, stock=0, offset=None, override=None, enabled=True, reserve=0) -> int:
    """Готовит товар и ОТМЕЧЕННЫЙ кабинет, возвращает id кабинета.

    Отметка кабинета обязательна: рассылка проверяет её наравне с порогами
    (находка 7 аудита — запись из очереди уходила на кабинет, с которого галочку
    уже сняли). В бою неотмеченных пар в очереди не бывает: все места постановки
    в очередь отбирают только `SyncSetting.enabled`."""
    db.query(Product).filter(Product.uid_1c == "u1").delete()
    p = Product(uid_1c="u1", stock_on_hand=stock, broadcast_enabled=enabled,
                broadcast_offset=offset, transmit_override=override, reserve=reserve)
    db.add(p)
    account = make_account(db)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    return account.id


def test_offset_positive(db):
    acc = _p(db, stock=29, offset=11)
    assert _quantity_to_send(db, "u1", acc, 29) == 18


def test_offset_negative(db):
    # Порог отрицательный: доступно больше учёта ЦС → транслируем больше остатка.
    acc = _p(db, stock=10, offset=-5)
    assert _quantity_to_send(db, "u1", acc, 10) == 15


def test_offset_floors_at_zero(db):
    acc = _p(db, stock=5, offset=20)
    assert _quantity_to_send(db, "u1", acc, 5) == 0


def test_offset_precedes_override(db):
    acc = _p(db, stock=30, offset=10, override=99)
    assert _quantity_to_send(db, "u1", acc, 30) == 20


def test_offset_uses_current_stock_not_quantity_arg(db):
    # Порог считает от stock_on_hand (текущий ЦС), а не от переданного quantity.
    acc = _p(db, stock=29, offset=11)
    assert _quantity_to_send(db, "u1", acc, 999) == 18


def test_disabled_zero(db):
    acc = _p(db, stock=29, offset=11, enabled=False)
    assert _quantity_to_send(db, "u1", acc, 29) == 0


# Роуты установки порога переехали на /products — их тесты в tests/test_web_products.py
# (test_offset_direct_value, test_offset_computed_from_recount, test_offset_can_be_negative,
# test_offset_cleared_by_empty_value, test_clear_legacy_override).

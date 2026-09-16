"""Порог трансляции (broadcast_offset): трансляция = max(0, текущий ЦС − порог).

Порог фиксирован, может быть ±, приоритетнее transmit_override, floor в 0.
"""

from app.models import Product
from app.workers.dispatch import _quantity_to_send


def _p(db, stock=0, offset=None, override=None, enabled=True, reserve=0):
    db.query(Product).filter(Product.uid_1c == "u1").delete()
    p = Product(uid_1c="u1", stock_on_hand=stock, broadcast_enabled=enabled,
                broadcast_offset=offset, transmit_override=override, reserve=reserve)
    db.add(p)
    db.commit()
    return p


def test_offset_positive(db):
    _p(db, stock=29, offset=11)
    assert _quantity_to_send(db, "u1", 1, 29) == 18


def test_offset_negative(db):
    # Порог отрицательный: доступно больше учёта ЦС → транслируем больше остатка.
    _p(db, stock=10, offset=-5)
    assert _quantity_to_send(db, "u1", 1, 10) == 15


def test_offset_floors_at_zero(db):
    _p(db, stock=5, offset=20)
    assert _quantity_to_send(db, "u1", 1, 5) == 0


def test_offset_precedes_override(db):
    _p(db, stock=30, offset=10, override=99)
    assert _quantity_to_send(db, "u1", 1, 30) == 20


def test_offset_uses_current_stock_not_quantity_arg(db):
    # Порог считает от stock_on_hand (текущий ЦС), а не от переданного quantity.
    _p(db, stock=29, offset=11)
    assert _quantity_to_send(db, "u1", 1, 999) == 18


def test_disabled_zero(db):
    _p(db, stock=29, offset=11, enabled=False)
    assert _quantity_to_send(db, "u1", 1, 29) == 0


# --- роут /testing/set-override: ввод «доступно» + «остаток ЦС на дату» → порог ---

def test_set_threshold_route_computes_offset(logged_in_client, web_db):
    web_db.add(Product(uid_1c="uX", stock_on_hand=29, broadcast_enabled=True,
                       transmit_override=7))
    web_db.commit()
    logged_in_client.post("/testing/set-override",
                          data={"uid_1c": "uX", "available": "32", "stock_at_date": "43"})
    web_db.expire_all()
    p = web_db.query(Product).filter(Product.uid_1c == "uX").first()
    assert p.broadcast_offset == 11          # 43 − 32
    assert p.transmit_override is None        # старую ручную цифру гасим


def test_set_threshold_route_negative(logged_in_client, web_db):
    web_db.add(Product(uid_1c="uY", stock_on_hand=10, broadcast_enabled=True))
    web_db.commit()
    logged_in_client.post("/testing/set-override",
                          data={"uid_1c": "uY", "available": "15", "stock_at_date": "10"})
    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "uY").first().broadcast_offset == -5


def test_clear_threshold_route(logged_in_client, web_db):
    web_db.add(Product(uid_1c="uZ", stock_on_hand=10, broadcast_enabled=True, broadcast_offset=3))
    web_db.commit()
    logged_in_client.post("/testing/set-override", data={"uid_1c": "uZ", "clear": "1"})
    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "uZ").first().broadcast_offset is None


def test_set_threshold_warns_when_broadcast_disabled(logged_in_client, web_db):
    """Порог задан, но трансляция SKU выключена → сообщение обязано сказать, что уходит 0."""
    from app.models import Product
    web_db.add(Product(uid_1c="u9", stock_on_hand=29, broadcast_enabled=False))
    web_db.commit()
    r = logged_in_client.post("/testing/set-override",
                              data={"uid_1c": "u9", "account_id": "", "available": "32", "stock_at_date": "43"})
    assert "ВЫКЛЮЧЕНА" in r.text and "уходит 0" in r.text
    web_db.refresh(web_db.query(Product).filter(Product.uid_1c == "u9").first())
    assert web_db.query(Product).filter(Product.uid_1c == "u9").first().broadcast_offset == 11

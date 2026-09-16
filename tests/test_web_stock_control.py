from datetime import date

from app.models import Product, PlatformAccount, Platform


def _add_product(web_db, uid="u1", stock=5, reserve=0, override=None, broadcast=True):
    p = Product(uid_1c=uid, article="A-" + uid, name="Товар " + uid, stock_on_hand=stock,
                reserve=reserve, transmit_override=override, broadcast_enabled=broadcast)
    web_db.add(p)
    web_db.commit()
    return p


def test_stock_control_page_renders(logged_in_client, web_db):
    _add_product(web_db)
    r = logged_in_client.get("/stock-control")
    assert r.status_code == 200
    assert "Управление остатками" in r.text
    assert "Товар u1" in r.text


def test_row_reserve_update(logged_in_client, web_db):
    _add_product(web_db, stock=5, reserve=0)
    logged_in_client.post("/stock-control/row/u1/reserve", data={"reserve": "2", "q": ""})
    web_db.expire_all()
    assert web_db.query(Product).first().reserve == 2


def test_row_override_is_sticky_and_clears_on_empty(logged_in_client, web_db):
    _add_product(web_db, stock=5, reserve=0)

    logged_in_client.post("/stock-control/row/u1/override", data={"value": "9", "q": ""})
    web_db.expire_all()
    assert web_db.query(Product).first().transmit_override == 9

    # обновление остатка из 1С не должно сбрасывать ручной override (залипает)
    p = web_db.query(Product).first()
    p.stock_on_hand = 3
    web_db.commit()
    web_db.expire_all()
    assert web_db.query(Product).first().transmit_override == 9

    # пустое значение → сброс override (снова автоматический расчёт)
    logged_in_client.post("/stock-control/row/u1/override", data={"value": "", "q": ""})
    web_db.expire_all()
    assert web_db.query(Product).first().transmit_override is None


def test_row_override_rejects_non_number(logged_in_client, web_db):
    _add_product(web_db, override=4)
    logged_in_client.post("/stock-control/row/u1/override", data={"value": "abc", "q": ""})
    web_db.expire_all()
    assert web_db.query(Product).first().transmit_override == 4  # не изменилось


def test_row_broadcast_toggle(logged_in_client, web_db):
    _add_product(web_db, broadcast=True)
    logged_in_client.post("/stock-control/row/u1/broadcast", data={"enabled": "false", "q": ""})
    web_db.expire_all()
    assert web_db.query(Product).first().broadcast_enabled is False


def test_row_active_since_set_and_clear(logged_in_client, web_db):
    _add_product(web_db)
    logged_in_client.post("/stock-control/row/u1/active-since", data={"value": "2026-09-01", "q": ""})
    web_db.expire_all()
    assert web_db.query(Product).first().broadcast_active_since == date(2026, 9, 1)

    logged_in_client.post("/stock-control/row/u1/active-since", data={"value": "", "q": ""})
    web_db.expire_all()
    assert web_db.query(Product).first().broadcast_active_since is None


def test_bulk_reserve_override_broadcast(logged_in_client, web_db):
    _add_product(web_db, uid="u1")
    _add_product(web_db, uid="u2")

    logged_in_client.post("/stock-control/bulk", data={
        "action": "set_reserve", "int_value": "3", "uids": ["u1", "u2"], "q": ""})
    web_db.expire_all()
    assert all(p.reserve == 3 for p in web_db.query(Product).all())

    logged_in_client.post("/stock-control/bulk", data={
        "action": "broadcast_off", "uids": ["u1", "u2"], "q": ""})
    web_db.expire_all()
    assert all(p.broadcast_enabled is False for p in web_db.query(Product).all())

    logged_in_client.post("/stock-control/bulk", data={
        "action": "set_override", "int_value": "7", "uids": ["u1"], "q": ""})
    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first().transmit_override == 7

    logged_in_client.post("/stock-control/bulk", data={
        "action": "clear_override", "uids": ["u1"], "q": ""})
    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first().transmit_override is None


def test_bulk_without_selection_is_noop(logged_in_client, web_db):
    _add_product(web_db, uid="u1", reserve=0)
    logged_in_client.post("/stock-control/bulk", data={"action": "set_reserve", "int_value": "5", "q": ""})
    web_db.expire_all()
    assert web_db.query(Product).first().reserve == 0


def test_dispatch_toggle_per_platform_and_all(logged_in_client, web_db):
    web_db.add(PlatformAccount(platform=Platform.wb, name="WB1", is_active=True, dispatch_enabled=True, warehouse_id="w"))
    web_db.add(PlatformAccount(platform=Platform.wb, name="WB2", is_active=True, dispatch_enabled=True, warehouse_id="w"))
    web_db.add(PlatformAccount(platform=Platform.ozon, name="OZ", is_active=True, dispatch_enabled=True, warehouse_id="w"))
    web_db.commit()

    # пауза только WB (обе WB-строки), Ozon не трогаем
    logged_in_client.post("/stock-control/dispatch-toggle", data={"scope": "wb", "enabled": "false", "q": ""})
    web_db.expire_all()
    wb = web_db.query(PlatformAccount).filter(PlatformAccount.platform == Platform.wb).all()
    oz = web_db.query(PlatformAccount).filter(PlatformAccount.platform == Platform.ozon).first()
    assert all(a.dispatch_enabled is False for a in wb)
    assert oz.dispatch_enabled is True

    # включить всё
    logged_in_client.post("/stock-control/dispatch-toggle", data={"scope": "all", "enabled": "true", "q": ""})
    web_db.expire_all()
    assert all(a.dispatch_enabled is True for a in web_db.query(PlatformAccount).all())

"""Страница «Тестирование» не должна трогать боевое.

`is_test` закрывает одну сторону: симуляция не создаёт боевых побочных записей.
Здесь закрыта другая — боевую запись нельзя ПЕРЕДАТЬ в симуляцию, — и ещё два
случая, где «безопасная проверка» вела себя как боевая рассылка.
"""
from datetime import datetime

from app.models import (Barcode, PlatformAccount, ProcessedOrder, OrderProcessStatus,
                        Product, SyncSetting)


def _seed(web_db, dispatch_enabled=True):
    account = PlatformAccount(platform="wb", name="Кабинет 1", warehouse_id="wh-1",
                              dispatch_enabled=dispatch_enabled)
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    web_db.add(Product(uid_1c="u1", article="ART-1", name="Товар",
                       stock_on_hand=10, broadcast_enabled=True))
    web_db.add(Barcode(barcode="111", uid_1c="u1"))
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()
    return account


def _live_order(web_db, account, order_id="WB-777"):
    order = ProcessedOrder(order_id=order_id, account_id=account.id, uid_1c="u1",
                           quantity=2, status=OrderProcessStatus.processed,
                           processed_at=datetime(2026, 9, 20, 10, 0))
    web_db.add(order)
    web_db.commit()
    return order


# ------------------------------------------- боевой заказ в симуляции

def test_simulating_a_cancel_refuses_a_live_order(logged_in_client, web_db):
    """Отмена вернула бы остаток у НАС по-настоящему, а задание в 1С пометила бы
    тестовым — обратного документа там не будет никогда. Товар вернулся в
    продажу у нас и остался отгруженным в 1С: наружу уходит больше, чем есть.
    Вдобавок заказ помечен отменённым, и настоящую отмену опрос потом пропустит
    как уже обработанную."""
    account = _seed(web_db)
    _live_order(web_db, account)

    r = logged_in_client.post("/testing/simulate-cancel", data={
        "uid_1c": "u1", "account_id": account.id, "order_id": "WB-777"},
        follow_redirects=False)

    assert r.status_code == 303
    web_db.expire_all()
    order = web_db.query(ProcessedOrder).one()
    assert order.status == OrderProcessStatus.processed, "боевой заказ не должен закрываться"
    assert web_db.query(Product).one().stock_on_hand == 10, "остаток не должен вырасти"


def test_simulating_a_confirm_refuses_a_live_order(logged_in_client, web_db):
    """То же для подтверждения: у нас заказ закрыт, а в 1С единица навсегда
    остаётся на промежуточном складе «Ожидает»."""
    account = _seed(web_db)
    _live_order(web_db, account)

    logged_in_client.post("/testing/simulate-confirm", data={
        "uid_1c": "u1", "account_id": account.id, "order_id": "WB-777"},
        follow_redirects=False)

    web_db.expire_all()
    assert web_db.query(ProcessedOrder).one().status == OrderProcessStatus.processed


def test_a_test_order_is_still_simulated(logged_in_client, web_db):
    """Проверка не должна запретить то, ради чего страница существует."""
    account = _seed(web_db)
    logged_in_client.post("/testing/simulate-order", data={
        "uid_1c": "u1", "account_id": account.id, "quantity": 2})
    web_db.expire_all()
    order = web_db.query(ProcessedOrder).one()
    assert order.order_id.startswith("TEST-")

    logged_in_client.post("/testing/simulate-cancel", data={
        "uid_1c": "u1", "account_id": account.id, "order_id": order.order_id})

    web_db.expire_all()
    assert web_db.query(ProcessedOrder).one().status == OrderProcessStatus.cancelled


# ------------------------------------------- «Шаг 1: отправить остаток»

def test_step_one_sends_nothing_to_a_paused_account(logged_in_client, web_db, monkeypatch):
    """Пауза кабинета — уже ступень лестницы (`transmit`, ступень 3), поэтому
    НЕПУСТОЙ остаток туда и раньше не уходил: лестница давала 0. Опасен был
    остаток сведённый к нулю у пары, куда мы когда-то писали: проверка «ноль не
    отправляем» пропускала его (нам ЕСТЬ что отзывать), и кнопка обнуляла
    карточку в кабинете, который оператор намеренно остановил — а журнал писал
    зелёное «успешно отправлен».

    Теперь отказ идёт раньше и называет причину. Контрольная половина теста
    обязательна: без неё тест зеленел бы и от того, что отправку остановило
    что-то постороннее.
    """
    account = _seed(web_db, dispatch_enabled=False)
    setting = web_db.query(SyncSetting).one()
    setting.last_nonzero_sent_at = datetime(2026, 9, 1, 10, 0)   # туда уже писали
    web_db.commit()
    sent = []

    class FakeClient:
        def push_stock(self, warehouse_id, items):
            sent.append(items)
            return {"ok": [i.barcode for i in items], "errors": []}

    monkeypatch.setattr("app.routers.testing.build_client", lambda db, aid: FakeClient())

    logged_in_client.post("/testing/push-stock", data={
        "uid_1c": "u1", "account_id": account.id}, follow_redirects=False)
    assert sent == [], "в остановленный кабинет не должен уходить и ноль"

    account.dispatch_enabled = True
    web_db.commit()
    logged_in_client.post("/testing/push-stock", data={
        "uid_1c": "u1", "account_id": account.id}, follow_redirects=False)
    assert sent, "без паузы отправка обязана дойти до площадки"


def test_step_one_records_that_we_wrote_to_the_card(logged_in_client, web_db, monkeypatch):
    """Отправка отсюда настоящая, значит и след обязан быть настоящим. Без него
    система забывает, что писала на карточку, и снятие галочки потом не отзовёт
    остаток — площадка продолжит продавать по нашему числу."""
    account = _seed(web_db)

    class FakeClient:
        def push_stock(self, warehouse_id, items):
            return {"ok": [i.barcode for i in items], "errors": []}

    monkeypatch.setattr("app.routers.testing.build_client", lambda db, aid: FakeClient())

    logged_in_client.post("/testing/push-stock", data={
        "uid_1c": "u1", "account_id": account.id}, follow_redirects=False)

    web_db.expire_all()
    setting = web_db.query(SyncSetting).one()
    assert setting.last_nonzero_sent_at is not None

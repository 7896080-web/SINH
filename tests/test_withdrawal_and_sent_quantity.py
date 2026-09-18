"""Отзыв остатка и то, какое число мы на самом деле отправили.

Оба дефекта вскрылись на одном разборе (18.09, товар 36446 SELIANIK XL TAS MELANJ).

1. Оператор снял галочки с кабинетов ОЗОН и КИТ. В очередь встали нули, и они
   УШЛИ на площадки. Трансляция товара в тот момент была ещё выключена — по
   автоматическим путям туда не отправлялось ничего и никогда. Мы не сняли свой
   остаток, мы обнулили чужие карточки, по которым шли продажи. Доотправку от
   этого уже защитили (`enqueue_full_resend` выходит сразу), а отзыв — нет.

2. Разобрать случай по базе не получилось: в `dispatch_queue.quantity` лежит
   исходный остаток на момент постановки в очередь, а на площадку уходит итог
   всей лестницы, посчитанный в момент отправки. При заданном пороге трансляции
   это всегда разные числа — в очереди стояло 18, на WB ушло 46.
"""
from datetime import date

from app.models import (Barcode, DispatchQueueItem, DispatchStatus, Platform,
                        PlatformAccount, Product, SyncSetting)
from app.workers.dispatch import run_dispatch_cycle
from tests.factories import make_account


class FakePlatform:
    def __init__(self):
        self.pushed = []

    def push_stock(self, warehouse_id, items):
        self.pushed.append(list(items))
        return {"ok": [i.barcode for i in items], "errors": []}


def _product(db, uid="u1", stock=18, broadcast=False, offset=None, recalc_ids=None):
    from app.timeutils import now_utc
    p = Product(uid_1c=uid, article="A-1", name="Товар", stock_on_hand=stock, reserve=0,
                broadcast_enabled=broadcast, broadcast_offset=offset,
                recalc_done_at=now_utc() if recalc_ids else None,
                recalc_account_ids=recalc_ids)
    db.add(p)
    db.add(Barcode(barcode=f"bc-{uid}", uid_1c=uid))
    db.commit()
    return p


# ------------------------------------------------ отзыв остатка

def test_unticking_a_cabinet_of_a_silent_product_sends_nothing(logged_in_client, web_db):
    """Тот самый случай: трансляция выключена, мы туда ни разу ничего не слали."""
    account = make_account(web_db, Platform.ozon, name="ОЗОН")
    _product(web_db, broadcast=False)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    logged_in_client.post(f"/products/u1/{account.id}/toggle", data={"enabled": "false"})

    assert web_db.query(DispatchQueueItem).count() == 0, \
        "ноль на площадку, куда мы не транслировали, обнуляет живую карточку"


def test_unticking_a_broadcasting_product_still_withdraws(logged_in_client, web_db):
    """Обратная сторона: где остаток реально уходил, отзыв обязателен — иначе
    площадка продолжит продавать по последнему присланному числу."""
    account = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(web_db, broadcast=True, recalc_ids=None)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    logged_in_client.post(f"/products/u1/{account.id}/toggle", data={"enabled": "false"})

    items = web_db.query(DispatchQueueItem).all()
    assert [i.quantity for i in items] == [0]
    assert items[0].reason == "manual_disable"


def test_turning_broadcast_off_still_withdraws(logged_in_client, web_db):
    """Главный выключатель — осознанный отзыв: его трогать нельзя."""
    account = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(web_db, broadcast=True)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    logged_in_client.post("/products/u1/broadcast", data={"enabled": "false"})

    items = web_db.query(DispatchQueueItem).all()
    assert [i.quantity for i in items] == [0]
    assert items[0].reason == "broadcast_off"


def test_excel_import_withdraws_like_the_interface(logged_in_client, web_db):
    """Снятие галочки через Excel раньше не отзывало остаток вовсе: на площадке
    оставалось последнее число, а заказы по снятой паре гейт уже пропускал."""
    from app.transmit import enqueue_withdrawal

    account = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(web_db, broadcast=True)
    setting = SyncSetting(uid_1c="u1", account_id=account.id, enabled=True)
    web_db.add(setting)
    web_db.commit()

    # Прямой вызов того же пути, что и импорт: галочка была, стала снята.
    enqueue_withdrawal(web_db, "u1", account.id)
    web_db.commit()

    assert web_db.query(DispatchQueueItem).count() == 1


# ------------------------------------------------ что ушло на самом деле

def test_the_queue_records_the_number_that_actually_left(db):
    """В очередь встало 18, на площадку ушло 46 — и обе цифры должны остаться."""
    account = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    product = _product(db, stock=18, broadcast=True, offset=-28,
                       recalc_ids=str(account.id))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id,
                             quantity=product.stock_on_hand, reason="manual_enable"))
    db.commit()

    platform = FakePlatform()
    run_dispatch_cycle(db, {account.id: platform}, [account])

    item = db.query(DispatchQueueItem).one()
    assert item.status == DispatchStatus.sent
    assert item.quantity == 18, "исходный остаток остаётся как был"
    assert item.sent_quantity == 46, "18 − порог (−28)"
    assert [i.quantity for i in platform.pushed[0]] == [46]


def test_sent_quantity_stays_empty_until_something_is_sent(db):
    account = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(db, stock=18, broadcast=True)
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=18,
                             reason="manual_enable"))
    db.commit()

    item = db.query(DispatchQueueItem).one()
    assert item.sent_quantity is None

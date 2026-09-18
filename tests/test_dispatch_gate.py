"""Находки 6 и 7 аудита — снятие галочки кабинета.

6. Снятие галочки не отзывало остаток: на площадке оставалось последнее
   отправленное число, она продолжала продавать, а заказы по снятой паре гейт
   отбора уже пропускал — ни списания у нас, ни документа в 1С.
7. Запись, попавшая в очередь ДО снятия галочки, переживала снятие: цикл
   рассылки брал её и отправлял на отключённый кабинет полный остаток.

Обе — про один и тот же разрыв: интерфейс (`transmit.explain`) проверял отметку
кабинета и паузу площадки, а рассылка (`transmit.quantity_for_account`) — нет.
Поэтому здесь же проверяется, что обе стороны считают одинаково.
"""
import pytest

from app.models import (Product, Barcode, SyncSetting, PlatformAccount, Platform,
                        DispatchQueueItem, DispatchStatus)
from app.transmit import explain, quantity_for_account
from app.workers.dispatch import run_dispatch_cycle
from tests.factories import make_account


class FakePlatformClient:
    def __init__(self):
        self.push_calls = []

    def push_stock(self, warehouse_id, items):
        self.push_calls.append((warehouse_id, list(items)))
        return {"ok": [i.barcode for i in items], "errors": []}


def _stocked_product(db, stock: int = 12, uid: str = "u1", barcode: str = "111") -> Product:
    p = Product(uid_1c=uid, article="A1", name="Товар", stock_on_hand=stock, broadcast_enabled=True)
    db.add(p)
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.commit()
    return p


def _already_transmitted(db, account_id: int, uid: str = "u1", quantity: int = 12):
    """Отправка, которая реально дошла до площадки.

    Отзыв остатка с неё и начинается: ноль снимает НАШЕ число, а если мы ничего
    не отправляли, он обнуляет чужую карточку. Раньше здесь этого не было — до
    18.09 отзыв срабатывал по одной только включённой трансляции, и на Озон с
    Kit уехали нули по товару, который туда ни разу не транслировался.
    """
    from app.timeutils import now_utc

    db.add(DispatchQueueItem(
        uid_1c=uid, account_id=account_id, quantity=quantity, sent_quantity=quantity,
        reason="manual_enable", status=DispatchStatus.sent, sent_at=now_utc()))
    db.commit()


# ------------------------------------------------ 6. снятие галочки отзывает остаток

def test_unchecking_cabinet_enqueues_zero(logged_in_client, web_db):
    """Само исправление: после снятия галочки в очереди появляется ноль."""
    _stocked_product(web_db)
    account = make_account(web_db, name="Кабинет")
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()
    _already_transmitted(web_db, account.id)

    r = logged_in_client.post(f"/products/u1/{account.id}/toggle", data={"enabled": "false"})

    assert r.status_code == 200
    queued = web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.reason == "manual_disable").all()
    assert [(q.quantity, q.reason) for q in queued] == [(0, "manual_disable")]


def test_zero_actually_reaches_the_platform(web_db):
    """Ноль должен не просто лечь в очередь, а дойти до площадки — иначе товар
    остаётся в продаже там, где мы его больше не контролируем."""
    _stocked_product(web_db)
    account = make_account(web_db, warehouse_id="wh-1")
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=False))
    web_db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=0,
                                 reason="manual_disable"))
    web_db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(web_db, {account.id: client}, [account])

    assert len(client.push_calls) == 1
    _, items = client.push_calls[0]
    assert [i.quantity for i in items] == [0]


def test_checking_cabinet_still_sends_full_stock(logged_in_client, web_db):
    """Обратное действие не должно пострадать: включение по-прежнему доотправляет
    полный остаток."""
    _stocked_product(web_db, stock=12)
    account = make_account(web_db, name="Кабинет")

    logged_in_client.post(f"/products/u1/{account.id}/toggle", data={"enabled": "true"})

    queued = web_db.query(DispatchQueueItem).all()
    assert [(q.quantity, q.reason) for q in queued] == [(12, "manual_enable")]


def test_repeated_uncheck_does_not_pile_up_zeros(logged_in_client, web_db):
    """Повторное снятие уже снятой галочки — не действие: очередь не растёт."""
    _stocked_product(web_db)
    account = make_account(web_db, name="Кабинет")
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    _already_transmitted(web_db, account.id)

    for _ in range(3):
        logged_in_client.post(f"/products/u1/{account.id}/toggle", data={"enabled": "false"})

    assert web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.reason == "manual_disable").count() == 1


def test_uncheck_is_written_to_audit(logged_in_client, web_db):
    from app.models import AuditLog

    _stocked_product(web_db)
    account = make_account(web_db, name="Кабинет")
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    _already_transmitted(web_db, account.id)

    logged_in_client.post(f"/products/u1/{account.id}/toggle", data={"enabled": "false"})

    entry = web_db.query(AuditLog).filter(AuditLog.action == "sync_disabled").first()
    assert entry is not None
    assert "в очередь 0" in entry.details


# --------------------------------- 7. очередь не переживает снятие галочки

def test_queued_item_does_not_reach_unchecked_cabinet(web_db):
    """Сценарий из аудита: запись легла в очередь при отмеченном кабинете, галочку
    сняли, и цикл рассылки отправлял полный остаток на отключённый кабинет."""
    _stocked_product(web_db, stock=12)
    account = make_account(web_db, warehouse_id="wh-1")
    setting = SyncSetting(uid_1c="u1", account_id=account.id, enabled=True)
    web_db.add(setting)
    web_db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=12, reason="order"))
    web_db.commit()

    setting.enabled = False          # оператор снял галочку до начала цикла
    web_db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(web_db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert [i.quantity for i in items] == [0]


def test_queued_item_without_any_setting_sends_zero(web_db):
    """Строки SyncSetting вообще нет — пара товар+кабинет не отмечена никогда.
    Такая запись в очереди может остаться от удалённой отметки."""
    _stocked_product(web_db, stock=12)
    account = make_account(web_db, warehouse_id="wh-1")
    web_db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=12, reason="order"))
    web_db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(web_db, {account.id: client}, [account])

    _, items = client.push_calls[0]
    assert [i.quantity for i in items] == [0]


def test_paused_account_computes_zero(web_db):
    """Пауза рассылки на кабинет: сам цикл такой кабинет и так пропускает (очередь
    копится), но расчёт обязан давать тот же ноль, что показывает интерфейс."""
    _stocked_product(web_db, stock=12)
    account = make_account(web_db, warehouse_id="wh-1")
    account.dispatch_enabled = False
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    assert quantity_for_account(web_db, "u1", account.id, 12) == 0


def test_pause_does_not_withdraw_stock(web_db):
    """Граница: пауза — это «подождать», а не «отозвать». Очередь копится и уйдёт
    при снятии паузы; ноль на площадку сама по себе пауза не отправляет."""
    _stocked_product(web_db, stock=12)
    account = make_account(web_db, warehouse_id="wh-1")
    account.dispatch_enabled = False
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=12, reason="order"))
    web_db.commit()

    client = FakePlatformClient()
    run_dispatch_cycle(web_db, {account.id: client}, [account])

    assert client.push_calls == []
    item = web_db.query(DispatchQueueItem).first()
    assert item.status == DispatchStatus.pending


# ------------------------------- интерфейс и рассылка считают одно и то же

@pytest.mark.parametrize("broadcast,enabled,dispatch_enabled,threshold,expected", [
    (True,  True,  True,  0,  12),   # всё включено
    (False, True,  True,  0,  0),    # выключен товар
    (True,  False, True,  0,  0),    # кабинет не отмечен  ← находка 7
    (True,  True,  False, 0,  0),    # рассылка на паузе   ← находка 7
    (True,  True,  True,  12, 0),    # порог кабинета не пройден
    (True,  True,  True,  11, 12),   # порог пройден
])
def test_ui_and_dispatch_agree(web_db, broadcast, enabled, dispatch_enabled, threshold, expected):
    """Главный инвариант: число в строке товара и число, которое уходит на
    площадку, считает один модуль — расхождение между ними и было причиной обеих
    находок."""
    product = _stocked_product(web_db, stock=12)
    product.broadcast_enabled = broadcast
    account = PlatformAccount(platform=Platform.wb, name="Кабинет", warehouse_id="wh-1",
                              dispatch_enabled=dispatch_enabled)
    web_db.add(account)
    web_db.commit()
    setting = SyncSetting(uid_1c="u1", account_id=account.id, enabled=enabled,
                          min_threshold=threshold)
    web_db.add(setting)
    web_db.commit()

    from_ui = explain(product, setting, account).quantity
    from_dispatch = quantity_for_account(web_db, "u1", account.id, 12)

    assert from_ui == from_dispatch == expected


# --------------------------------------------- тишина до включения трансляции

def test_editing_a_silent_product_queues_nothing(db):
    """Правка брони, факта или порога у товара, который ещё не транслируется, не
    должна ничего отправлять на площадку.

    Это и есть требование «до момента полного расчёта не транслируем ни ноль, ни
    какой-либо остаток»: оператор настраивает товар, а карточка на площадке живёт
    своей жизнью, пока он не нажмёт «Вкл»."""
    from app.models import DispatchQueueItem, Platform, Product, SyncSetting
    from app.transmit import enqueue_full_resend
    from tests.factories import make_account

    account = make_account(db, Platform.wb)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10,
                   broadcast_enabled=False))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    enqueue_full_resend(db, "u1", account.id, reason="reserve_changed")
    db.commit()

    assert db.query(DispatchQueueItem).count() == 0


def test_a_transmitting_product_still_gets_its_update(db):
    """Обратная сторона: у включённого товара правка по-прежнему доезжает."""
    from app.models import DispatchQueueItem, Platform, Product, SyncSetting
    from app.transmit import enqueue_full_resend
    from tests.factories import make_account

    account = make_account(db, Platform.wb)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10,
                   broadcast_enabled=True))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    enqueue_full_resend(db, "u1", account.id, reason="reserve_changed")
    db.commit()

    assert db.query(DispatchQueueItem).count() == 1


def test_switching_a_product_off_still_withdraws_the_stock(web_db, logged_in_client):
    """Граница, которую легко снести этим же правилом: снятие с трансляции —
    ОСОЗНАННЫЙ отзыв. На площадку обязан уйти ноль, иначе она продолжит продавать
    по последнему присланному числу. Раньше это работало побочным эффектом
    (в очередь ставилась обычная доотправка, а рассылка считала по ней ноль);
    теперь отзыв делается явно."""
    from app.models import DispatchQueueItem, Platform, PlatformAccount, Product, SyncSetting

    account = PlatformAccount(platform=Platform.wb, name="WB-1", warehouse_id="wh")
    web_db.add(account)
    web_db.commit()
    web_db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10,
                       broadcast_enabled=True))
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()
    _already_transmitted(web_db, account.id, quantity=10)

    logged_in_client.post("/products/u1/broadcast", data={"enabled": "false"})

    queued = web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.reason == "broadcast_off").all()
    assert len(queued) == 1
    assert queued[0].quantity == 0

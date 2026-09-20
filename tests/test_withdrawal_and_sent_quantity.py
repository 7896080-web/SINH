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
    from app.timeutils import now_utc

    account = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(web_db, broadcast=True, recalc_ids=None)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=18, sent_quantity=46,
        reason="manual_enable", status=DispatchStatus.sent, sent_at=now_utc()))
    web_db.commit()

    logged_in_client.post(f"/products/u1/{account.id}/toggle", data={"enabled": "false"})

    items = web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.reason == "manual_disable").all()
    assert [i.quantity for i in items] == [0]


def test_turning_broadcast_off_still_withdraws(logged_in_client, web_db):
    """Главный выключатель — осознанный отзыв: его трогать нельзя."""
    from app.timeutils import now_utc

    account = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(web_db, broadcast=True)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=18, sent_quantity=46,
        reason="manual_enable", status=DispatchStatus.sent, sent_at=now_utc()))
    web_db.commit()

    logged_in_client.post("/products/u1/broadcast", data={"enabled": "false"})

    items = web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.reason == "broadcast_off").all()
    assert [i.quantity for i in items] == [0]


def test_excel_import_withdraws_like_the_interface(web_db):
    """Снятие галочки через Excel раньше не отзывало остаток вовсе: на площадке
    оставалось последнее число, а заказы по снятой паре гейт уже пропускал.
    Условие то же, что и в интерфейсе: отзываем, если было что отзывать."""
    from app.timeutils import now_utc
    from app.transmit import should_withdraw

    account = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    product = _product(web_db, broadcast=True)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    assert should_withdraw(web_db, product, account.id) is False

    web_db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=18, sent_quantity=46,
        reason="manual_enable", status=DispatchStatus.sent, sent_at=now_utc()))
    web_db.commit()

    assert should_withdraw(web_db, product, account.id) is True


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


def test_the_queue_records_the_sku_it_was_sent_with(db):
    """У товара бывает несколько баркодов, и какой из них уйдёт, решает
    `_resolve_push_target` уже в момент отправки. Без записи ключ по базе потом
    не восстановить: 19.09 разбор «почему на WB ноль» из-за этого занял час —
    количество знали, sku нет. Он же нужен сверке, чтобы спросить площадку про
    ТОТ идентификатор, которым отправляли, а не про любой баркод товара."""
    account = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(db, stock=18, broadcast=True, recalc_ids=str(account.id))
    db.add(Barcode(barcode="второй", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=18,
                             reason="manual_enable"))
    db.commit()

    platform = FakePlatform()
    run_dispatch_cycle(db, {account.id: platform}, [account])

    item = db.query(DispatchQueueItem).one()
    assert item.sent_sku, "sku, которым отправляли, обязан быть записан"
    assert item.sent_sku == platform.pushed[0][0].barcode, \
        "записан именно тот идентификатор, который ушёл на площадку"


def test_sent_quantity_stays_empty_until_something_is_sent(db):
    account = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(db, stock=18, broadcast=True)
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=18,
                             reason="manual_enable"))
    db.commit()

    item = db.query(DispatchQueueItem).one()
    assert item.sent_quantity is None


# ------------------------------------------------ отзывать только то, что слали

def test_unticking_a_cabinet_we_never_sent_to_stays_silent(logged_in_client, web_db):
    """Продолжение разбора. Первый фикс смотрел только на трансляцию, а тут она
    ВКЛЮЧЕНА: галочку на Kit поставили, расчёт её покрыл, но остаток туда ещё не
    уехал. Снятие галочки в этот момент отправило бы ноль на карточку, которой мы
    ни разу не касались, — ровно та же ошибка, что и 18.09."""
    account = make_account(web_db, Platform.kit, name="КИТ")
    _product(web_db, broadcast=True, recalc_ids=None)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    logged_in_client.post(f"/products/u1/{account.id}/toggle", data={"enabled": "false"})

    assert web_db.query(DispatchQueueItem).count() == 0


def test_a_cabinet_we_did_send_to_is_still_withdrawn(logged_in_client, web_db):
    from app.timeutils import now_utc

    account = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(web_db, broadcast=True)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=18, sent_quantity=46,
        reason="manual_enable", status=DispatchStatus.sent, sent_at=now_utc()))
    web_db.commit()

    logged_in_client.post(f"/products/u1/{account.id}/toggle", data={"enabled": "false"})

    assert web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.reason == "manual_disable").count() == 1


def test_past_withdrawals_do_not_count_as_transmissions(web_db):
    """На кабинет уходили одни нули — значит отзывать по-прежнему нечего."""
    from app.timeutils import now_utc
    from app.transmit import ever_transmitted

    account = make_account(web_db, Platform.kit, name="КИТ")
    _product(web_db, broadcast=True)
    web_db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=0, sent_quantity=0,
        reason="manual_disable", status=DispatchStatus.sent, sent_at=now_utc()))
    web_db.commit()

    assert ever_transmitted(web_db, "u1", account.id) is False


def test_a_row_older_than_sent_quantity_is_judged_by_its_reason(web_db):
    """Записи до pm45 точного числа не хранят. Судить остаётся по причине —
    иначе товар, который реально транслировался, перестал бы отзываться."""
    from app.timeutils import now_utc
    from app.transmit import ever_transmitted

    account = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(web_db, broadcast=True)
    web_db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=18, sent_quantity=None,
        reason="broadcast_toggled", status=DispatchStatus.sent, sent_at=now_utc()))
    web_db.commit()

    assert ever_transmitted(web_db, "u1", account.id) is True


def test_a_legacy_reconciliation_row_counts_as_a_transmission(web_db):
    """Находка 4 аудита: легаси-строка `reconciliation` могла унести ноль (до
    18.09 сверка ставила в очередь и товар с выключенной трансляцией, и кабинет
    вне расчёта, а лестница обнуляла их уже при отправке) — и всё равно считается
    здесь отправкой.

    Это осознанный выбор направления ошибки, а не недосмотр. Строка со статусом
    `sent` и непустым `sent_at` означает, что `push_stock` по этой карточке мы
    уже звали: ушёл там ноль — карточка и так на нуле, лишний отзыв не меняет
    ничего. Обратная трактовка стоила бы оверселла: товар, который правда
    транслировался до появления колонки, при снятии галочки не был бы отозван,
    площадка продолжила бы им торговать, а заказы по снятой паре живой опрос
    пропускает целиком — ни списания, ни документа в 1С."""
    from app.timeutils import now_utc
    from app.transmit import ever_transmitted

    account = make_account(web_db, Platform.ozon, name="Озон")
    _product(web_db, broadcast=True)
    web_db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=18, sent_quantity=None,
        reason="reconciliation", status=DispatchStatus.sent, sent_at=now_utc()))
    web_db.commit()

    assert ever_transmitted(web_db, "u1", account.id) is True


def test_an_absorbed_row_is_not_a_transmission(web_db):
    """`dispatch` схлопывает несколько изменений по товару за цикл и помечает
    поглощённые статусом `sent` — на площадку они не уходили, и `sent_at` у них
    пуст. Без условия на `sent_at` любая такая строка сошла бы за отправку и
    открыла бы отзыв там, где отзывать нечего."""
    from app.transmit import ever_transmitted

    account = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(web_db, broadcast=True)
    web_db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=18, sent_quantity=None,
        reason="order", status=DispatchStatus.sent, sent_at=None,
        last_error="поглощено более новым изменением в этом цикле"))
    web_db.commit()

    assert ever_transmitted(web_db, "u1", account.id) is False


def test_a_queued_but_unsent_row_is_not_a_transmission(web_db):
    """Запись, которая до площадки не доехала, ничего туда не положила."""
    from app.transmit import ever_transmitted

    account = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    _product(web_db, broadcast=True)
    web_db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=18,
                                 reason="manual_enable"))
    web_db.commit()

    assert ever_transmitted(web_db, "u1", account.id) is False


def test_turning_broadcast_off_skips_cabinets_that_never_got_anything(
        logged_in_client, web_db):
    from app.timeutils import now_utc

    wb = make_account(web_db, Platform.wb, name="ИП ЯВОРСКАЯ")
    kit = make_account(web_db, Platform.kit, name="КИТ")
    _product(web_db, broadcast=True)
    web_db.add(SyncSetting(uid_1c="u1", account_id=wb.id, enabled=True))
    web_db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    web_db.add(DispatchQueueItem(
        uid_1c="u1", account_id=wb.id, quantity=18, sent_quantity=46,
        reason="manual_enable", status=DispatchStatus.sent, sent_at=now_utc()))
    web_db.commit()

    logged_in_client.post("/products/u1/broadcast", data={"enabled": "false"})

    withdrawals = web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.reason == "broadcast_off").all()
    assert [w.account_id for w in withdrawals] == [wb.id]

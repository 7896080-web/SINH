"""«Актуализирован» — свойство пары товар+кабинет, а не одного товара.

Разбор 18.09 на боевом сервере. По товару прошёл расчёт: подняли 7 заказов с
одного кабинета WB, провели перемещения в 1С, остаток сошёлся, строка загорелась
«актуализирован». После этого оператор спросил: а если я сейчас отмечу Яндекс
KIT — туда сразу уйдёт остаток? Уходил. Через 45 секунд, полным числом, хотя
продажи KIT с базовой даты никто не поднимал и в 1С их нет: остаток по этому
кабинету завышен ровно на них.

Причина в том, что расчёт спрашивает заказы ТОЛЬКО у кабинетов, отмеченных в его
момент (`recalc._enabled_accounts`), а отметка «актуализирован» ставилась на
товар целиком. Кабинет, отмеченный позже, наследовал чужую проверку.

Здесь закреплено: расчёт запоминает, чьи заказы он реально прочитал, и на
кабинет вне этого списка не уходит ни остаток, ни ноль — до пересчёта.
"""
from datetime import date

from app.models import (Barcode, DispatchQueueItem, Platform, Product, SyncSetting)
from app.recalc import catch_up_product
from app.transmit import covered_accounts, enqueue_full_resend, explain, quantity_for_account
from app.workers.platform_clients.base import PlatformOrder
from app.workers.scheduler import PENDING_WAREHOUSE_NAME
from tests.factories import make_account

DAY = date(2026, 8, 7)


class FakeClient:
    def __init__(self, orders=(), unresolved=0):
        self.orders = list(orders)
        # Столько строк заказов площадка не дала опознать (у Kit — 429 на запросе
        # варианта). Клиент считает их сам, см. KitClient.last_unresolved.
        self.last_unresolved = unresolved

    def get_orders_since(self, since):
        return list(self.orders)


def _wh(platform):
    return PENDING_WAREHOUSE_NAME.get(platform, "Ожидает")


def _product(db, uid="u1", stock=20, broadcast=False):
    p = Product(uid_1c=uid, article="A-1", name="Товар", stock_on_hand=stock, reserve=0,
                broadcast_enabled=broadcast, offset_base_date=DAY,
                offset_base_stock=stock, fact_at_date=stock)
    db.add(p)
    db.add(Barcode(barcode=f"bc-{uid}", uid_1c=uid))
    db.commit()
    return p


def _tick(db, uid, account_id, enabled=True):
    db.add(SyncSetting(uid_1c=uid, account_id=account_id, enabled=enabled))
    db.commit()


# ------------------------------------------------ расчёт запоминает кабинеты

def test_the_catch_up_records_which_cabinets_it_actually_read(db):
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    product = _product(db)
    _tick(db, "u1", wb.id)

    catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)
    db.commit()

    db.refresh(product)
    assert product.recalc_done_at is not None
    assert covered_accounts(product) == {wb.id}


def test_a_cabinet_ticked_after_the_catch_up_is_not_covered(db):
    """Тот самый вопрос оператора: «а если я сейчас поставлю галочку на КИТ?»"""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    kit = make_account(db, Platform.kit, name="КИТ")
    product = _product(db, broadcast=True)
    _tick(db, "u1", wb.id)

    catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)
    _tick(db, "u1", kit.id)          # галочку поставили ПОСЛЕ расчёта

    assert covered_accounts(product) == {wb.id}

    setting = db.query(SyncSetting).filter(SyncSetting.account_id == kit.id).first()
    result = explain(product, setting, kit)
    assert result.quantity == 0
    assert "не покрывал" in result.reason
    # Прогноза «уйдёт N после включения» здесь быть не должно: правильный ответ —
    # сначала пересчёт, и после него число всё равно станет другим.
    assert result.potential == 0


def test_nothing_is_dispatched_to_a_cabinet_outside_the_catch_up(db):
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    kit = make_account(db, Platform.kit, name="КИТ")
    product = _product(db, broadcast=True)
    _tick(db, "u1", wb.id)

    catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)
    _tick(db, "u1", kit.id)

    assert quantity_for_account(db, "u1", kit.id, product.stock_on_hand) == 0
    assert quantity_for_account(db, "u1", wb.id, product.stock_on_hand) > 0


def test_no_queue_item_at_all_for_an_uncovered_cabinet(db):
    """Ноль в очередь ставить тоже нельзя: рассылка его ОТПРАВИТ, а для карточки,
    на которую мы ни разу ничего не слали, это обнуление, а не отзыв остатка."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    kit = make_account(db, Platform.kit, name="КИТ")
    product = _product(db, broadcast=True)
    _tick(db, "u1", wb.id)

    catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)
    db.query(DispatchQueueItem).delete()
    db.commit()

    enqueue_full_resend(db, "u1", kit.id)
    db.commit()

    assert db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == kit.id).count() == 0


def test_a_product_never_caught_up_keeps_the_old_behaviour(db):
    """Товары, настроенные до появления расчёта, ступень не трогает: у них
    `recalc_done_at` пуст, и ворота закрыты главным выключателем, а не ею."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    product = _product(db, broadcast=True)
    product.offset_base_date = None
    product.offset_base_stock = None
    product.fact_at_date = None
    _tick(db, "u1", wb.id)
    db.commit()

    assert quantity_for_account(db, "u1", wb.id, product.stock_on_hand) > 0


# ------------------------------------------------ потерянные строки заказов

def test_lost_order_lines_block_the_done_mark(db):
    """Площадка ответила, но часть строк опознать не удалось (429 у Kit на
    запросе варианта). Раньше такой заказ молча выбрасывался, проблем не
    появлялось, и товар получал «актуализирован» по заказам, которых не видел."""
    kit = make_account(db, Platform.kit, name="КИТ")
    product = _product(db)
    _tick(db, "u1", kit.id)

    stats = catch_up_product(db, product, lambda d, aid: FakeClient(unresolved=3), _wh)
    db.commit()

    db.refresh(product)
    assert product.recalc_done_at is None
    assert any("3 строкам" in p for p in stats["problems"])
    assert covered_accounts(product) == set()


def test_an_honest_zero_still_counts_as_checked(db):
    """Ноль заказов — допустимый ответ. Блокировать надо неполноту, а не тишину:
    иначе товар, который просто не продавался, нельзя было бы включить."""
    kit = make_account(db, Platform.kit, name="КИТ")
    product = _product(db)
    _tick(db, "u1", kit.id)

    catch_up_product(db, product, lambda d, aid: FakeClient(orders=[]), _wh)
    db.commit()

    db.refresh(product)
    assert product.recalc_done_at is not None
    assert covered_accounts(product) == {kit.id}


def test_an_order_that_did_arrive_is_still_applied(db):
    """Страховка от перегиба: новая ступень не должна мешать обычному расчёту."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    product = _product(db, stock=20)
    _tick(db, "u1", wb.id)
    order = PlatformOrder(order_id="o1", barcode="bc-u1", quantity=2,
                          raw_status="new", order_date=date(2026, 8, 11))

    stats = catch_up_product(db, product, lambda d, aid: FakeClient([order]), _wh)

    assert stats["applied"] == 1
    db.refresh(product)
    assert product.stock_on_hand == 18


# ------------------------------------------------ расчёт толкает новый кабинет

def test_a_newly_covered_cabinet_gets_the_stock_pushed_to_it(db):
    """18.09: галочку на Kit поставили, расчёт её покрыл — и наружу не ушло
    ничего. Ворота открылись, а толкнуть было некому: событие, которое ставит
    доотправку, случилось ДО расчёта, когда ступень 2 справедливо отказала.
    Карточка на Kit так и осталась стоять в нуле, который мы туда и отправили."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    kit = make_account(db, Platform.kit, name="КИТ")
    product = _product(db, broadcast=True)
    _tick(db, "u1", wb.id)

    catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)   # покрыт только WB
    db.commit()
    _tick(db, "u1", kit.id)
    db.query(DispatchQueueItem).delete()
    db.commit()

    catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)   # теперь и Kit
    db.commit()

    queued = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == kit.id).all()
    assert len(queued) == 1
    assert queued[0].reason == "recalc_covered"


def test_a_cabinet_already_covered_is_not_pushed_again(db):
    """Повторный расчёт не должен сыпать доотправки на ровном месте."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    product = _product(db, broadcast=True)
    _tick(db, "u1", wb.id)

    catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)
    db.commit()
    db.query(DispatchQueueItem).delete()
    db.commit()

    catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)
    db.commit()

    assert db.query(DispatchQueueItem).count() == 0


def test_nothing_is_pushed_while_broadcast_is_off(db):
    """Расчёт идёт ДО включения трансляции — и сам её включать не должен."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    product = _product(db, broadcast=False)
    _tick(db, "u1", wb.id)

    catch_up_product(db, product, lambda d, aid: FakeClient(), _wh)
    db.commit()

    assert db.query(DispatchQueueItem).count() == 0

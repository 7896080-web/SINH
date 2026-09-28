"""Защита от двойной отмены — со стороны приложения.

Отмена перемещений в 1С НЕ идемпотентна — в отличие от `CREATE_MOVEMENT`, где
идемпотентность по номеру заказа сделана и проверена на бою 19.09. Повторная
отмена создаёт ЛИШНИЙ ПРИХОД товара, которого не было: до 19.09 — потому что
обработка находила по шаблону собственные реверсы и отменяла заодно и возврат,
после 19.09 — потому что реверсы из выборки исключены, но исходное перемещение
остаётся проведённым, находится снова и получает ВТОРОЙ обратный документ.
Механизм поменялся, вывод нет.

Поэтому гарантируем со своей стороны: второе задание `CANCEL_MOVEMENT` по одному
заказу и кабинету не создаётся никогда, в каком бы состоянии ни было первое.
"""
import pytest

from app.models import (Barcode, FtpTask, FtpTaskStatus, OrderProcessStatus, Platform,
                        ProcessedOrder, Product, SyncSetting)
from app.workers.order_poller import (existing_cancel_task, process_cancellation,
                                      process_new_order)
from app.workers.platform_clients.base import PlatformOrder
from tests.factories import make_account


def _order_accepted(db, quantity: int = 3, stock: int = 10, is_test: bool = False):
    """Принятый заказ: товар, кабинет, ProcessedOrder и задание на перемещение."""
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=stock,
                   broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    order_id = ("TEST-1" if is_test else "o1")
    process_new_order(db, PlatformOrder(order_id=order_id, barcode="111", quantity=quantity,
                                        raw_status="new"),
                      account, "WB.Ожидает", is_test=is_test)
    record = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == order_id).first()
    return account, record, order_id


def _cancel(db, account, record, order_id, quantity: int = 3, is_test: bool = False,
            partial: bool = False):
    order = PlatformOrder(order_id=order_id, barcode="111", quantity=quantity,
                          raw_status="cancelled", is_cancellation=True,
                          is_partial_refund=partial,
                          refused_quantity=quantity if partial else 0)
    return process_cancellation(db, order, record, account, is_test=is_test)


def _cancel_tasks(db, is_test: bool = False) -> list[FtpTask]:
    return db.query(FtpTask).filter(FtpTask.command == "CANCEL_MOVEMENT",
                                    FtpTask.is_test.is_(is_test)).all()


# ------------------------------------------------ штатный путь не изменился

def test_first_cancellation_creates_the_task(db):
    account, record, order_id = _order_accepted(db)

    result = _cancel(db, account, record, order_id)

    assert result["status"] == "reversed"
    assert result["duplicate_cancel"] is False
    assert len(_cancel_tasks(db)) == 1


def test_first_cancellation_returns_the_stock(db):
    account, record, order_id = _order_accepted(db, quantity=3, stock=10)
    assert db.query(Product).first().stock_on_hand == 7

    _cancel(db, account, record, order_id)

    assert db.query(Product).first().stock_on_hand == 10


# ------------------------------------------------ второго задания не будет

@pytest.mark.parametrize("status", [
    FtpTaskStatus.pending,     # ещё не отправлено
    FtpTaskStatus.sent,        # в 1С, ответа нет
    FtpTaskStatus.done,        # 1С отмену провела — повтор и есть фантом
    FtpTaskStatus.timeout,     # ответа нет: провела или нет, неизвестно
    FtpTaskStatus.failed,      # 1С отказала — нужен разбор, а не повтор вслепую
])
def test_second_cancellation_creates_no_second_task(db, status):
    account, record, order_id = _order_accepted(db)
    _cancel(db, account, record, order_id)
    task = _cancel_tasks(db)[0]
    task.status = status
    db.commit()

    result = _cancel(db, account, record, order_id)

    assert result["duplicate_cancel"] is True
    assert result["ftp_task_id"] == task.id      # ссылаемся на уже существующее
    assert len(_cancel_tasks(db)) == 1


def test_partial_refunds_create_no_task_at_all(db):
    """Частичный возврат не заводит задания в 1С ВООБЩЕ — ни первого, ни второго.

    Раньше первое задание создавалось, и это было полдела: `CANCEL_MOVEMENT`
    отменяет ВЕСЬ заказ (количества в команде нет ни поля), а себе мы возвращали
    только отказанную часть. Заказ на 5, отказ от 2: у нас +2, в 1С +5, и
    разницу в три единицы часовая сверка втягивает в остаток как приход — мы
    начинаем продавать отгруженное. Второй отказ по тому же заказу отбрасывался
    как дубль, то есть остаток по нему не возвращался вовсе, а площадка
    приносила эту отмену каждые две минуты всё окно открытых заказов.

    Провести частичный отказ нечем, пока протокол 1С не научится принимать
    количество в команде отмены. До тех пор — отказ с записью в журнал.
    """
    account, record, order_id = _order_accepted(db, quantity=5)
    stock_before = db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand

    first = _cancel(db, account, record, order_id, quantity=2, partial=True)
    second = _cancel(db, account, record, order_id, quantity=2, partial=True)

    assert first["status"] == "unsupported"
    assert second["status"] == "unsupported"
    assert _cancel_tasks(db) == []
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == stock_before


# ------------------------------------------------ границы

def test_other_cabinet_with_the_same_order_id_is_not_blocked(db):
    """Номера заказов у разных кабинетов могут совпадать — чужая отмена не
    должна блокировать нашу."""
    account, record, order_id = _order_accepted(db)
    _cancel(db, account, record, order_id)

    other = make_account(db, platform=Platform.ozon, name="Другой кабинет", warehouse_id="wh-2")
    db.add(SyncSetting(uid_1c="u1", account_id=other.id, enabled=True))
    db.commit()
    process_new_order(db, PlatformOrder(order_id=order_id, barcode="111", quantity=1,
                                        raw_status="new"),
                      other, "OZON.Ожидает")
    other_record = db.query(ProcessedOrder).filter(
        ProcessedOrder.account_id == other.id, ProcessedOrder.order_id == order_id).first()

    result = _cancel(db, other, other_record, order_id, quantity=1)

    assert result["duplicate_cancel"] is False
    assert len(_cancel_tasks(db)) == 2


def test_simulation_and_production_do_not_block_each_other(db):
    """Граница is_test: симуляция не видит боевых заданий и наоборот."""
    account, record, order_id = _order_accepted(db, is_test=True)
    _cancel(db, account, record, order_id, is_test=True)

    assert len(_cancel_tasks(db, is_test=True)) == 1
    assert existing_cancel_task(db, order_id, account.id, is_test=False) is None


def test_creating_movement_is_not_affected(db):
    """Защита касается только отмены: задания на создание перемещения по разным
    заказам создаются как и раньше."""
    account, record, order_id = _order_accepted(db)
    process_new_order(db, PlatformOrder(order_id="o2", barcode="111", quantity=1,
                                        raw_status="new"),
                      account, "WB.Ожидает")

    created = db.query(FtpTask).filter(FtpTask.command == "CREATE_MOVEMENT").all()
    assert len(created) == 2


def test_helper_finds_the_task_in_any_status(db):
    account, record, order_id = _order_accepted(db)
    _cancel(db, account, record, order_id)
    task = _cancel_tasks(db)[0]

    for status in FtpTaskStatus:
        task.status = status
        db.commit()
        assert existing_cancel_task(db, order_id, account.id) is not None


def test_stock_is_not_returned_twice(db):
    """Вторая половина защиты: повторная отмена не должна ещё раз накрутить
    остаток. Иначе спасение от фантома в 1С обернулось бы оверселлом у нас —
    товар вернулся бы на склад дважды, а физически его нет."""
    account, record, order_id = _order_accepted(db, quantity=3, stock=10)
    _cancel(db, account, record, order_id)
    assert db.query(Product).first().stock_on_hand == 10

    record.status = OrderProcessStatus.processed      # как будто площадка прислала снова
    db.commit()
    result = _cancel(db, account, record, order_id)

    assert result["status"] == "duplicate"
    assert db.query(Product).first().stock_on_hand == 10


def test_duplicate_does_not_enqueue_a_second_dispatch(db):
    """И на площадки повторная отмена ничего не рассылает: значение не менялось."""
    from app.models import DispatchQueueItem

    account, record, order_id = _order_accepted(db)
    _cancel(db, account, record, order_id)
    before = db.query(DispatchQueueItem).count()

    record.status = OrderProcessStatus.processed
    db.commit()
    _cancel(db, account, record, order_id)

    assert db.query(DispatchQueueItem).count() == before


def test_duplicate_closes_the_order_so_polling_stops(db):
    """Заказ всё же закрываем: иначе опрос приносил бы его каждые две минуты и
    каждый раз упирался в ту же проверку."""
    account, record, order_id = _order_accepted(db)
    _cancel(db, account, record, order_id)

    record.status = OrderProcessStatus.processed
    db.commit()
    _cancel(db, account, record, order_id)

    db.refresh(record)
    assert record.status == OrderProcessStatus.cancelled

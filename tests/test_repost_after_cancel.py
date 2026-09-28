"""Повтор зависшего перемещения по УЖЕ ОТМЕНЁННОМУ заказу — и его разбор.

Дыра, найденная аудитом 22.09 двумя независимыми проходами. Последовательность,
которая её открывает, вся штатная:

1. заказ принят, `CREATE_MOVEMENT` ушёл файлом, 1С роняет файл ЦЕЛИКОМ и не
   отвечает — через пятнадцать минут задание становится `timeout`;
2. площадка приносит отмену. `process_cancellation` возвращает остаток, заводит
   `CANCEL_MOVEMENT` и ставит заказу `cancelled`, а зависшее создание НЕ ТРОГАЕТ —
   у него нет для этого ни кода, ни оснований;
3. отмена уезжает ближайшим минутным файлом. `ОтменитьПеремещенияЗаказа`
   документа не находит (его и не было) и отвечает `OK|нет документов для
   отмены` — задание отмены закрывается как выполненное;
4. через полчаса `repost_stuck_movements` возвращает создание в `pending`, и 1С
   СОЗДАЁТ перемещение по заказу, которого больше нет.

Отменить этот документ уже нечем: `existing_cancel_task` видит закрытое
`CANCEL_MOVEMENT` в любом статусе и второго не выпустит, а сам заказ со статусом
`cancelled` из опроса отмен выпал. Единица навсегда числится отгруженной, лёжа
на ЦС: остаток занижен, наружу уходит меньше, чем есть, и часовая сверка это
закрепляет.

Идемпотентность 1С, на которую опирается весь механизм повтора, здесь не
помогает, а работает против: она обещана по СУЩЕСТВУЮЩЕМУ документу, а тут
документа нет вовсе.
"""
from datetime import timedelta

import pytest

from app.models import (FtpTask, FtpTaskStatus, OrderProcessStatus, Platform,
                        ProcessedOrder)
from app.timeutils import now_utc
from app.workers import ftp_channel as ch
from tests.factories import make_account


@pytest.fixture
def on(monkeypatch):
    """Автоповтор включён — как на бою с 19.09."""
    monkeypatch.setenv(ch.MOVEMENT_REPOST_ENV, "1")


def _task(db, account, *, command="CREATE_MOVEMENT", order_id="5638678769",
          age_minutes=60, status=FtpTaskStatus.timeout, reposts=0):
    t = FtpTask(command=command, barcode="2000932307169", warehouse_from="ЦС Склад",
                warehouse_to="Wildberries_Склад_FBO", quantity=1, order_id=order_id,
                account_id=account.id, status=status, repost_count=reposts,
                is_test=False, sent_at=now_utc() - timedelta(minutes=age_minutes))
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _order(db, account, order_id="5638678769", status=OrderProcessStatus.cancelled):
    db.add(ProcessedOrder(account_id=account.id, order_id=order_id, uid_1c="u1",
                          quantity=1, status=status))
    db.commit()


# ------------------------------------------- повтор по отменённому не уходит

def test_a_create_for_a_cancelled_order_is_never_reposted(db, on):
    account = make_account(db, Platform.wb)
    task = _task(db, account)
    _order(db, account)

    stats = ch.repost_stuck_movements(db)

    db.refresh(task)
    assert task.status is FtpTaskStatus.timeout, "задание вернули в очередь на отправку"
    assert stats["reposted"] == 0
    assert stats["order_cancelled"] == 1


def test_a_create_for_a_live_order_is_still_reposted(db, on):
    """Обратная сторона: обычное зависшее задание повторяться обязано — ради
    этого механизм и написан, и глушить его целиком нельзя."""
    account = make_account(db, Platform.wb)
    task = _task(db, account)
    _order(db, account, status=OrderProcessStatus.processed)

    stats = ch.repost_stuck_movements(db)

    db.refresh(task)
    assert task.status is FtpTaskStatus.pending
    assert stats["reposted"] == 1 and stats["order_cancelled"] == 0


def test_a_cancelled_order_in_another_cabinet_does_not_block_the_repost(db, on):
    """Номера заказов у разных площадок могут совпасть. Сверять их без кабинета
    значило бы заглушить повтор по чужому живому заказу."""
    wb = make_account(db, Platform.wb, name="WB")
    kit = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")
    task = _task(db, wb, order_id="777")
    _order(db, kit, order_id="777")          # отменён СОСЕДНИЙ кабинет

    stats = ch.repost_stuck_movements(db)

    db.refresh(task)
    assert task.status is FtpTaskStatus.pending
    assert stats["reposted"] == 1


# ------------------------------------------- зато уходит в ручной разбор

def test_a_create_for_a_cancelled_order_goes_to_manual_review(db, on):
    """Повторять нельзя, но и молчать нельзя: `timeout` вечно считается «в пути»,
    и остаток по товару занижен, пока задание висит. Есть документ в 1С или нет —
    знает только человек, который туда посмотрит."""
    account = make_account(db, Platform.wb)
    task = _task(db, account)
    _order(db, account)

    assert [t.id for t in ch.tasks_needing_review(db)] == [task.id]


def test_a_stuck_cancellation_goes_to_manual_review(db, on):
    """Отмены автоповтор не берёт НИКОГДА (про идемпотентность отмены 1С ничего
    не обещала), поэтому `repost_count` у них не растёт и не может. Условие
    разбора требовало исчерпанных повторов — и зависшая отмена не попадала в него
    вовсе: при включённом на бою автоповторе она висела бы бесконечно.

    Цена у этого обратная обычной: открытая отмена вычитается из «в пути», то
    есть остаток ЗАВЫШЕН, наружу уходит больше, чем есть. Это оверселл."""
    account = make_account(db, Platform.wb)
    task = _task(db, account, command="CANCEL_MOVEMENT")

    assert [t.id for t in ch.tasks_needing_review(db)] == [task.id]


def test_a_stuck_confirmation_goes_to_manual_review_too(db, on):
    """Подтверждение автоповтор тоже не берёт. Остаток оно не искажает, но
    висящее невидимое задание — само по себе то, что человек должен закрыть."""
    account = make_account(db, Platform.wb)
    task = _task(db, account, command="CONFIRM_MOVEMENT")

    assert [t.id for t in ch.tasks_needing_review(db)] == [task.id]


def test_a_live_stuck_create_still_waits_for_the_repost(db, on):
    """И главное, чего портить нельзя: обычное зависшее создание в разбор рано —
    его ещё возьмёт автоповтор, а звать человека к тому, что рассосётся само,
    значит приучить его не ходить по этим ссылкам."""
    account = make_account(db, Platform.wb)
    _task(db, account)
    _order(db, account, status=OrderProcessStatus.processed)

    assert ch.tasks_needing_review(db) == []


def test_a_fresh_timeout_is_not_reviewed_even_if_cancelled(db, on):
    """Опоздавший ответ 1С закрывает задание сам, и полчаса на это отведены
    намеренно. Отменённый заказ этот срок не отменяет."""
    account = make_account(db, Platform.wb)
    _task(db, account, age_minutes=1)
    _order(db, account)

    assert ch.tasks_needing_review(db) == []

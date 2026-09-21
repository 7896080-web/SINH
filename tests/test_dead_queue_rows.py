"""Отказ рассылки, который уже ничего не означает, — не расхождение.

21.09 на бою отчёт держал КРИТИЧНУЮ находку по записи очереди **id=1** — самой
первой в системе, созданной 14.09, от дефекта Kit, починенного двадцатого.
Кабинет по этой паре не отмечен, и непустой остаток мы туда не отправляли ни
разу. Значит следствие находки — «остаток списан, а площадка продолжает
продавать по старому числу» — неправда: площадка держит наше число, только если
мы его туда посылали. А запись мёртвая: рассылка её не возьмёт, статуса она не
сменит никогда, и отчёт остался бы красным навсегда.

Условия отбрасывания ОБА, и второе тут главное. Снятая галочка сама по себе
отказ не отменяет: отправляли 50, человек снял галочку, отзыв не доехал — на
площадке лежит 50, и она продаёт то, чего нет. Об этом ниже отдельный тест.
"""

from app.models import (DispatchQueueItem, DispatchStatus, Platform, Product,
                        SyncSetting)
from app.report import CARD_MISSING_MARKS, collect_findings
from app.timeutils import now_utc
from tests.factories import make_account


def _pair(db, account, *, ticked: bool, transmitted: bool = False,
          error: str = "400 площадка недоступна"):
    db.add(Product(uid_1c="u1", article="2403", name="Конко Джемпер",
                   stock_on_hand=5, broadcast_enabled=True))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=ticked))
    if transmitted:
        # Непустой остаток, реально ушедший: именно он и означает, что площадка
        # держит НАШЕ число.
        db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=50,
                                 status=DispatchStatus.sent, reason="order",
                                 is_test=False, sent_quantity=50,
                                 sent_at=now_utc() - __import__("datetime").timedelta(days=2)))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5,
                             status=DispatchStatus.error, reason="test",
                             is_test=False, last_error=error))
    db.commit()


def _keys(db):
    return {f.key for f in collect_findings(db)}


def _kit(db):
    return make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")


# ------------------------------------------ мёртвое находкой не считаем

def test_an_unticked_pair_we_never_sent_to_is_not_a_finding(db):
    """Ровно случай id=1 с боя: галочки нет, остаток туда не уходил никогда."""
    _pair(db, _kit(db), ticked=False, transmitted=False)

    assert "dispatch_errors" not in _keys(db)


def test_the_same_holds_for_unknown_sku(db):
    """По такой паре решать нечего и здесь: кабинет не отмечен."""
    _pair(db, _kit(db), ticked=False, transmitted=False,
          error=f"…{CARD_MISSING_MARKS[0]}…")

    assert "unknown_sku" not in _keys(db)


# ------------------------------------ а всё, что может значить, оставляем

def test_a_failed_withdrawal_is_still_a_finding(db):
    """Главная граница. Отправляли 50, галочку сняли, отзыв не доехал — на
    площадке лежит 50, и она продаёт то, чего нет. Снятая галочка отказ НЕ
    отменяет."""
    _pair(db, _kit(db), ticked=False, transmitted=True)

    assert "dispatch_errors" in _keys(db)


def test_a_ticked_pair_is_still_reported(db):
    """Даже первая в жизни отправка, не доехавшая, — находка: пару включили,
    значит ждут, что остаток туда поедет."""
    _pair(db, _kit(db), ticked=True, transmitted=False)

    assert "dispatch_errors" in _keys(db)


def test_a_product_missing_from_the_catalogue_is_not_dropped(db):
    """Запись очереди переживает удаление товара. Отсутствие товара поводом
    молчать не считается — иначе находка насчитает больше, чем покажет."""
    account = _kit(db)
    db.add(SyncSetting(uid_1c="призрак", account_id=account.id, enabled=True))
    db.add(DispatchQueueItem(uid_1c="призрак", account_id=account.id, quantity=1,
                             status=DispatchStatus.error, reason="test",
                             is_test=False, last_error="400"))
    db.commit()

    assert "dispatch_errors" in _keys(db)


def test_it_comes_back_when_the_cabinet_is_ticked(db):
    """Ничего не удаляется — мы лишь не считаем расхождением то, что им не
    является. Отметили кабинет — находка вернулась сама."""
    account = _kit(db)
    _pair(db, account, ticked=False, transmitted=False)
    assert "dispatch_errors" not in _keys(db)

    db.query(SyncSetting).filter(SyncSetting.uid_1c == "u1").one().enabled = True
    db.commit()

    assert "dispatch_errors" in _keys(db)


def test_the_row_stays_in_the_database(db):
    """Отчёт ТОЛЬКО ЧИТАЕТ. Ничего не удаляем и не закрываем."""
    _pair(db, _kit(db), ticked=False, transmitted=False)

    collect_findings(db)

    assert db.query(DispatchQueueItem).filter(
        DispatchQueueItem.status == DispatchStatus.error).count() == 1


def test_a_tick_on_another_cabinet_does_not_cover_this_one(db):
    """Пара считается парой, а не товаром: отмеченный сосед не оправдывает
    неотмеченного."""
    kit = _kit(db)
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    db.add(Product(uid_1c="u1", article="2403", name="Джемпер",
                   stock_on_hand=5, broadcast_enabled=True))
    db.add(SyncSetting(uid_1c="u1", account_id=wb.id, enabled=True))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=kit.id, quantity=5,
                             status=DispatchStatus.error, reason="test",
                             is_test=False, last_error="400"))
    db.commit()

    assert "dispatch_errors" not in _keys(db)

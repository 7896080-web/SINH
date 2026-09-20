"""Массовая переотправка остатков на площадки.

Понадобилась после 19.09: пока в кабинет WB писала вторая система (та, с которой
идёт переход), наши числа там перетирались, а у нас всё это время значилось
«отправлено». Рассылка событийная — отправив число, она к нему не возвращается,
и когда вторая система замолкает, на площадке остаётся ЕЁ картина. Сдвинуть её
нечем: событий по товару больше не будет, остаток-то не менялся. Команда и есть
такое событие, поставленное руками.

Главное, что проверяется здесь, — что команда НИЧЕГО НЕ ОБХОДИТ. Массовая
операция без гейтов отправила бы ноль по каждой паре, до которой мы ещё не
дошли, и обнулила бы живые карточки разом по всему каталогу — то же, что
случилось 18.09 с Озоном и Kit, только сразу везде.
"""

from app.models import (DispatchQueueItem, DispatchStatus, Platform, Product,
                        SyncSetting)
from app.timeutils import now_utc
from app.transmit import enqueue_resend_all
from tests.factories import make_account


def _product(db, uid="u1", stock=10, broadcast=True, recalc_ids=None, recalc=True):
    db.add(Product(uid_1c=uid, article="A1", name="Товар", stock_on_hand=stock,
                   broadcast_enabled=broadcast,
                   recalc_done_at=now_utc() if recalc else None,
                   recalc_account_ids=recalc_ids))
    db.commit()


def _queue(db, uid=None):
    q = db.query(DispatchQueueItem)
    if uid:
        q = q.filter(DispatchQueueItem.uid_1c == uid)
    return q.all()


# --------------------------------------------------------------- делает дело

def test_a_broadcasting_product_is_queued_for_its_marked_cabinets(db):
    account = make_account(db, name="ИП ЯВОРСКАЯ")
    _product(db, stock=68, recalc_ids=str(account.id))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    stats = enqueue_resend_all(db)

    assert stats == {"products": 1, "queued": 1}
    item = _queue(db)[0]
    assert item.account_id == account.id
    assert item.quantity == 68, "в очередь кладётся ТЕКУЩИЙ остаток"
    assert item.reason == "manual_resend_all"
    assert item.status == DispatchStatus.pending


def test_every_marked_cabinet_of_the_product_gets_a_row(db):
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    kit = make_account(db, Platform.kit, name="КИТ")
    _product(db, recalc_ids=f"{wb.id},{kit.id}")
    db.add(SyncSetting(uid_1c="u1", account_id=wb.id, enabled=True))
    db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    db.commit()

    stats = enqueue_resend_all(db)

    assert stats["queued"] == 2
    assert {i.account_id for i in _queue(db)} == {wb.id, kit.id}


def test_running_it_twice_is_safe(db):
    """Число берётся не из команды: в очередь кладётся текущий остаток, а итог
    считает лестница в момент отправки. Поэтому повтор отправит то же самое, что
    ушло бы само, — команду можно давать сколько угодно раз."""
    account = make_account(db)
    _product(db, stock=7, recalc_ids=str(account.id))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    enqueue_resend_all(db)
    enqueue_resend_all(db)

    assert {i.quantity for i in _queue(db)} == {7}


# ------------------------------------------------------- и ничего не обходит

def test_a_silent_product_is_never_queued(db):
    """Товар с выключенной трансляцией: по нему лестница даёт ноль, и этот ноль
    уехал бы на площадку. Для карточки, на которую мы ни разу не отправляли, это
    не отзыв остатка, а обнуление чужих продаж."""
    account = make_account(db)
    _product(db, broadcast=False, recalc_ids=str(account.id))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    stats = enqueue_resend_all(db)

    assert stats == {"products": 0, "queued": 0}
    assert _queue(db) == []


def test_a_cabinet_the_recalc_did_not_cover_is_never_queued(db):
    """«Актуализирован» — свойство пары товар+кабинет. По кабинету вне расчёта
    уйдёт ноль (ступень 2 лестницы), а не сверенный с его продажами остаток."""
    covered = make_account(db, Platform.wb, name="Покрытый")
    untouched = make_account(db, Platform.kit, name="Не покрытый")
    _product(db, recalc_ids=str(covered.id))
    db.add(SyncSetting(uid_1c="u1", account_id=covered.id, enabled=True))
    db.add(SyncSetting(uid_1c="u1", account_id=untouched.id, enabled=True))
    db.commit()

    stats = enqueue_resend_all(db)

    assert stats["queued"] == 1
    assert [i.account_id for i in _queue(db)] == [covered.id]


def test_an_unmarked_cabinet_is_never_queued(db):
    """Галочка снята — оператор решил туда не передавать. Массовая команда это
    решение не отменяет."""
    account = make_account(db)
    _product(db, recalc_ids=str(account.id))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=False))
    db.commit()

    assert enqueue_resend_all(db)["queued"] == 0


def test_a_product_without_any_cabinet_counts_but_queues_nothing(db):
    """Товар транслируется, но не отмечен нигде: считать его пропущенным честно,
    а слать некуда."""
    _product(db)
    db.commit()

    assert enqueue_resend_all(db) == {"products": 1, "queued": 0}


def test_an_empty_catalogue_is_not_an_error(db):
    assert enqueue_resend_all(db) == {"products": 0, "queued": 0}


# ------------------------------------------------------------- команда в UI

def test_the_button_queues_and_reports(logged_in_client, web_db):
    from app.models import PlatformAccount

    account = PlatformAccount(platform=Platform.wb, name="ИП ЯВОРСКАЯ", warehouse_id="wh-1")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    web_db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=68,
                       broadcast_enabled=True, recalc_done_at=now_utc(),
                       recalc_account_ids=str(account.id)))
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    r = logged_in_client.post("/diagnostics/resend-all", follow_redirects=False)
    assert r.status_code == 303

    assert web_db.query(DispatchQueueItem).count() == 1
    page = logged_in_client.get("/diagnostics")
    assert "В очередь поставлено 1" in page.text


def test_the_button_says_so_when_there_is_nothing_to_send(logged_in_client, web_db):
    """Молчаливый успех на пустом каталоге оператор прочитает как «отправлено»."""
    r = logged_in_client.post("/diagnostics/resend-all", follow_redirects=False)
    assert r.status_code == 303

    page = logged_in_client.get("/diagnostics")
    assert "Переотправлять нечего" in page.text


def test_the_command_needs_a_login(client):
    r = client.post("/diagnostics/resend-all", follow_redirects=False)

    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")

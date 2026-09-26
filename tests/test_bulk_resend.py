"""Переотправка остатка по отбору — и почему она понадобилась отдельной кнопкой.

Числа на площадке стирают мимо нас: правкой карточек в кабинете, публикацией
витрины, второй системой. Рассылка событийная — отправив число, она считает его
доставленным и сама к нему не возвращается, а остаток у нас не менялся, значит
события не будет вовсе. На витрине навсегда остаётся чужая картина.

Способ вернуть числа был один: выключить трансляцию и включить обратно. Он
работает, и именно так и делали. Но выключение ОТЗЫВАЕТ остаток — отправляет
ноль на живую карточку, — и между двумя нажатиями площадка держит 0 и не
продаёт. Человек своими руками делает ровно то, что чинит, да ещё и сразу по
всему отбору. Это и есть предмет проверок ниже: новая кнопка обязана поставить
в очередь то же самое, но НИ ОДНОГО нуля при этом не отправить.

Второе, не менее важное: `enqueue_full_resend` проверяет трансляцию и покрытие
расчётом, но про галочку кабинета не знает НИЧЕГО — её фильтруют вызывающие.
Поставь кнопка запись по неотмеченной паре, лестница дала бы ноль ступенью 1, и
он уехал бы отзывом на живую карточку: кнопка «верни числа» их бы и стёрла.
"""
from app.models import (DispatchQueueItem, Platform, Product, SyncSetting)
from app.timeutils import now_utc
from tests.factories import make_account


def _product(web_db, uid, stock=20, broadcast=True, recalc_ids=None):
    web_db.add(Product(uid_1c=uid, article=uid, name="Рубашка", stock_on_hand=stock,
                       reserve=0, broadcast_enabled=broadcast,
                       recalc_done_at=now_utc(), recalc_account_ids=recalc_ids))


def _queue(web_db, uid=None, account_id=None):
    q = web_db.query(DispatchQueueItem)
    if uid:
        q = q.filter(DispatchQueueItem.uid_1c == uid)
    if account_id:
        q = q.filter(DispatchQueueItem.account_id == account_id)
    return q.all()


def _resend(client, **extra):
    return client.post("/products/bulk", data={"action": "resend", **extra},
                       follow_redirects=False)


def test_it_queues_the_current_stock_for_the_marked_cabinets(logged_in_client, web_db):
    kit = make_account(web_db, name="КИТ", platform=Platform.kit)
    _product(web_db, "u1", stock=20, recalc_ids=str(kit.id))
    web_db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    web_db.commit()

    assert _resend(logged_in_client, uids=["u1"]).status_code == 303

    rows = _queue(web_db, "u1")
    assert len(rows) == 1, rows
    assert rows[0].account_id == kit.id
    # Именно ТЕКУЩИЙ остаток: число берётся в момент постановки, а итог считает
    # лестница при отправке — поэтому команду безопасно дать дважды подряд.
    assert rows[0].quantity == 20


def test_it_sends_no_zero_the_way_switching_broadcast_off_does(logged_in_client, web_db):
    """Главное отличие от «выключить и включить», ради которого кнопка и есть.

    Выключение трансляции ставит в очередь ОТЗЫВ — ноль на живую карточку.
    Переотправка не ставит нулей вовсе: у товара с остатком в очереди должно
    оказаться его число, а не ноль.
    """
    kit = make_account(web_db, name="КИТ", platform=Platform.kit)
    _product(web_db, "u1", stock=20, recalc_ids=str(kit.id))
    web_db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    web_db.commit()

    _resend(logged_in_client, uids=["u1"])

    assert [r.quantity for r in _queue(web_db, "u1")] == [20]
    assert all(r.quantity != 0 for r in _queue(web_db)), "переотправка отправила ноль"


def test_an_unmarked_pair_is_never_queued(logged_in_client, web_db):
    """Ноль ступенью 1 уехал бы отзывом — кнопка «верни числа» стёрла бы их."""
    kit = make_account(web_db, name="КИТ", platform=Platform.kit)
    _product(web_db, "u1", stock=20, recalc_ids=str(kit.id))
    web_db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=False))
    web_db.commit()

    answer = _resend(logged_in_client, uids=["u1"], account_id=str(kit.id))

    assert _queue(web_db, "u1") == [], "запись по СНЯТОЙ паре попала в очередь"
    assert answer.status_code == 303


def test_a_chosen_cabinet_narrows_the_send_to_it(logged_in_client, web_db):
    """Поправили витрину одного кабинета — остальные трогать незачем."""
    kit = make_account(web_db, name="КИТ", platform=Platform.kit)
    ozon = make_account(web_db, name="ОЗОН", platform=Platform.ozon)
    _product(web_db, "u1", stock=20, recalc_ids=f"{kit.id},{ozon.id}")
    web_db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    web_db.add(SyncSetting(uid_1c="u1", account_id=ozon.id, enabled=True))
    web_db.commit()

    _resend(logged_in_client, uids=["u1"], account_id=str(kit.id))

    assert [r.account_id for r in _queue(web_db, "u1")] == [kit.id]


def test_without_a_cabinet_it_goes_to_every_marked_one(logged_in_client, web_db):
    """Обычный случай «верни числа везде» — иначе по кабинету за раз.

    И «все» здесь значит ВСЕ ОТМЕЧЕННЫЕ, а не все, у кого есть строка настройки.
    Снятый кабинет в наборе обязателен: без него проверка молчала бы, спрашивай
    кнопка про галочку или нет, — а по снятой паре лестница даёт ноль ступенью 1,
    и он уехал бы отзывом на живую карточку.
    """
    kit = make_account(web_db, name="КИТ", platform=Platform.kit)
    ozon = make_account(web_db, name="ОЗОН", platform=Platform.ozon)
    dropped = make_account(web_db, name="ИП ЯВОРСКАЯ", platform=Platform.wb)
    _product(web_db, "u1", stock=20,
             recalc_ids=f"{kit.id},{ozon.id},{dropped.id}")
    web_db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    web_db.add(SyncSetting(uid_1c="u1", account_id=ozon.id, enabled=True))
    web_db.add(SyncSetting(uid_1c="u1", account_id=dropped.id, enabled=False))
    web_db.commit()

    _resend(logged_in_client, uids=["u1"])

    sent_to = sorted(r.account_id for r in _queue(web_db, "u1"))
    assert sent_to == sorted([kit.id, ozon.id]), sent_to
    assert dropped.id not in sent_to, "запись по СНЯТОМУ кабинету попала в очередь"


def test_broadcasting_off_is_refused_and_said_out_loud(logged_in_client, web_db):
    """Гейт тот же, что у любой доотправки: по такой паре ушёл бы ноль.

    И отказ обязан быть СЛЫШНЫМ: оператор решил бы, что числа поехали, а узнал
    бы обратное, только придя смотреть витрину.
    """
    kit = make_account(web_db, name="КИТ", platform=Platform.kit)
    _product(web_db, "u1", stock=20, broadcast=False, recalc_ids=str(kit.id))
    web_db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    web_db.commit()

    _resend(logged_in_client, uids=["u1"], account_id=str(kit.id))
    assert _queue(web_db, "u1") == []

    page = logged_in_client.get("/products")
    assert "Не поставлено 1" in page.text, page.text[:400]


def test_a_cabinet_outside_the_recalc_is_refused(logged_in_client, web_db):
    """Ступень 2 лестницы: расчёт этот кабинет не покрывал — уйдёт ноль."""
    kit = make_account(web_db, name="КИТ", platform=Platform.kit)
    other = make_account(web_db, name="ОЗОН", platform=Platform.ozon)
    # Расчёт покрыл только ОЗОН, а отмечен КИТ.
    _product(web_db, "u1", stock=20, recalc_ids=str(other.id))
    web_db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    web_db.commit()

    _resend(logged_in_client, uids=["u1"], account_id=str(kit.id))

    assert _queue(web_db, "u1") == [], "запись по непокрытому кабинету попала в очередь"


def test_it_changes_nothing_in_the_row_itself(logged_in_client, web_db):
    """Переотправка — не правка: ни одно поле строки меняться не должно.

    Иначе кнопка, которую жмут после каждой правки витрины, потихоньку двигала
    бы порог или дату, и заметить это было бы неоткуда.
    """
    kit = make_account(web_db, name="КИТ", platform=Platform.kit)
    _product(web_db, "u1", stock=20, recalc_ids=str(kit.id))
    web_db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    web_db.commit()
    before = {c.name: getattr(web_db.query(Product).first(), c.name)
              for c in Product.__table__.columns}

    _resend(logged_in_client, uids=["u1"], account_id=str(kit.id))

    web_db.expire_all()
    after = {c.name: getattr(web_db.query(Product).first(), c.name)
             for c in Product.__table__.columns}
    assert before == after, "переотправка изменила строку товара"


def test_the_message_counts_records_not_rows(logged_in_client, web_db):
    """«Изменено строк» тут было бы неправдой: не изменилась ни одна.

    А число записей важнее числа товаров — у товара бывает несколько
    отмеченных кабинетов, и уедет он в каждый.
    """
    kit = make_account(web_db, name="КИТ", platform=Platform.kit)
    ozon = make_account(web_db, name="ОЗОН", platform=Platform.ozon)
    _product(web_db, "u1", stock=20, recalc_ids=f"{kit.id},{ozon.id}")
    web_db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    web_db.add(SyncSetting(uid_1c="u1", account_id=ozon.id, enabled=True))
    web_db.commit()

    _resend(logged_in_client, uids=["u1"])
    page = logged_in_client.get("/products")

    assert "поставлено 2 записей по 1 товарам" in page.text, page.text[:500]
    assert "изменено строк" not in page.text.lower()

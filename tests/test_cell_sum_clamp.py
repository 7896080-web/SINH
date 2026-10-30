"""Обрезка суммы по ячейке — ОДНА, по итогу, а не по каждому слагаемому.

29.10 на бою `2503 SIYAH` и `2403 SIYAH` ведут на одну карточку WB. Остаток
лежал на 2503 (40), баркод карточки привязан к 2403, и заказы списывались с
2403 — тот ушёл в МИНУС (−7). Физически же со склада уехали те самые семь
штук, то есть на складе осталось 33, а на карточку уезжало 40: оверселл.

Причина арифметическая. `shares()` считала вклад каждого жильца через
`sku_quantity`, а та обрезает снизу (`max(0, …)`), — значит минус соседа в
сумму не попадал вовсе. Сумма БЕЗ обрезки по слагаемому даёт ровно 33: минус и
есть мера несписанных продаж.

Обрезать надо один раз, по итогу (`stock_cell.cell_quantity`), а выключатели
становятся ОДНОСТОРОННИМИ: не дают ДОБАВИТЬ остаток пары, которой мы не
управляем, но позволяют ВЫЧЕСТЬ её минус — вещь уехала со склада независимо от
того, отмечена ли пара.
"""
from app.models import (Barcode, DispatchQueueItem, Platform,
                        PlatformCatalogItem, Product, SyncSetting)
from app.stock_cell import cell_quantity, explain_sum, members, shares
from app.transmit import contribution_to_cell, quantity_for_account
from app.workers.dispatch import run_dispatch_cycle
from tests.factories import make_account

CELL = "177:4242"


class _Wb:
    stock_key = "barcode"

    def __init__(self):
        self.sent = []

    def push_stock(self, warehouse_id, items):
        self.sent.append([(i.barcode, i.quantity) for i in items])
        return {"ok": [i.barcode for i in items], "errors": []}


def _row(db, account, uid, barcode, stock, *, cell=CELL, enabled=True,
         broadcast=True, queued=True, threshold=0):
    db.add(Product(uid_1c=uid, article=uid, name="Куртка", stock_on_hand=stock,
                   broadcast_enabled=broadcast,
                   recalc_account_ids=str(account.id)))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=enabled,
                       min_threshold=threshold))
    db.add(PlatformCatalogItem(account_id=account.id, external_id=cell,
                               barcode=barcode, article="WB-2503",
                               name="Карточка"))
    if queued:
        db.add(DispatchQueueItem(uid_1c=uid, account_id=account.id,
                                 quantity=stock, reason="order"))


def _случай_2503(db, *, payer_stock=40, debt=-7):
    """Боевая расстановка 29.10: управляемый жилец и минус на паре без галочки."""
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-2503", "2000000000011", payer_stock)
    _row(db, account, "u-2403", "2000000000022", debt, enabled=False,
         queued=False)
    db.commit()
    return account


def test_the_minus_of_a_neighbour_is_subtracted_from_the_cell(db):
    """Главный случай: 40 и −7 — наружу уходит 33, а не 40.

    Это и есть оверселл 29.10. Семь штук физически уехали со склада, и число
    на карточке обязано их учитывать, кем бы ни был назван плательщик.
    """
    account = _случай_2503(db)

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.sent == [[("2000000000011", 33)]]
    item = db.query(DispatchQueueItem).one()
    assert item.sent_quantity == 33


def test_a_blocked_neighbour_still_cannot_add_its_stock(db):
    """Обратная сторона асимметрии, и она не менее обязательна.

    Пара без галочки — половина карточки, которой мы не управляем: её остаток в
    ячейку не идёт. Сделай мы выключатель симметричным «в обе стороны», на
    карточку уехало бы 58 — прямой оверселл по чужой половине.
    """
    account = _случай_2503(db, debt=18)

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.sent == [[("2000000000011", 40)]]
    # И то же самое слагаемым: выключатель гасит «сколько можно отдать» и не
    # трогает минус. Обе стороны в одном тесте — иначе правило выглядело бы
    # соблюдённым и при симметричном выключателе.
    assert contribution_to_cell(db, "u-2403", account.id, 18) == 0
    assert contribution_to_cell(db, "u-2403", account.id, -7) == -7


def test_a_negative_cell_sum_is_clamped_to_zero_once(db):
    """Минус наружу не уедет: для площадки ноль и есть «не продавать»."""
    account = _случай_2503(db, payer_stock=2, debt=-7)

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.sent == [[("2000000000011", 0)]]
    assert db.query(DispatchQueueItem).one().sent_quantity == 0


def test_the_caveat_says_the_sum_was_clamped(db):
    """«Ушла сумма 0» рядом со слагаемыми 2 и −7 читается как ошибка расчёта:
    человек идёт искать, почему не ушло 2."""
    account = _случай_2503(db, payer_stock=2, debt=-7)

    run_dispatch_cycle(db, {account.id: _Wb()}, [account])

    note = db.query(DispatchQueueItem).one().last_error
    assert "сумма слагаемых -5" in note, note
    assert "уехал ноль" in note, note
    assert "u-2403 -7" in note, note


def test_the_caveat_names_the_negative_member(db):
    """Сумма ушла меньше остатка жильца — и видно, за счёт чего именно."""
    account = _случай_2503(db)

    run_dispatch_cycle(db, {account.id: _Wb()}, [account])

    note = db.query(DispatchQueueItem).one().last_error
    assert "ушла сумма 33" in note, note
    assert "u-2503 40" in note and "u-2403 -7" in note, note


def test_a_lone_product_with_a_negative_stock_still_sends_zero(db):
    """Одиночных товаров — весь каталог, и их путь обязан остаться прежним.

    У одной строки минус значит «отгрузили больше, чем числилось», читатель у
    него свой (`report._check_negative_stock`), а наружу уходит ноль.
    """
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-1", "2000000000011", -5)
    db.commit()

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.sent == [[("2000000000011", 0)]]
    item = db.query(DispatchQueueItem).one()
    assert item.sent_quantity == 0
    assert item.last_error is None, "оговорки про сумму у одиночки быть не должно"


def test_the_cabinet_threshold_suppresses_only_what_it_would_add(db):
    """Порог кабинета — страховой буфер, то есть запрет ДОБАВЛЯТЬ.

    К минусу он отношения не имеет: вещь уехала со склада, и порог её туда не
    вернёт. Проверяются обе стороны — без второй правило выглядело бы
    соблюдённым и при симметричном пороге.
    """
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-мало", "2000000000011", 3, threshold=5, queued=False)
    _row(db, account, "u-минус", "2000000000022", -7, threshold=5, queued=False)
    db.commit()

    assert contribution_to_cell(db, "u-мало", account.id, 3) == 0
    assert contribution_to_cell(db, "u-минус", account.id, -7) == -7


def test_a_holdback_does_not_borrow_from_the_neighbour(db):
    """Вычитается ФИЗИЧЕСКИЙ минус, а не запрет отдавать.

    Порог трансляции и бронь — односторонняя команда «не отдавай столько»: они
    обязаны обнулить вклад СВОЕЙ строки и не имеют права занимать у соседа.
    Считай вклад по всей формуле без обрезки (`остаток − порог`), распроданная
    партия с давно измеренным расхождением тащила бы живую вниз на выдуманное
    число — ошибка в безопасную сторону, но выдуманная, и разбирать её нечем.
    """
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-порог", "2000000000011", 2, queued=False)
    db.commit()
    product = db.query(Product).filter(Product.uid_1c == "u-порог").one()
    product.stock_discrepancy = 5
    product.broadcast_offset = 5          # порог выше остатка: отдавать нечего
    db.commit()

    assert quantity_for_account(db, "u-порог", account.id, 2) == 0
    assert contribution_to_cell(db, "u-порог", account.id, 2) == 0, \
        "порог не вычитается из соседа"


def test_the_contribution_is_the_ladder_plus_the_shortfall(db):
    """Слагаемое — это ровно «лестница» плюс «сколько уехало мимо учёта».

    Второй копии формулы «сколько» в проекте нет: вклад спрашивает ту же
    `quantity_for_account`, которой считает рассылка. Разойдись они, одиночный
    путь считал бы по одной формуле, ячейка по другой, и разницу увидели бы на
    остатках.
    """
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-плюс", "2000000000011", 40, queued=False)
    _row(db, account, "u-минус", "2000000000022", -7, queued=False)
    db.commit()

    for uid, base in (("u-плюс", 40), ("u-минус", -7)):
        assert (contribution_to_cell(db, uid, account.id, base)
                == quantity_for_account(db, uid, account.id, base) + min(0, base)), uid


def test_the_cell_total_is_not_recomputed_by_hand(db):
    """Итог ячейки считает `cell_quantity`, и он же стоит в рассылке.

    Повтори вызывающий `sum(...)` у себя — страница показывала бы не то число,
    которое уходит наружу.
    """
    account = _случай_2503(db)
    uids = members(db, account.id, CELL, "u-2503")
    parts = shares(db, account.id, uids)

    assert sorted(parts) == [("u-2403", -7), ("u-2503", 40)]
    assert cell_quantity(parts) == 33
    assert cell_quantity([("x", -1), ("y", -2)]) == 0


def test_the_products_page_shows_the_number_that_goes_out(logged_in_client, web_db):
    """Строка обязана называть то же число, что уедет на площадку.

    Покажи она сумму без минуса, человек сверил бы её с витриной, увидел 33 на
    площадке и 40 у себя — и решил бы, что отправка не прошла.
    """
    account = make_account(web_db, Platform.wb, name="ИП КАРАМАН",
                           warehouse_id="wh-1")
    _row(web_db, account, "u-2503", "2000000000011", 40, queued=False)
    _row(web_db, account, "u-2403", "2000000000022", -7, enabled=False,
         queued=False)
    web_db.commit()

    body = logged_in_client.get("/products").text
    assert "на карточке ещё" in body, body[:400]
    assert "уходит 33" in body, "строка показывает итог ячейки"


def test_the_page_shows_zero_where_the_sum_went_negative(logged_in_client, web_db):
    """И обрезку страница показывает ту же, что рассылка.

    Повтори она `sum(...)` у себя — в строке стояло бы «уходит -5», то есть
    число, которого на площадке не бывает, а наружу уехал бы ноль.
    """
    account = make_account(web_db, Platform.wb, name="ИП КАРАМАН",
                           warehouse_id="wh-1")
    _row(web_db, account, "u-2503", "2000000000011", 2, queued=False)
    _row(web_db, account, "u-2403", "2000000000022", -7, enabled=False,
         queued=False)
    web_db.commit()

    body = logged_in_client.get("/products").text
    assert "уходит 0" in body, "итог ячейки обрезан до нуля"
    assert "уходит -5" not in body


def test_the_minus_is_not_invented_where_there_is_no_cell(db):
    """Соседа нет — минус остаётся минусом одной строки и наружу уходит нулём.

    Без этого вклад минуса начал бы вычитаться из чего-нибудь ещё.
    """
    account = make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")
    _row(db, account, "u-2503", "2000000000011", 40)
    _row(db, account, "u-другая", "2000000000022", -7, cell="177:9999",
         enabled=False, queued=False)
    db.commit()

    client = _Wb()
    run_dispatch_cycle(db, {account.id: client}, [account])

    assert client.sent == [[("2000000000011", 40)]]

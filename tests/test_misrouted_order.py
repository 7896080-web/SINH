"""Заказ списывается с того, у кого остаток ЕСТЬ, а не уходит в минус у соседа.

29.10 на бою: `2503 SIYAH` и `2403 SIYAH` ведут на одну карточку WB. Остаток
лежит на 2503, он отмечен и транслируется; баркод карточки привязан к 2403, у
него ноль и галочки нет. Каждый заказ списывался с 2403 — тот уходил в МИНУС, —
а у 2503 не убавлялось вовсе, и рассылка отправляла его полное число снова и
снова: оверселл, который сам не кончится никогда.

Физически же со склада ЦС уехала единица 2503. Значит списывать надо с 2503, и
это не «подогнать цифру», а назвать правильного плательщика.

Карточку при этом делят ДВЕ ПАРТИИ ОДНОЙ МОДЕЛИ, и сопоставление верное:
партии идут с пересекающимися размерами (4033 M–3XL и 4052 3XL–6XL делят 3XL;
2403 и пришедшая за ней 2503 — один и тот же товар), баркоды привязаны по
цветоразмеру и пулу. Значит подмена плательщика — не обход поломки мэппинга, а
штатный разбор, и аномалии по ней НЕТ: см.
`test_the_swap_says_what_it_did_and_leaves_no_anomaly`.

Проверки тут про СЛЕДСТВИЕ: у кого убавилось, у кого нет и что уехало в 1С.
Обратные проверки важнее прямых: обычный путь (товар покрывает свой заказ)
обязан остаться байт в байт, иначе подмена начнёт перекладывать продажи между
здоровыми строками.
"""
from datetime import date

from app.models import (AnomalyReason, Barcode, FtpTask, PlatformCatalogItem,
                        ProcessedOrder, Product, SyncAnomaly, SyncSetting)
from app.workers.order_poller import process_new_order
from app.workers.platform_clients.base import PlatformOrder
from tests.factories import make_account

CELL = "111:222"


def _item(db, account, uid, barcode, stock, *, enabled=True, broadcast=True,
          in_cell=True):
    db.add(Product(uid_1c=uid, article=uid, name="Свитшот", size="M",
                   color="SIYAH", stock_on_hand=stock,
                   broadcast_enabled=broadcast,
                   recalc_account_ids=str(account.id)))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=enabled))
    if in_cell:
        db.add(PlatformCatalogItem(account_id=account.id, external_id=CELL,
                                   barcode=barcode, article="WB", name="Карточка"))
    db.commit()


def _order(barcode="BC-2403", qty=1):
    return PlatformOrder(order_id="O-1", barcode=barcode, quantity=qty,
                         raw_status="new", order_date=date.today())


def _take(db, account, order, **kw):
    """Провести заказ. По умолчанию — путём РАСЧЁТА (`respect_enabled=False`).

    Так и надо для проверок про арифметику списания: живой опрос спрашивает
    галочку, и там, где подмена НЕ срабатывает, он заказ по неотмеченной паре
    отсеет гейтом — списания не будет вовсе, и проверка мерила бы отказ гейта
    вместо того, что собиралась. Про сам гейт есть свой тест, и он передаёт
    `respect_enabled=True` явно.
    """
    kw.setdefault("is_test", False)
    kw.setdefault("order_date", date.today())
    kw.setdefault("respect_enabled", False)
    return process_new_order(db, order, account, "WB.Ожидает", **kw)


def _stock(db, uid):
    return db.query(Product).filter(Product.uid_1c == uid).first().stock_on_hand


def test_the_order_is_taken_from_whoever_has_the_stock(db):
    """Главное: списывается 2503, а 2403 не уходит в минус."""
    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 0, enabled=False)
    _item(db, account, "2503", "BC-2503", 40)

    result = _take(db, account, _order())

    assert result["status"] == "processed", result
    assert _stock(db, "2503") == 39, "списалось не с того, у кого остаток"
    assert _stock(db, "2403") == 0, "товар баркода ушёл в минус — то, что чинили"
    # И заказ записан на ПЛАТЕЛЬЩИКА: иначе отмена вернула бы остаток не туда.
    assert db.query(ProcessedOrder).one().uid_1c == "2503"


def test_the_1c_document_names_the_payer(db):
    """В 1С уезжает баркод ПЛАТЕЛЬЩИКА — документ обязан быть верным.

    Перемещение ЦС -> склад площадки 1С разбирает по баркоду. Уйди туда баркод
    2403, 1С двигала бы товар, которого на ЦС нет, — ровно тот минус, только
    уже в чужом учёте, где мы его не поправим.
    """
    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 0, enabled=False)
    _item(db, account, "2503", "BC-2503", 40)

    _take(db, account, _order())

    task = db.query(FtpTask).filter(FtpTask.command == "CREATE_MOVEMENT").one()
    assert task.barcode == "BC-2503", task.barcode


def test_the_swap_says_what_it_did_and_leaves_no_anomaly(db):
    """Подмена — ШТАТНЫЙ случай: аномалии нет, а след есть.

    Один день (pm173) она заводила аномалию с советом «поправьте
    сопоставление», и совет был невыполним: два артикула 1С на одной карточке
    — это партии одной модели с пересекающимися размерами (4033 M–3XL и 4052
    3XL–6XL делят 3XL; 2403 и 2503 — один товар), баркоды сопоставлены ВЕРНО
    по цветоразмеру и пулу. Править нечего, а строка на каждую такую продажу —
    поток на штатной работе, после которого страницу аномалий не открывают.

    Совсем молчать тоже нельзя: вопрос «почему убавился остаток ДРУГОГО
    артикула» задают, и ответ обязан быть в журнале приёма заказа.
    """
    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 0, enabled=False)
    _item(db, account, "2503", "BC-2503", 40)

    result = _take(db, account, _order())

    assert db.query(SyncAnomaly).count() == 0, "аномалии на штатном случае нет"
    assert result["anomaly_created"] is False
    assert "2403" in result["detail"] and "2503" in result["detail"]
    assert "другая партия" in result["detail"], result["detail"]
    assert "сопоставлен" not in result["detail"], \
        "совет править мэппинг невыполним: править нечего"


def test_an_item_that_can_cover_pays_itself(db):
    """Обратная проверка, и она тут важнее прямой.

    Обычный путь — весь каталог: товар покрывает свой заказ, и подмена не
    должна случаться вовсе. Сработай она здесь, продажи начали бы
    перекладываться между двумя здоровыми строками одной карточки, а это уже не
    починка, а новая беда — и молчаливая.
    """
    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 5)
    _item(db, account, "2503", "BC-2503", 40)

    _take(db, account, _order())

    assert _stock(db, "2403") == 4
    assert _stock(db, "2503") == 40, "подмена сработала там, где не надо"
    assert db.query(SyncAnomaly).count() == 0


def test_nobody_able_means_the_old_behaviour(db):
    """Покрыть некому — уходим в минус, как и раньше.

    Выдумать плательщика там, где его нет, значило бы списать с товара,
    которого тоже нет. Минус тогда честно значит «отгрузили больше, чем
    числилось», и у него есть свой читатель в отчёте.
    """
    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 0, enabled=False)
    _item(db, account, "2503", "BC-2503", 0)

    _take(db, account, _order())

    assert _stock(db, "2403") == -1
    assert db.query(SyncAnomaly).filter(
        SyncAnomaly.reason == AnomalyReason.order_misrouted).count() == 0


def test_a_partial_cover_is_not_a_cover(db):
    """Сосед с остатком МЕНЬШЕ заказа плательщиком не становится.

    Делить один заказ на двоих значило бы два документа в 1С по одной продаже,
    а у 1С идемпотентность обещана по НОМЕРУ ЗАКАЗА — второй документ она
    создала бы как повтор. Поэтому правило одно: покрывает целиком или нет.
    """
    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 0, enabled=False)
    _item(db, account, "2503", "BC-2503", 2)

    _take(db, account, _order(qty=3))

    assert _stock(db, "2403") == -3
    assert _stock(db, "2503") == 2


def test_the_choice_is_reproducible(db):
    """Двое могут покрыть — платит тот, у кого остаток больше.

    Произвольный выбор означал бы, что один и тот же заказ списывается то с
    одного, то с другого: разобрать такое потом нечем, а сверка с 1С каждый час
    двигала бы оба числа.
    """
    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 0, enabled=False)
    _item(db, account, "2503", "BC-2503", 10)
    _item(db, account, "2603", "BC-2603", 40)

    _take(db, account, _order())

    assert _stock(db, "2603") == 39, "платит не тот, у кого остаток больше"
    assert _stock(db, "2503") == 10


def test_the_live_poller_processes_it_now(db):
    """И то, ради чего всё: ЖИВОЙ опрос такой заказ больше не пропускает.

    Он спрашивает галочку (`respect_enabled=True`), а у 2403 её нет — заказ
    отсеивался гейтом и не списывался нигде, то есть оверселл накапливался
    молча. Подмена стоит ДО гейта, и гейт спрашивает про пару ПЛАТЕЛЬЩИКА: у
    2503 галочка есть, значит продажа проводится.
    """
    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 0, enabled=False)
    _item(db, account, "2503", "BC-2503", 40)

    result = _take(db, account, _order(), respect_enabled=True)

    assert result["status"] == "processed", result
    assert _stock(db, "2503") == 39


def test_a_neighbour_on_another_card_is_not_a_payer(db):
    """Обратная проверка к выбору соседа: ячейка, а не вся номенклатура.

    Товар с остатком, который НЕ делит карточку, к этой продаже отношения не
    имеет. Спроси подмена просто «у кого есть остаток», она списывала бы с
    любого — и это было бы не починкой, а порчей учёта по всему каталогу.
    """
    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 0, enabled=False)
    _item(db, account, "ЧУЖОЙ", "BC-OTHER", 40, in_cell=False)

    _take(db, account, _order())

    assert _stock(db, "2403") == -1
    assert _stock(db, "ЧУЖОЙ") == 40, "списалось с товара с ДРУГОЙ карточки"


def test_a_simulation_never_swaps(db):
    """Симуляция боевых полей не трогает — значит и плательщика не меняет.

    Страница «Тестирование» прогоняет товар, который ей назвали. Подмени она
    плательщика, оператор видел бы отчёт про один товар, а числа считались бы
    по другому, — и это при полном молчании: боевых записей симуляция не
    создаёт, искать расхождение было бы негде.
    """
    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 0, enabled=False)
    _item(db, account, "2503", "BC-2503", 40)

    result = _take(db, account, PlatformOrder(
        order_id="TEST-1", barcode="BC-2403", quantity=1,
        raw_status="new", order_date=date.today()), is_test=True)

    assert result["uid_1c"] == "2403", result
    assert result["misrouted_from"] is None
    assert _stock(db, "2503") == 40 and _stock(db, "2403") == 0


def test_the_report_stays_silent_about_the_swap(db):
    """Отчёт про подмену молчит — и это РЕШЕНИЕ, а не упущение.

    Проверяется по следствию: собираем отчёт целиком на том самом состоянии,
    которое 29.10 разбирали руками, и требуем, чтобы ни одна находка про него
    не говорила. Верни кто-нибудь находку «баркод ведёт не на тот товар» — на
    штатной расстановке двух партий она закричит, и тест упадёт.
    """
    from app.report import collect_findings

    account = make_account(db)
    _item(db, account, "2403", "BC-2403", 0, enabled=False)
    _item(db, account, "2503", "BC-2503", 40)
    result = _take(db, account, _order())

    assert result["misrouted_from"] == "2403", "подмена обязана была сработать"
    keys = {f.key for f in collect_findings(db)}
    assert "misrouted_orders" not in keys, keys
    assert "unmanaged_fed_pairs" not in keys, keys

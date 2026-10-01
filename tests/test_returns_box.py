"""Коробка возвратов: скан заводит вещь, одна кнопка проводит полный цикл.

Что здесь проверяется в первую очередь. Страница делает ПОЛНЫЙ ЦИКЛ одним
проходом — приёмка, перемещение «склад площадки → ЦС» и решение, — и раньше это
было не так: пачка собиралась из вещей, уже принятых на другой странице, то есть
коробку приходилось проводить дважды, теми же сканами. Для коробки, которую
разбирают в тот же час, второй проход не добавлял ни одного решения.

Отсюда и главный предмет тестов — СЧЁТ и ПЛОЩАДКА. Счёт: отсканировал три раза —
завелись три РАЗНЫЕ вещи, а не одна трижды; иначе в 1С уехал бы один документ
вместо трёх, а две единицы остались бы числиться на складе площадки навсегда.
Площадка: она ставится по выбранному складу, и от неё зависит, ОТКУДА 1С вернёт
вещь, — разойдись они, документ вернул бы товар не с того склада, и учёт
разъехался бы молча.
"""
import pytest

from app import returns as R
from app.models import (Barcode, FtpTask, Platform, Product, ReturnBatchEntry,
                        ReturnItem, ReturnStatus, ScrapReason)


@pytest.fixture()
def goods(web_db):
    web_db.add(Product(uid_1c="uid-1", name="Свитшот", article="СВ-01", size="62",
                       stock_on_hand=5))
    web_db.add(Barcode(barcode="2000000000017", uid_1c="uid-1"))
    web_db.add(Product(uid_1c="uid-2", name="Джемпер", article="ДЖ-02", size="50",
                       stock_on_hand=3))
    web_db.add(Barcode(barcode="2000000000024", uid_1c="uid-2"))
    web_db.commit()


WB_WH = "Wildberries_Склад_FBO"
OZON_WH = "OZON_Склад"


def _mode(client, mode="sale", reason="", warehouse=WB_WH):
    client.post("/returns/box/mode",
                data={"mode": mode, "reason": reason, "warehouse": warehouse})


def _send(client, mode="sale", reason="", warehouse=WB_WH):
    return client.post("/returns/box/send",
                       data={"mode": mode, "reason": reason,
                             "warehouse": warehouse},
                       follow_redirects=True)


def _scan(client, code):
    return client.post("/returns/box/scan", data={"code": code},
                       follow_redirects=True)


def _items(web_db):
    return web_db.query(ReturnItem).order_by(ReturnItem.id).all()


# ------------------------------------------------------------- полный цикл

def test_a_scan_accepts_the_thing_itself(logged_in_client, web_db, goods):
    """Скан и есть приёмка: через другую страницу коробку проводить не нужно.

    Это и есть полный цикл. Пока вещь заводили отдельно, коробку прогоняли
    сканером дважды, и второй проход не добавлял ни одного решения.
    """
    _mode(logged_in_client, warehouse=WB_WH)

    _scan(logged_in_client, "2000000000017")

    item = web_db.query(ReturnItem).one()
    assert item.barcode == "2000000000017"
    assert item.status is ReturnStatus.accepted
    assert item.uid_1c == "uid-1", "товар 1С опознан сразу"
    assert web_db.query(ReturnBatchEntry).one().return_id == item.id


def test_the_platform_comes_from_the_chosen_warehouse(logged_in_client, web_db, goods):
    """Площадку не спрашиваем вторым полем: она следует из склада.

    От неё зависит, откуда 1С заберёт единицу. Разойдись два поля — документ
    вернул бы товар не с того склада, и на чужом стало бы на единицу меньше,
    чем лежит.
    """
    _mode(logged_in_client, warehouse=OZON_WH)

    _scan(logged_in_client, "2000000000017")

    assert web_db.query(ReturnItem).one().platform is Platform.ozon


def test_three_scans_of_one_barcode_make_three_things(logged_in_client, web_db, goods):
    """Счёт обязан сойтись: три одинаковых свитшота — три РАЗНЫЕ вещи.

    Свернись они в одну запись, в 1С уехал бы один документ вместо трёх, а две
    единицы остались бы числиться на складе площадки навсегда — и узнать об этом
    было бы неоткуда.

    Сканы идут подряд, за доли секунды: ровно так их и прогоняют из коробки.
    Защита от дребезга сканера, отбрасывающая повтор по времени, отняла бы здесь
    настоящую вещь МОЛЧА, поэтому её тут нет — см. `scan_into_box`.
    """
    _mode(logged_in_client, warehouse=WB_WH)

    for _ in range(3):
        _scan(logged_in_client, "2000000000017")

    assert web_db.query(ReturnItem).count() == 3
    assert web_db.query(ReturnBatchEntry).count() == 3
    assert len({i.id for i in _items(web_db)}) == 3


def test_a_scan_without_a_warehouse_creates_nothing(logged_in_client, web_db, goods):
    """Склад спрашивается ДО сканов, а не на передаче.

    Спроси мы его в конце, сорок вещей уже лежали бы заведёнными не с той
    площадкой, и «выберите склад» пришлось бы читать как «перезаведите коробку».
    """
    _mode(logged_in_client, warehouse="")

    page = _scan(logged_in_client, "2000000000017").text

    assert "выберите склад" in page.lower(), page[:400]
    assert web_db.query(ReturnItem).count() == 0
    assert web_db.query(ReturnBatchEntry).count() == 0


def test_an_unknown_barcode_is_accepted_and_counted_apart(logged_in_client, web_db, goods):
    """Неопознанный баркод приёмке НЕ мешает — правило общее на весь раздел.

    Вещь физически есть независимо от нашего мэппинга, и отказать в скане значит
    заставить человека отложить её в сторону и забыть. Но в 1С она не уедет, и
    сказать об этом надо ЗАРАНЕЕ: иначе человек нажмёт «Передать» и получит
    список отказов там, где ждал готового дела.
    """
    _mode(logged_in_client, warehouse=WB_WH)

    _scan(logged_in_client, "2000000000099")
    page = logged_in_client.get("/returns/box").text

    item = web_db.query(ReturnItem).one()
    assert item.uid_1c is None
    assert "БЕЗ ТОВАРА 1С" in page


def test_scanning_sends_nothing_to_1c(logged_in_client, web_db, goods):
    """До «Передать в 1С» не случилось ничего необратимого.

    Обратный порядок (скан сразу отправляет) выглядит быстрее и платит хуже:
    ошибившись причиной на сороковой вещи, человек отменял бы сорок отправок по
    одной.
    """
    _mode(logged_in_client, warehouse=WB_WH)

    _scan(logged_in_client, "2000000000017")

    assert web_db.query(ReturnItem).one().status is ReturnStatus.accepted
    assert web_db.query(FtpTask).count() == 0


# ------------------------------------------------------------ наклейка RET

def test_a_label_scan_takes_the_thing_already_in_the_shop(
        logged_in_client, web_db, goods):
    """Пачка из химчистки: записи по этим вещам уже есть.

    Товарный баркод завёл бы для них ВТОРЫЕ записи, и половина коробки осталась
    бы лежать в разборе навсегда. Наклейка опознаёт именно вещь — для того и
    печатается на приёмке.
    """
    item = R.accept(web_db, "2000000000017", Platform.wb)
    item.status = ReturnStatus.cleaning
    web_db.commit()
    _mode(logged_in_client, warehouse=WB_WH)

    _scan(logged_in_client, R.label_number(item))

    assert web_db.query(ReturnItem).count() == 1, "второй записи не завелось"
    assert web_db.query(ReturnBatchEntry).one().return_id == item.id


def test_a_label_of_an_already_decided_thing_is_refused(logged_in_client, web_db, goods):
    """Вещь, по которой задание уже ушло, второго решения не примет.

    Спрашиваем ту же таблицу переходов, что и одиночная кнопка: разойдись они,
    пачка отправляла бы то, чего страница вещи не даёт.
    """
    item = R.accept(web_db, "2000000000017", Platform.wb)
    item.status = ReturnStatus.awaiting_1c
    web_db.commit()
    _mode(logged_in_client, warehouse=WB_WH)

    page = _scan(logged_in_client, R.label_number(item)).text

    assert "решение уже принято" in page
    assert web_db.query(ReturnBatchEntry).count() == 0


def test_the_same_label_twice_says_so(logged_in_client, web_db, goods):
    """«Уже в пачке» лечится взглядом на список — значит так и надо сказать."""
    item = R.accept(web_db, "2000000000017", Platform.wb)
    web_db.commit()
    _mode(logged_in_client, warehouse=WB_WH)
    _scan(logged_in_client, R.label_number(item))

    page = _scan(logged_in_client, R.label_number(item)).text

    assert "уже в пачке" in page
    assert web_db.query(ReturnBatchEntry).count() == 1


# --------------------------------------------------------- передача в 1С

def test_sending_the_batch_goes_through_the_same_path_as_one_item(
        logged_in_client, web_db, goods):
    """Массовый путь обязан делать то же, что построчный."""
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000017")
    _scan(logged_in_client, "2000000000024")

    page = _send(logged_in_client).text

    assert "Передано в 1С: 2" in page, page[:400]
    assert {i.status for i in _items(web_db)} == {ReturnStatus.awaiting_1c}
    assert {t.command for t in web_db.query(FtpTask).all()} == {R.RETURN_COMMAND}
    assert web_db.query(ReturnBatchEntry).count() == 0, "переданное уходит из пачки"


def test_the_task_moves_the_item_from_that_warehouse_to_the_central_one(
        logged_in_client, web_db, goods):
    """Полный цикл возврата: со склада площадки на ЦС, и уже там — в оборот.

    Проверяем СЛЕДСТВИЕ — что уехало в задании, — а не то, что страница
    показала: имя склада в документе и есть вся суть выбора.
    """
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000017")

    _send(logged_in_client, warehouse=WB_WH)

    task = web_db.query(FtpTask).one()
    assert task.command == R.RETURN_COMMAND
    assert task.warehouse_from == WB_WH
    assert task.warehouse_to == R.TARGET_WAREHOUSE


def test_a_scrap_batch_moves_from_the_same_warehouse(logged_in_client, web_db, goods):
    """У утилизации цикл длиннее на документ, но склад тот же: 1С сначала вернёт
    вещь со склада площадки на ЦС и только потом спишет её оттуда — иначе
    единица висела бы на складе площадки вечно."""
    _mode(logged_in_client, "scrap", ScrapReason.defect.value, WB_WH)
    _scan(logged_in_client, "2000000000017")

    _send(logged_in_client, "scrap", ScrapReason.defect.value, WB_WH)

    task = web_db.query(FtpTask).one()
    assert task.command == R.SCRAP_COMMAND
    assert task.warehouse_from == WB_WH and task.warehouse_to == R.TARGET_WAREHOUSE


def test_a_scrap_batch_carries_the_one_common_reason(logged_in_client, web_db, goods):
    """Причина одна на пачку — так и просил склад. И она обязана доехать до
    КАЖДОЙ вещи: по ней 1С выбирает хоз. операцию списания."""
    _mode(logged_in_client, "scrap", ScrapReason.swapped.value, WB_WH)
    _scan(logged_in_client, "2000000000017")
    _scan(logged_in_client, "2000000000024")

    _send(logged_in_client, "scrap", ScrapReason.swapped.value)

    assert {i.scrap_reason for i in _items(web_db)} == {ScrapReason.swapped}
    assert {i.status for i in _items(web_db)} == {ReturnStatus.awaiting_scrap}
    assert {t.command for t in web_db.query(FtpTask).all()} == {R.SCRAP_COMMAND}


def test_scrap_without_a_reason_is_refused_before_anything_moves(
        logged_in_client, web_db, goods):
    """Пустая причина — отказ ВСЕЙ пачке, а не «спишем как-нибудь».

    Документ без хоз. операции 1С провела бы, а движение ушло бы не туда, и
    увидели бы это в отчётах через месяц.
    """
    _mode(logged_in_client, "scrap", "", WB_WH)
    _scan(logged_in_client, "2000000000017")

    page = _send(logged_in_client, "scrap", "").text

    assert "обязательна причина" in page
    assert web_db.query(ReturnItem).one().status is ReturnStatus.accepted
    assert web_db.query(FtpTask).count() == 0


def test_one_refusal_does_not_carry_away_the_rest(logged_in_client, web_db, goods):
    """Вещь без товара 1С отправить нельзя — но остальные уезжают.

    И она остаётся в пачке со своей причиной: получив «передано 1» при двух в
    коробке, человек обязан видеть, какая осталась.

    Отказывающая идёт в пачке ПЕРВОЙ намеренно. Поставь её второй — и цикл,
    обрывающийся на первом отказе, прошёл бы тест: здоровая вещь успела бы
    уехать до обрыва, а уносит он именно тех, кто стоит ПОСЛЕ.
    """
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000099")      # баркода нет в мэппинге
    _scan(logged_in_client, "2000000000017")
    orphan, good = _items(web_db)

    page = _send(logged_in_client).text

    assert "Передано в 1С: 1" in page
    assert "Осталось в пачке 1" in page
    web_db.refresh(good), web_db.refresh(orphan)
    assert good.status is ReturnStatus.awaiting_1c
    assert orphan.status is ReturnStatus.accepted
    assert web_db.query(ReturnBatchEntry).one().return_id == orphan.id


def test_sending_without_a_warehouse_is_refused(logged_in_client, web_db, goods):
    """Без склада 1С не знает, ОТКУДА забрать вещь.

    Скан его уже требует, но форма передачи несёт склад отдельным полем, и
    вторая дверь тут не лишняя: пачка одна на установку, а поле человек мог
    поменять в соседнем окне.
    """
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000017")

    page = _send(logged_in_client, warehouse="").text

    assert "Выберите склад" in page
    assert web_db.query(ReturnItem).one().status is ReturnStatus.accepted
    assert web_db.query(FtpTask).count() == 0


def test_a_wrong_warehouse_refuses_the_whole_batch_by_name(
        logged_in_client, web_db, goods):
    """Коробка приехала с одного ПВЗ, и склад у неё один.

    Случай живой: человек прогнал часть коробки, переключил склад и продолжил.
    Вещь другой площадки в пачке — это не «две лишние строки», а признак, что
    коробку собрали не с того склада: остальные тридцать восемь, возможно,
    уехали бы неверно. Поэтому отказ ВСЕЙ передаче, до единого задания, и строки
    названы поимённо — «нельзя» человеку не помогает.
    """
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000017")
    _mode(logged_in_client, "sale", warehouse=OZON_WH)
    _scan(logged_in_client, "2000000000024")
    ours, alien = _items(web_db)

    page = _send(logged_in_client, warehouse=WB_WH).text

    assert "Не передано НИЧЕГО" in page, page[:400]
    assert R.label_number(alien) in page
    web_db.refresh(ours), web_db.refresh(alien)
    assert ours.status is ReturnStatus.accepted, "своя вещь тоже осталась на месте"
    assert web_db.query(FtpTask).count() == 0
    assert web_db.query(ReturnBatchEntry).count() == 2, "пачка цела"


# ------------------------------------------------------------------ экран

def test_the_page_shows_totals_split_by_platform(logged_in_client, web_db, goods):
    """Разбивка по площадкам не украшение: от площадки зависит склад-источник
    перемещения, а две площадки в одной коробке — сигнал, что попало лишнее."""
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000017")
    _mode(logged_in_client, "sale", warehouse=OZON_WH)
    _scan(logged_in_client, "2000000000024")

    page = logged_in_client.get("/returns/box").text

    assert "WILDBERRIES" in page.upper() and "OZON" in page.upper()
    assert "Передать в 1С — 2 шт" in page


def test_the_page_marks_the_rows_that_do_not_fit_the_warehouse(
        logged_in_client, web_db, goods):
    """Отказ на передаче — поздно, если до него человек не видел проблемы."""
    _mode(logged_in_client, "sale", warehouse=OZON_WH)
    _scan(logged_in_client, "2000000000024")
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000017")

    page = logged_in_client.get("/returns/box").text

    assert "не с этого склада" in page
    assert "НЕ С ЭТОГО СКЛАДА" in page


def test_the_send_button_asks_with_the_number(logged_in_client, web_db, goods):
    """Пачка переживает перезагрузку и смену человека: «передать всё» без числа
    не говорит ни о чём."""
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000017")

    page = logged_in_client.get("/returns/box").text

    assert "Передать в 1С: 1 шт" in page


def test_an_unknown_warehouse_name_is_not_remembered(logged_in_client, web_db):
    """Имя склада уезжает в 1С СТРОКОЙ, и чужое там означало бы документ не с
    того склада. Незнакомое значит «не выбран», а не «запишем как есть»."""
    _mode(logged_in_client, "sale", warehouse="Склад-которого-нет")

    page = logged_in_client.get("/returns/box").text

    assert "склад не выбран" in page
    assert "Склад-которого-нет" not in page


def test_the_page_is_its_own_menu_item(logged_in_client, web_db):
    """Страница отдельная — значит и пункт меню свой.

    Страница, к которой ведёт один элемент на другой странице, для человека
    отсутствует; а подсветка чужого пункта читается как «перешёл не туда».
    """
    import re

    page = logged_in_client.get("/returns/box").text
    nav = page.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]

    assert '/returns/box' in re.findall(r'<a class="nav__item[^>]*href="([^"]+)"', nav)
    assert re.findall(r'<a class="nav__item active"[^>]*href="([^"]+)"',
                      nav) == ["/returns/box"]


def test_the_training_banner_hangs_here_too(logged_in_client, web_db, goods):
    """Полоса режима обязана висеть на КАЖДОЙ странице, где заводят вещи.

    Эта заводит: скан и есть приёмка. Увидь человек полосу один раз на входе в
    раздел, через час он про неё не вспомнит, а неверное представление о том, в
    каком ты режиме, и есть вся опасность затеи.
    """
    R.set_test_mode(web_db, True)
    web_db.commit()

    page = logged_in_client.get("/returns/box").text

    assert "ТРЕНИРОВОЧНЫЙ РЕЖИМ" in page


def test_the_warehouse_account_can_open_the_page(logged_in_client, web_db):
    """Склад — единственный, кому эта страница и нужна."""
    assert logged_in_client.get("/returns/box").status_code == 200


# ------------------------------------------------------------ тренировка

def test_a_scan_in_test_mode_marks_the_thing_itself(logged_in_client, web_db, goods):
    """`is_test` живёт НА ВЕЩИ, а не на моменте отправки.

    Считай `send_to_1c` режим на момент передачи, настоящий возврат, принятый
    при включённой тренировке, молча стал бы тренировочным: в 1С он не поехал бы
    никогда, а при выходе из режима его бы ещё и стёрли.
    """
    R.set_test_mode(web_db, True)
    web_db.commit()
    _mode(logged_in_client, "sale", warehouse=WB_WH)

    _scan(logged_in_client, "2000000000017")
    page = logged_in_client.get("/returns/box").text

    assert web_db.query(ReturnItem).one().is_test is True
    assert "ТРЕНИРОВКА" in page


def test_clearing_test_data_takes_items_out_of_the_batch(logged_in_client, web_db, goods):
    """Выход из тренировки стирает вещи — строка пачки обязана уйти с ними.

    Массовый `DELETE` не поднимает ни каскад ORM, ни `ON DELETE CASCADE`: на
    SQLite внешние ключи по умолчанию не проверяются вовсе. Осиротевшая строка
    показала бы в пачке вещь, которой уже нет.
    """
    R.set_test_mode(web_db, True)
    web_db.commit()
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000017")

    R.clear_test_data(web_db)
    web_db.commit()

    assert web_db.query(ReturnBatchEntry).count() == 0


# ----------------------------------------------------------------- пачка

def test_clearing_the_batch_leaves_the_things_accepted(logged_in_client, web_db, goods):
    """Очистка снимает СТРОКИ, а не вещи.

    Удалить их было бы соблазнительно — скан их и завёл, — но приёмка это
    утверждение о физическом мире: вещь лежит на складе, и стереть запись значит
    соврать. Ошиблись сканом — «Отменить приёмку» на странице вещи.
    """
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000017")

    logged_in_client.post("/returns/box/clear", follow_redirects=True)

    assert web_db.query(ReturnItem).one().status is ReturnStatus.accepted
    assert web_db.query(ReturnBatchEntry).count() == 0


def test_cancelling_acceptance_takes_the_item_out_of_the_batch(
        logged_in_client, web_db, goods):
    """Осиротевшая строка пачки показала бы вещь, которой уже нет, и «Передать»
    спотыкалась бы о неё каждый раз. На SQLite каскад по умолчанию не работает."""
    _mode(logged_in_client, "sale", warehouse=WB_WH)
    _scan(logged_in_client, "2000000000017")
    item = web_db.query(ReturnItem).one()

    logged_in_client.post(f"/returns/{item.id}/cancel", follow_redirects=True)

    assert web_db.query(ReturnBatchEntry).count() == 0


def test_the_warehouse_list_is_the_one_used_when_the_order_shipped(web_db):
    """Своего списка складов у страницы нет и быть не должно.

    Вернуть вещь 1С может только оттуда, куда её увезло перемещение при
    продаже. Заведи мы второй список — он однажды разошёлся бы с первым, и
    документ возврата перестал бы проводиться, а узнали бы мы об этом на живой
    коробке.
    """
    from app.workers.scheduler import PENDING_WAREHOUSE_NAME

    assert R.warehouse_choices() == PENDING_WAREHOUSE_NAME
    assert R.platform_of_warehouse(WB_WH) is Platform.wb
    assert R.platform_of_warehouse("чужое") is None


# ------------------------------------------- выбор склада, режима и причины

def test_choosing_a_warehouse_alone_does_not_switch_the_mode(logged_in_client, web_db):
    """Три списка — три формы, и каждая правит ТОЛЬКО своё поле.

    Пока форма была одна, выпадающий список сам по себе на сервер ничего не
    отправлял: человек выбирал склад, поле скана оставалось погашенным, и
    применить выбор можно было лишь нажав соседнюю кнопку — а звались соседние
    кнопки действиями. Теперь список применяется сразу, и форма склада поля
    `mode` не несёт вовсе. Считай обработчик отсутствующее поле пустым (а
    умолчание `Form("sale")` именно так и делало), выбор склада МОЛЧА
    переключал бы утилизацию на возврат в продажу — и коробка уехала бы в
    оборот вместо утиля.
    """
    _mode(logged_in_client, "scrap", ScrapReason.defect.value, OZON_WH)

    logged_in_client.post("/returns/box/mode", data={"warehouse": WB_WH})
    page = logged_in_client.get("/returns/box").text

    assert "УТИЛИЗАЦИЯ" in page, page[:400]
    assert WB_WH in page
    assert "ВОЗВРАТ В ПРОДАЖУ" not in page


def test_choosing_a_reason_alone_keeps_the_warehouse(logged_in_client, web_db):
    """Зеркальное: форма причины не несёт склада, и терять его ей нечем.

    Потеряйся он здесь, поле скана гасло бы посреди коробки — ровно в тот
    момент, когда человек уточнил причину.
    """
    _mode(logged_in_client, "scrap", "", WB_WH)

    logged_in_client.post("/returns/box/mode",
                          data={"reason": ScrapReason.swapped.value})
    page = logged_in_client.get("/returns/box").text

    assert WB_WH in page, page[:400]
    assert "склад не выбран" not in page


def test_an_empty_reason_still_clears_it(logged_in_client, web_db):
    """Пустая строка — ЗНАЧЕНИЕ, а не «поле не прислали».

    «— выберите причину —» обязана снимать причину: иначе ошибочно выбранную
    не отменить ничем, а передача уедет не с той статьёй затрат.
    """
    _mode(logged_in_client, "scrap", ScrapReason.defect.value, WB_WH)

    logged_in_client.post("/returns/box/mode", data={"reason": ""})
    page = logged_in_client.get("/returns/box").text

    assert "Причина не выбрана" in page, page[:400]


def test_each_list_applies_on_choice_without_a_button(logged_in_client, web_db):
    """Выбор в списке и есть ответ на вопрос — отдельная кнопка это лишний шаг.

    На нём всё и застревало: склад выбран на экране, а на сервере его нет, и
    поле скана погашено. Проверяем РАЗМЕТКУ, потому что предмет правки — то,
    что делает браузер, а не то, что считает обработчик.
    """
    page = logged_in_client.get("/returns/box").text

    for field in ("warehouse", "reason"):
        chunk = page.split(f'name="{field}"', 1)[1][:200]
        assert "this.form.submit()" in chunk, f"список {field} не применяется сам"


def test_the_mode_button_is_a_choice_and_sends_nothing(logged_in_client, web_db, goods):
    """«Вернуть в продажу» читается как команда, а переключает режим.

    Человек жмёт её и ждёт, что вещи уедут; поэтому страница говорит это
    словами, а тест стережёт само поведение: в 1С не уходит НИЧЕГО, и пачка
    остаётся целой.
    """
    _mode(logged_in_client, "scrap", ScrapReason.defect.value, WB_WH)
    _scan(logged_in_client, "2000000000017")

    logged_in_client.post("/returns/box/mode", data={"mode": "sale"})

    assert web_db.query(FtpTask).count() == 0
    assert web_db.query(ReturnBatchEntry).count() == 1
    assert web_db.query(ReturnItem).one().status is ReturnStatus.accepted


def test_the_reason_warning_is_silent_when_returning_to_sale(logged_in_client, web_db):
    """Требовать причину там, где она ни на что не влияет, — способ научить
    человека не читать предупреждения вовсе."""
    _mode(logged_in_client, "sale", "", WB_WH)

    page = logged_in_client.get("/returns/box").text

    assert "Причина не выбрана" not in page, page[:400]

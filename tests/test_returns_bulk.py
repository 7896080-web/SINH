"""Массовое решение по коробке возвратов: скан пачкой, одна передача в 1С.

Главное здесь — ИДЕНТИФИКАТОР. На странице утилизации файлом вещь опознаётся
номером `RET-…`, и это правило записано жёстко: баркод опознаёт SKU, а три
одинаковых свитшота дают три записи с одним баркодом — по нему не понять, какую
именно выбросили.

Здесь вопрос другой, и потому ответ другой. Человек держит вещь в руках и решает
про НЕЁ; какая из трёх записей ей соответствует, не знает никто и знать не
нужно — вещи физически неразличимы. Важно ровно одно: чтобы счёт сошёлся.
Отсканировал три раза — уехали три РАЗНЫЕ вещи, а не одна трижды. Всё
устройство `pick_for_batch` про это, и тесты ниже — тоже.
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


def _accept(web_db, barcode, platform=Platform.wb, status=ReturnStatus.accepted):
    item = R.accept(web_db, barcode, platform)
    item.status = status
    web_db.commit()
    return item


def _mode(client, mode, reason=""):
    client.post("/returns/bulk/mode", data={"mode": mode, "reason": reason})


def _scan(client, code):
    return client.post("/returns/bulk/scan", data={"code": code},
                       follow_redirects=True)


def test_three_scans_of_one_barcode_take_three_different_items(
        logged_in_client, web_db, goods):
    """Счёт обязан сойтись: три одинаковых свитшота — три РАЗНЫЕ вещи.

    Возьми скан ту же самую, в 1С уехал бы один номер трижды, а две вещи
    остались бы лежать в разборе навсегда — и никто бы об этом не узнал.
    """
    first = _accept(web_db, "2000000000017")
    second = _accept(web_db, "2000000000017")
    third = _accept(web_db, "2000000000017")

    for _ in range(3):
        _scan(logged_in_client, "2000000000017")

    picked = {e.return_id for e in web_db.query(ReturnBatchEntry).all()}
    assert picked == {first.id, second.id, third.id}


def test_a_fourth_scan_says_there_are_no_more(logged_in_client, web_db, goods):
    """Молчать нельзя: человек стоит с вещью в руках и ждёт ответа."""
    _accept(web_db, "2000000000017")
    _scan(logged_in_client, "2000000000017")

    page = _scan(logged_in_client, "2000000000017").text

    assert "уже в пачке" in page, page[:400]
    assert web_db.query(ReturnBatchEntry).count() == 1


def test_an_unknown_barcode_is_refused_by_name(logged_in_client, web_db, goods):
    """«Нельзя» не помогает. Отказ называет баркод и что с ним делать."""
    page = _scan(logged_in_client, "2000000000031").text

    assert "2000000000031" in page and "приёмка" in page


def test_the_oldest_item_goes_first(logged_in_client, web_db, goods):
    """Выбор детерминированный: дольше лежит — раньше разбирают.

    Возьми мы произвольную, два прогона по одной коробке разошлись бы, и
    разобраться потом было бы нечем.
    """
    old = _accept(web_db, "2000000000017")
    _accept(web_db, "2000000000017")

    _scan(logged_in_client, "2000000000017")

    assert web_db.query(ReturnBatchEntry).one().return_id == old.id


def test_an_item_already_decided_is_not_taken(logged_in_client, web_db, goods):
    """Вещь, по которой задание уже ушло, второго решения не примет.

    Спрашиваем ту же таблицу переходов, что и одиночная кнопка: разойдись они,
    пачка отправляла бы то, чего страница вещи не даёт.
    """
    _accept(web_db, "2000000000017", status=ReturnStatus.awaiting_1c)

    page = _scan(logged_in_client, "2000000000017").text

    assert "решение уже принято" in page
    assert web_db.query(ReturnBatchEntry).count() == 0


def test_scanning_does_not_touch_the_status(logged_in_client, web_db, goods):
    """До «Передать в 1С» не случилось НИЧЕГО — иначе передумать было бы нечем."""
    item = _accept(web_db, "2000000000017")

    _scan(logged_in_client, "2000000000017")

    web_db.refresh(item)
    assert item.status is ReturnStatus.accepted
    assert web_db.query(FtpTask).count() == 0


def test_sending_the_batch_goes_through_the_same_path_as_one_item(
        logged_in_client, web_db, goods):
    """Массовый путь обязан делать то же, что построчный."""
    a = _accept(web_db, "2000000000017")
    b = _accept(web_db, "2000000000024")
    _mode(logged_in_client, "sale")
    _scan(logged_in_client, "2000000000017")
    _scan(logged_in_client, "2000000000024")

    page = logged_in_client.post("/returns/bulk/send", data={"mode": "sale"},
                                 follow_redirects=True).text

    assert "Передано в 1С: 2" in page, page[:400]
    web_db.refresh(a), web_db.refresh(b)
    assert a.status is ReturnStatus.awaiting_1c and b.status is ReturnStatus.awaiting_1c
    tasks = web_db.query(FtpTask).all()
    assert {t.command for t in tasks} == {R.RETURN_COMMAND}
    assert web_db.query(ReturnBatchEntry).count() == 0, "переданное уходит из пачки"


def test_a_scrap_batch_carries_the_one_common_reason(logged_in_client, web_db, goods):
    """Причина одна на пачку — так и просил склад. И она обязана доехать до
    КАЖДОЙ вещи: по ней 1С выбирает хоз. операцию списания."""
    a = _accept(web_db, "2000000000017")
    b = _accept(web_db, "2000000000024")
    _mode(logged_in_client, "scrap", ScrapReason.swapped.value)
    _scan(logged_in_client, "2000000000017")
    _scan(logged_in_client, "2000000000024")

    logged_in_client.post("/returns/bulk/send",
                          data={"mode": "scrap", "reason": ScrapReason.swapped.value},
                          follow_redirects=True)

    web_db.refresh(a), web_db.refresh(b)
    assert a.scrap_reason is ScrapReason.swapped
    assert b.scrap_reason is ScrapReason.swapped
    assert a.status is ReturnStatus.awaiting_scrap
    assert {t.command for t in web_db.query(FtpTask).all()} == {R.SCRAP_COMMAND}


def test_scrap_without_a_reason_is_refused_before_anything_moves(
        logged_in_client, web_db, goods):
    """Пустая причина — отказ ВСЕЙ пачке, а не «спишем как-нибудь».

    Документ без хоз. операции 1С провела бы, а движение ушло бы не туда, и
    увидели бы это в отчётах через месяц.
    """
    item = _accept(web_db, "2000000000017")
    _mode(logged_in_client, "scrap")
    _scan(logged_in_client, "2000000000017")

    page = logged_in_client.post("/returns/bulk/send",
                                 data={"mode": "scrap", "reason": ""},
                                 follow_redirects=True).text

    assert "обязательна причина" in page
    web_db.refresh(item)
    assert item.status is ReturnStatus.accepted
    assert web_db.query(FtpTask).count() == 0


def test_one_refusal_does_not_carry_away_the_rest(logged_in_client, web_db, goods):
    """Вещь без товара 1С отправить нельзя — но остальные уезжают.

    И она остаётся в пачке со своей причиной: получив «передано 1» при двух в
    коробке, человек обязан видеть, какая осталась.

    Отказывающая идёт в пачке ПЕРВОЙ намеренно. Поставь её второй — и цикл,
    обрывающийся на первом отказе, прошёл бы тест: здоровая вещь успела бы
    уехать до обрыва, а уносит он именно тех, кто стоит ПОСЛЕ.
    """
    orphan = _accept(web_db, "2000000000099")      # баркода нет в мэппинге
    good = _accept(web_db, "2000000000017")
    _mode(logged_in_client, "sale")
    _scan(logged_in_client, "2000000000099")
    _scan(logged_in_client, "2000000000017")

    page = logged_in_client.post("/returns/bulk/send", data={"mode": "sale"},
                                 follow_redirects=True).text

    assert "Передано в 1С: 1" in page
    assert "Осталось в пачке 1" in page
    web_db.refresh(good), web_db.refresh(orphan)
    assert good.status is ReturnStatus.awaiting_1c
    assert orphan.status is ReturnStatus.accepted
    assert web_db.query(ReturnBatchEntry).one().return_id == orphan.id


def test_the_page_shows_totals_split_by_platform(logged_in_client, web_db, goods):
    """Разбивка по площадкам не украшение: от площадки зависит склад-источник
    перемещения, а две площадки в одной коробке — сигнал, что попало лишнее."""
    _accept(web_db, "2000000000017", platform=Platform.wb)
    _accept(web_db, "2000000000024", platform=Platform.ozon)
    _scan(logged_in_client, "2000000000017")
    _scan(logged_in_client, "2000000000024")

    page = logged_in_client.get("/returns/bulk").text

    assert "WILDBERRIES" in page.upper() and "OZON" in page.upper()
    assert "Передать в 1С — 2 шт" in page


def test_the_send_button_asks_with_the_number(logged_in_client, web_db, goods):
    """Пачка переживает перезагрузку и смену человека: «передать всё» без числа
    не говорит ни о чём."""
    _accept(web_db, "2000000000017")
    _scan(logged_in_client, "2000000000017")

    page = logged_in_client.get("/returns/bulk").text

    assert "Передать в 1С: 1 шт" in page


def test_clearing_the_batch_leaves_the_items_alone(logged_in_client, web_db, goods):
    item = _accept(web_db, "2000000000017")
    _scan(logged_in_client, "2000000000017")

    logged_in_client.post("/returns/bulk/clear", follow_redirects=True)

    web_db.refresh(item)
    assert item.status is ReturnStatus.accepted
    assert web_db.query(ReturnBatchEntry).count() == 0


def test_cancelling_acceptance_takes_the_item_out_of_the_batch(
        logged_in_client, web_db, goods):
    """Осиротевшая строка пачки показала бы вещь, которой уже нет, и «Передать»
    спотыкалась бы о неё каждый раз. На SQLite каскад по умолчанию не работает."""
    item = _accept(web_db, "2000000000017")
    _scan(logged_in_client, "2000000000017")

    logged_in_client.post(f"/returns/{item.id}/cancel", follow_redirects=True)

    assert web_db.query(ReturnBatchEntry).count() == 0


def test_the_warehouse_account_can_open_the_page(logged_in_client, web_db):
    """Склад — единственный, кому эта страница и нужна."""
    assert logged_in_client.get("/returns/bulk").status_code == 200


def test_clearing_test_data_takes_items_out_of_the_batch(logged_in_client, web_db, goods):
    """Выход из тренировки стирает вещи — строка пачки обязана уйти с ними.

    Массовый `DELETE` не поднимает ни каскад ORM, ни `ON DELETE CASCADE`: на
    SQLite внешние ключи по умолчанию не проверяются вовсе. Осиротевшая строка
    показала бы в пачке вещь, которой уже нет.
    """
    R.set_test_mode(web_db, True)
    web_db.commit()
    item = R.accept(web_db, "2000000000017", Platform.wb, is_test=True)
    web_db.commit()
    R.add_to_batch(web_db, item)
    web_db.commit()

    R.clear_test_data(web_db)
    web_db.commit()

    assert web_db.query(ReturnBatchEntry).count() == 0


def test_a_scan_in_test_mode_keeps_the_batch_honest(logged_in_client, web_db, goods):
    """Тренировочная вещь видна в пачке ПОМЕЧЕННОЙ.

    `is_test` живёт на вещи, а не на моменте, и пачка это не меняет: смешать
    тренировочные с боевыми в одном списке молча значило бы отправить настоящий
    возврат «понарошку» — в 1С он не поехал бы никогда.
    """
    R.set_test_mode(web_db, True)
    web_db.commit()
    item = R.accept(web_db, "2000000000017", Platform.wb, is_test=True)
    web_db.commit()
    R.add_to_batch(web_db, item)
    web_db.commit()

    page = logged_in_client.get("/returns/bulk").text

    assert "ТРЕНИРОВКА" in page

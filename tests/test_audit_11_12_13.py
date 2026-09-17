"""Находки 11, 12, 13 аудита.

11. Сверка клампила отрицательный остаток в ноль: пересортица −3 превращалась в 0
    и в журнале сверки тоже, хотя весь остальной код минус специально хранит.
12. Симуляция на странице «Тестирование» меняла боевой `stock_on_hand`, и
    следующая РЕАЛЬНАЯ рассылка отправляла на площадку заниженное тестом число.
13. Артикул и наименование из 1С не экранируют разделитель `|`: наименование
    «Джинсы|36» сдвигало поля, количество читалось из соседней колонки, а
    баркод — из следующей.
"""
from app.models import (Product, Barcode, PlatformAccount, Platform, SyncSetting,
                        ProcessedOrder, OrderProcessStatus, DispatchQueueItem)
from app.workers.ftp_channel import (parse_stock_export_rows, parse_stock_export_file,
                                     parse_barcode_dict)
from app.workers.order_poller import process_new_order, process_cancellation, open_test_out
from app.workers.platform_clients.base import PlatformOrder
from app.workers.reconciliation import run_reconciliation
from tests.factories import make_account


# ------------------------------------------- 11. отрицательный остаток

def test_negative_stock_from_1c_is_preserved(db):
    """Пересортица −3 должна дойти до остатка как есть: на площадку всё равно
    уйдёт 0 (клампит рассылка), но величина расхождения не теряется."""
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=5, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()

    run_reconciliation(db, {"111": -3})

    product = db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == -3


def test_negative_stock_is_in_the_reconciliation_log(db):
    from app.models import ReconciliationLog

    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=5, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()

    run_reconciliation(db, {"111": -3})

    log = db.query(ReconciliationLog).filter(ReconciliationLog.uid_1c == "u1").first()
    assert log.actual_1c == -3
    assert log.delta == -8


def test_max_across_barcodes_still_wins(db):
    """Несколько баркодов одного товара — по-прежнему берём максимум: это один
    физический остаток, а не сумма."""
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=0, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(Barcode(barcode="222", uid_1c="u1"))
    db.commit()

    run_reconciliation(db, {"111": -3, "222": 4})

    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 4


def test_negative_stock_sends_zero_to_the_platform(db):
    """Граница: минус хранится, но на площадку уходит ноль, а не отрицательное."""
    from app.transmit import quantity_for_account

    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=-3, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    assert quantity_for_account(db, "u1", account.id, -3) == 0


# ------------------------------- 12. симуляция не трогает боевые поля

def _product_with_cabinets(db):
    a1 = make_account(db, name="Кабинет 1")
    a2 = make_account(db, platform=Platform.ozon, name="Кабинет 2")
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=100,
                   broadcast_enabled=True, transmit_override=50))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=a1.id, enabled=True))
    db.add(SyncSetting(uid_1c="u1", account_id=a2.id, enabled=True))
    db.commit()
    return a1, a2


def test_simulated_order_leaves_real_fields_alone(db):
    """Сценарий из аудита: остаток 100, симуляция заказа на 7 → в базе было 93,
    и следующая боевая запись отправляла 93 вместо 100."""
    a1, _ = _product_with_cabinets(db)

    result = process_new_order(db, PlatformOrder(order_id="TEST-1", barcode="111", quantity=7, raw_status="new"),
                               a1, "WB.Ожидает", is_test=True)

    product = db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 100        # боевой остаток не тронут
    assert product.transmit_override == 50     # и ручная цифра тоже
    assert result["new_stock"] == 93           # но показываем правдоподобное число


def test_real_order_still_decrements(db):
    """Обратная граница: настоящий заказ обязан списывать остаток как раньше."""
    a1, _ = _product_with_cabinets(db)

    result = process_new_order(db, PlatformOrder(order_id="777", barcode="111", quantity=7, raw_status="new"),
                               a1, "WB.Ожидает")

    product = db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 93
    assert product.transmit_override == 43
    assert result["new_stock"] == 93


def test_chained_simulations_look_like_a_real_sequence(db):
    """Две симуляции подряд должны считаться друг от друга, а не обе от 100 —
    иначе оператор увидит нереалистичные числа."""
    a1, _ = _product_with_cabinets(db)

    first = process_new_order(db, PlatformOrder(order_id="TEST-1", barcode="111", quantity=7, raw_status="new"),
                              a1, "WB.Ожидает", is_test=True)
    second = process_new_order(db, PlatformOrder(order_id="TEST-2", barcode="111", quantity=3, raw_status="new"),
                               a1, "WB.Ожидает", is_test=True)

    assert (first["new_stock"], second["new_stock"]) == (93, 90)
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 100


def test_simulated_cancel_returns_the_simulated_value(db):
    a1, _ = _product_with_cabinets(db)
    process_new_order(db, PlatformOrder(order_id="TEST-1", barcode="111", quantity=7, raw_status="new"),
                      a1, "WB.Ожидает", is_test=True)
    record = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "TEST-1").first()

    result = process_cancellation(db, PlatformOrder(order_id="TEST-1", barcode="111", quantity=7, raw_status="new"),
                                  record, a1, is_test=True)

    assert result["new_stock"] == 100
    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 100


def test_open_test_out_counts_only_open_test_orders(db):
    a1, _ = _product_with_cabinets(db)
    process_new_order(db, PlatformOrder(order_id="TEST-1", barcode="111", quantity=7, raw_status="new"),
                      a1, "WB.Ожидает", is_test=True)
    process_new_order(db, PlatformOrder(order_id="555", barcode="111", quantity=4, raw_status="new"),
                      a1, "WB.Ожидает")            # реальный заказ — не в счёт

    assert open_test_out(db, "u1") == 7

    record = db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "TEST-1").first()
    record.status = OrderProcessStatus.cancelled
    db.commit()

    assert open_test_out(db, "u1") == 0


def test_test_fanout_carries_the_simulated_value(db):
    """Тестовая запись в очереди должна нести симулированное число (на площадку
    она всё равно не уйдёт — её исключает is_test)."""
    a1, a2 = _product_with_cabinets(db)

    process_new_order(db, PlatformOrder(order_id="TEST-1", barcode="111", quantity=7, raw_status="new"),
                      a1, "WB.Ожидает", is_test=True)

    item = db.query(DispatchQueueItem).filter(DispatchQueueItem.account_id == a2.id).first()
    assert (item.quantity, item.is_test) == (93, True)


# --------------------------------- 13. разделитель внутри наименования

def test_pipe_in_name_does_not_shift_stock_export_fields():
    """Наименование «Джинсы|36»: количество и баркоды обязаны прочитаться верно."""
    rows = parse_stock_export_rows("uid-1|ART-1|Джинсы|36 синие|42|111,222|M|синий")

    assert len(rows) == 1
    row = rows[0]
    assert row["uid_1c"] == "uid-1"
    assert row["article"] == "ART-1"
    assert row["quantity"] == 42
    assert row["barcodes"] == ["111", "222"]
    assert row["size"] == "M"
    assert row["color"] == "синий"
    assert row["name"] == "Джинсы|36 синие"     # текст сохраняем как есть


def test_pipe_in_name_does_not_shift_flat_stock_map():
    assert parse_stock_export_file("uid-1|ART-1|Джинсы|36|42|111,222|M|синий") == {"111": 42, "222": 42}


def test_pipe_in_name_does_not_shift_barcode_dict():
    rows = parse_barcode_dict("uid-1|ART-1|Джинсы|36 синие|111|M|синий")

    assert rows[0]["barcode"] == "111"
    assert rows[0]["size"] == "M"
    assert rows[0]["color"] == "синий"
    assert rows[0]["name"] == "Джинсы|36 синие"


def test_normal_rows_are_unchanged():
    """Обычные строки без разделителя внутри полей разбираются ровно как раньше."""
    rows = parse_stock_export_rows("uid-1|ART-1|Джинсы прямые|42|111,222|M|синий")
    assert rows[0]["name"] == "Джинсы прямые"
    assert rows[0]["quantity"] == 42

    old_format = parse_stock_export_rows("uid-2|ART-2|Куртка|7|333")
    assert (old_format[0]["quantity"], old_format[0]["barcodes"]) == (7, ["333"])
    assert old_format[0]["size"] == ""


def test_negative_quantity_survives_the_parser():
    rows = parse_stock_export_rows("uid-1|ART-1|Джинсы|36|-3|111|M|синий")
    assert rows[0]["quantity"] == -3


def test_garbage_line_is_skipped_not_crashed():
    assert parse_stock_export_rows("совсем не строка обмена") == []

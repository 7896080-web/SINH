"""Три исправления по аудиту 16.09.2026 — каждое воспроизводит найденный дефект.

1. Сверка применяла снимок 1С часовой давности и раздувала остаток на объём продаж
   за час → оверселл.
2. Товар, распроданный в 1С до нуля, исчезал из выгрузки и не сверялся никогда →
   приложение вечно транслировало последнее ненулевое число → оверселл.
3. Синтетический TEST-заказ уходил в API площадки, клиент WB падал на разборе номера,
   и через пять циклов предохранитель гасил боевой кабинет.
"""
import os
from datetime import timedelta, timezone

from app.models import (Product, Barcode, ProcessedOrder, OrderProcessStatus, FtpTask,
                        FtpTaskStatus, Platform, SyncSetting, DispatchQueueItem)
from app.timeutils import now_utc
from app.workers.ftp_channel import LocalExchange, fetch_stock_export_rows
from app.workers.order_poller import poll_cancellations, poll_confirmations, TEST_ORDER_PREFIX
from app.workers.reconciliation import run_reconciliation
from tests.factories import make_account


def _exchange(tmp_path):
    ex = LocalExchange(tmp_path / "t", tmp_path / "r", tmp_path / "a")
    ex._ensure_dirs()
    return ex


def _write_stock(ex, name, body, mtime=None):
    """`mtime` — naive UTC, как его отдаёт now_utc(). Перевод в эпоху ОБЯЗАН быть
    явным: `.timestamp()` у naive-даты трактует её как локальное время, и на сервере
    в UTC+3 файл «постарел» бы на три часа (этот тест так и падал на сервере)."""
    p = ex.dir_results / name
    p.write_text(body, encoding="utf-8")
    if mtime is not None:
        ts = mtime.replace(tzinfo=timezone.utc).timestamp()
        os.utime(p, (ts, ts))
    return p


# ---------------------------------------------------------------- 1. свежесть снимка

def test_stale_stock_snapshot_is_skipped(tmp_path):
    """Файл старше последнего запроса выгрузки не применяется, а архивируется."""
    ex = _exchange(tmp_path)
    requested_at = now_utc()
    _write_stock(ex, "stock_20260919010000.txt", "u1|A|Т|10|111", mtime=requested_at - timedelta(hours=1))

    rows = fetch_stock_export_rows(ex, not_older_than=requested_at)

    assert rows == []
    assert (tmp_path / "a" / "stock_20260919010000.txt").exists()      # унесён в архив, не залипает
    assert not (tmp_path / "r" / "stock_20260919010000.txt").exists()


def test_fresh_stock_snapshot_is_applied(tmp_path):
    ex = _exchange(tmp_path)
    requested_at = now_utc() - timedelta(minutes=5)
    _write_stock(ex, "stock_20260919020000.txt", "u1|A|Т|8|111", mtime=now_utc())

    rows = fetch_stock_export_rows(ex, not_older_than=requested_at)

    assert [r["quantity"] for r in rows] == [8]


def test_only_newest_snapshot_wins(tmp_path):
    """Два снимка склеивать нельзя: товар, исчезнувший из нового, воскрес бы из старого."""
    ex = _exchange(tmp_path)
    base = now_utc()
    _write_stock(ex, "stock_20260916120000.txt", "u1|A|Т|10|111\nu2|B|Т2|4|222", mtime=base)
    _write_stock(ex, "stock_20260916130000.txt", "u1|A|Т|8|111", mtime=base + timedelta(minutes=1))

    rows = fetch_stock_export_rows(ex, not_older_than=base - timedelta(minutes=1))

    assert [(r["uid_1c"], r["quantity"]) for r in rows] == [("u1", 8)]


def test_stale_snapshot_no_longer_inflates_stock(db, tmp_path):
    """Сквозная проверка исходного сценария: продали 2, 1С уже знает про 8,
    в results лежит снимок часовой давности с 10 — остаток обязан остаться 8."""
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=8, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()

    ex = _exchange(tmp_path)
    requested_at = now_utc()
    _write_stock(ex, "stock_20260919010000.txt", "u1|A|Т|10|111", mtime=requested_at - timedelta(hours=1))
    rows = fetch_stock_export_rows(ex, not_older_than=requested_at)
    if rows:
        run_reconciliation(db, {"111": rows[0]["quantity"]}, missing_means_zero=True)

    assert db.query(Product).first().stock_on_hand == 8


# ---------------------------------------------------------------- 2. исчезнувший товар

def test_product_missing_from_full_snapshot_is_zeroed(db):
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=8, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(Product(uid_1c="u2", article="B", name="Т2", stock_on_hand=4, broadcast_enabled=True))
    db.add(Barcode(barcode="222", uid_1c="u2"))
    db.commit()

    # В 1С u2 распродан в ноль → строки в снимке нет.
    stats = run_reconciliation(db, {"111": 8}, missing_means_zero=True)

    assert stats["zeroed_missing"] == 1
    assert db.query(Product).filter(Product.uid_1c == "u2").first().stock_on_hand == 0


def test_missing_product_is_kept_when_flag_off(db):
    """Без флага поведение прежнее — частичная выгрузка ничего не обнуляет."""
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=8, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(Product(uid_1c="u2", article="B", name="Т2", stock_on_hand=4, broadcast_enabled=True))
    db.add(Barcode(barcode="222", uid_1c="u2"))
    db.commit()

    run_reconciliation(db, {"111": 8})

    assert db.query(Product).filter(Product.uid_1c == "u2").first().stock_on_hand == 4


def test_truncated_snapshot_does_not_zero_everything(db):
    """Снимок покрывает меньше половины прежних ненулевых товаров — не обнуляем."""
    for i in range(30):
        db.add(Product(uid_1c=f"u{i}", article=f"A{i}", name="Т", stock_on_hand=5, broadcast_enabled=True))
        db.add(Barcode(barcode=f"bc{i}", uid_1c=f"u{i}"))
    db.commit()

    stats = run_reconciliation(db, {"bc0": 5, "bc1": 5}, missing_means_zero=True)

    assert stats["zeroed_missing"] == 0
    assert stats["snapshot_suspicious"] == 2
    assert db.query(Product).filter(Product.stock_on_hand == 5).count() == 30


def test_product_without_barcode_is_not_zeroed(db):
    """Товар без баркода в выгрузке и не может появиться — не его вина."""
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=8, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(Product(uid_1c="nobc", article="N", name="Без баркода", stock_on_hand=7))
    db.commit()

    run_reconciliation(db, {"111": 8}, missing_means_zero=True)

    assert db.query(Product).filter(Product.uid_1c == "nobc").first().stock_on_hand == 7


# ---------------------------------------------------------------- 3. синтетика в API

class _Recorder:
    """Клиент, который запоминает, какие номера заказов ему отдали."""

    def __init__(self):
        self.asked_cancel = None
        self.asked_confirm = None

    def get_cancelled_orders(self, order_ids):
        self.asked_cancel = list(order_ids)
        return []

    def get_confirmed_orders(self, order_ids):
        self.asked_confirm = list(order_ids)
        return []


def _orders(db, account):
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=10, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(ProcessedOrder(account_id=account.id, order_id="5467811441", uid_1c="u1", quantity=1,
                          status=OrderProcessStatus.processed))
    db.add(ProcessedOrder(account_id=account.id, order_id=f"{TEST_ORDER_PREFIX}ab12cd34ef",
                          uid_1c="u1", quantity=1, status=OrderProcessStatus.processed))
    db.commit()


def test_synthetic_order_never_reaches_cancellation_polling(db):
    account = make_account(db, Platform.wb)
    _orders(db, account)
    client = _Recorder()

    poll_cancellations(db, client, account)

    assert client.asked_cancel == ["5467811441"]


def test_synthetic_order_never_reaches_confirmation_polling(db):
    account = make_account(db, Platform.wb)
    _orders(db, account)
    client = _Recorder()

    poll_confirmations(db, client, account, "WB.Ожидает", "Склад WB")

    assert client.asked_confirm == ["5467811441"]


def test_wb_client_skips_an_id_it_cannot_parse(monkeypatch):
    """Нечисловой номер заказа клиент WB отбрасывает, а не падает на всей пачке.

    Раньше падал — `int("TEST-abc")` бросал ValueError ещё до похода в сеть, — и
    это было ПРИЧИНОЙ падения живого опроса. Починили тогда с другой стороны
    (`_real_open_orders` не отдаёт синтетические заказы), и правильно; этот тест
    фиксировал саму причину, чтобы её не потеряли.

    Теперь причина убрана и здесь. Повод — не красота: нечисловой номер может
    взяться и без страницы тестирования. Заказ WB без поля `id` превращался в
    строку «None», оседал в `ProcessedOrder` — и дальше отмены переставали
    отслеживаться по ВСЕМУ кабинету, а через пять опросов предохранитель гасил
    его совсем. Одна испорченная запись не должна этого делать.

    Фильтр синтетических заказов при этом никуда не делся — его проверяют
    соседние тесты; здесь мы лишь убеждаемся, что вторая линия обороны есть.
    """
    from app.workers.platform_clients import wb as wb_module
    from app.workers.platform_clients.wb import WbClient

    asked = []

    def _fake_post(self, path, body):
        asked.append(body)
        return {"orders": []}

    monkeypatch.setattr(WbClient, "_post", _fake_post)
    client = WbClient(token="x", warehouse_id="w")

    # Только нечисловой — в сеть идти не с чем, запроса быть не должно.
    assert client.get_cancelled_orders([f"{TEST_ORDER_PREFIX}abc"]) == []
    assert asked == []

    # Нечисловой рядом с настоящим — настоящий доезжает.
    client.get_cancelled_orders([f"{TEST_ORDER_PREFIX}abc", "5467811441"])
    assert asked == [{"orders": [5467811441]}]


def test_file_mtime_is_timezone_independent(tmp_path):
    """Свежесть снимка не должна зависеть от пояса сервера: время файла и время
    запроса сравниваются в одной шкале (naive UTC). На сервере UTC+3 наивный
    перевод давал сдвиг на три часа, и свежий снимок отбрасывался как устаревший."""
    ex = _exchange(tmp_path)
    moment = now_utc()
    _write_stock(ex, "stock_x.txt", "u1|A|Т|5|111", mtime=moment)

    got = ex.file_mtime_utc("stock_x.txt")

    assert abs((got - moment).total_seconds()) < 2, (
        f"время файла {got} разошлось с now_utc() {moment} — проверьте пояс")

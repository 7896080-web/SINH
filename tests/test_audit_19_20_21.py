"""Находки 19, 20, 21 аудита — обработчики делают не то, что обещают.

19. Битый файл Excel ронял импорт в 500: все три импорта аккуратно собирают
    ошибки по строкам, но падали на ОТКРЫТИИ файла, ещё до первой строки.
20. «Загрузить заказы с площадки» обещает просмотр без изменений, а молча
    сохраняла дату старта задним числом — без сообщения и без записи в аудит.
21. Бэкфилл не откатывал сессию при ошибке: после неудачного commit внутри
    process_new_order следующее же обращение к базе вылетало наружу, оператор
    видел 500 на частично проведённом прогоне и повторял его.
"""
import io

import pytest
from openpyxl import Workbook

from app.excel_utils import ExcelReadError, read_xlsx_rows
from app.models import AuditLog, Product


# ------------------------------------------------ 19. битый файл Excel

def _xlsx(headers: list[str], rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_broken_file_raises_readable_error():
    """Не .xlsx вовсе (например, .csv, переименованный в .xlsx)."""
    with pytest.raises(ExcelReadError) as exc:
        read_xlsx_rows(b"ID_1C;Barcode\n1;111\n")

    assert "не читается как .xlsx" in str(exc.value)


def test_empty_file_raises_readable_error():
    with pytest.raises(ExcelReadError) as exc:
        read_xlsx_rows(b"")

    assert "пустой" in str(exc.value)


def test_truncated_xlsx_raises_readable_error():
    """Недокачанный файл: начало правильное, хвост обрезан."""
    data = _xlsx(["ID_1С"], [["u1"]])

    with pytest.raises(ExcelReadError):
        read_xlsx_rows(data[: len(data) // 2])


def test_valid_file_still_reads():
    rows = read_xlsx_rows(_xlsx(["ID_1С", "Резерв"], [["u1", 3]]))

    assert rows == [{"ID_1С": "u1", "Резерв": 3}]


@pytest.mark.parametrize("url", ["/products/import", "/mapping/import", "/anomalies/import"])
def test_import_pages_answer_with_a_message_not_500(logged_in_client, web_db, url):
    """Главное: оператор получает сообщение, а не 500. Проверяем все три импорта —
    дефект был общий."""
    r = logged_in_client.post(url, files={"file": ("сломано.xlsx", "это не xlsx".encode("utf-8"), "application/vnd.ms-excel")},
                              follow_redirects=False)

    assert r.status_code == 303                     # редирект с флешем, а не 500


def test_import_error_message_reaches_the_operator(logged_in_client, web_db):
    r = logged_in_client.post("/mapping/import",
                              files={"file": ("сломано.xlsx", "это не xlsx".encode("utf-8"), "application/vnd.ms-excel")},
                              follow_redirects=True)

    assert "не читается как .xlsx" in r.text


# --------------------------- 20. просмотр заказов ничего не меняет

def _product(web_db, uid: str = "u1"):
    from app.models import Barcode

    p = Product(uid_1c=uid, article="A", name="Т", stock_on_hand=10, broadcast_enabled=True)
    web_db.add(p)
    web_db.add(Barcode(barcode="111", uid_1c=uid))
    web_db.commit()
    return p


def test_loading_orders_does_not_save_the_start_date(logged_in_client, web_db):
    """Сценарий из аудита: дата старта задним числом менялась с пустой на
    2026-08-07 без сообщения и без записи в аудит."""
    product = _product(web_db)
    assert product.broadcast_active_since is None

    logged_in_client.post("/testing/load-real-orders",
                          data={"uid_1c": "u1", "account_id": "all", "start_date": "2026-08-07"})

    web_db.refresh(product)
    assert product.broadcast_active_since is None


def test_loading_orders_still_uses_the_typed_date(logged_in_client, web_db):
    """Но введённую дату просмотр обязан учитывать — иначе кнопка станет
    бесполезной."""
    _product(web_db)

    r = logged_in_client.post("/testing/load-real-orders",
                              data={"uid_1c": "u1", "account_id": "all", "start_date": "2026-08-07"})

    assert "2026-08-07" in r.text


def test_loading_orders_falls_back_to_the_saved_date(logged_in_client, web_db):
    from datetime import date

    product = _product(web_db)
    product.broadcast_active_since = date(2026, 8, 7)
    web_db.commit()

    r = logged_in_client.post("/testing/load-real-orders",
                              data={"uid_1c": "u1", "account_id": "all", "start_date": ""})

    assert "2026-08-07" in r.text
    web_db.refresh(product)
    assert product.broadcast_active_since == date(2026, 8, 7)   # не затёрли пустым


def test_backfill_saves_the_date_and_writes_it_to_audit(logged_in_client, web_db):
    """Бэкфилл — явное действие оператора: там дату сохраняем, но уже с записью
    в журнал действий, чтобы изменение не было молчаливым."""
    from datetime import date

    product = _product(web_db)

    logged_in_client.post("/testing/backfill",
                          data={"uid_1c": "u1", "account_id": "all", "start_date": "2026-08-07"})

    web_db.refresh(product)
    assert product.broadcast_active_since == date(2026, 8, 7)
    entry = web_db.query(AuditLog).filter(AuditLog.action == "active_since_changed").first()
    assert entry is not None
    assert "2026-08-07" in entry.details


# ------------------------------- 21. бэкфилл откатывает сессию

def test_backfill_survives_a_failing_order(logged_in_client, web_db, monkeypatch):
    """Сбой на одном заказе не должен ронять весь прогон: оператор видит
    сообщение, а не 500 на частично проведённых данных."""
    import app.routers.testing as testing_router
    from app.models import PlatformAccount, Platform, ProcessedOrder

    _product(web_db)
    account = PlatformAccount(platform=Platform.wb, name="Кабинет", warehouse_id="wh")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)

    rows = [
        {"order_id": "o1", "barcode": "111", "quantity": 1, "raw_status": "new",
         "order_date": None, "already": False, "account_id": account.id, "account_name": "Кабинет"},
        {"order_id": "o2", "barcode": "111", "quantity": 1, "raw_status": "new",
         "order_date": None, "already": False, "account_id": account.id, "account_name": "Кабинет"},
    ]
    monkeypatch.setattr(testing_router, "_fetch_real_orders", lambda *a, **kw: (rows, ""))

    calls = {"n": 0}

    def _boom(db, order, account, warehouse_pending, **kwargs):
        calls["n"] += 1
        if order.order_id == "o1":
            # Настоящий сбой коммита, как в бою: нарушение уникальности
            # (кабинет + номер заказа). Сессия после него ТРЕБУЕТ отката, и любое
            # следующее обращение к базе падает, пока откат не сделан.
            db.add(ProcessedOrder(account_id=account.id, order_id="dup", uid_1c="u1", quantity=1))
            db.commit()
            db.add(ProcessedOrder(account_id=account.id, order_id="dup", uid_1c="u1", quantity=1))
            db.commit()
        db.add(ProcessedOrder(account_id=account.id, order_id=order.order_id, uid_1c="u1", quantity=1))
        db.commit()
        return {"status": "processed", "ftp_task_id": None, "new_stock": 9}

    monkeypatch.setattr(testing_router, "process_new_order", _boom)

    r = logged_in_client.post("/testing/backfill",
                              data={"uid_1c": "u1", "account_id": "all", "start_date": "2026-08-07"},
                              follow_redirects=False)

    assert r.status_code == 303                    # не 500
    assert calls["n"] == 2                          # второй заказ всё же обработан
    assert web_db.query(ProcessedOrder).filter(ProcessedOrder.order_id == "o2").count() == 1


def test_backfill_log_survives_a_later_failure(logged_in_client, web_db, monkeypatch):
    """Строка журнала об успешном заказе не должна пропасть из-за отката,
    сделанного при сбое на следующем заказе."""
    import app.routers.testing as testing_router
    from app.models import PlatformAccount, Platform, TestLogEntry

    _product(web_db)
    account = PlatformAccount(platform=Platform.wb, name="Кабинет", warehouse_id="wh")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)

    rows = [
        {"order_id": "ok-1", "barcode": "111", "quantity": 1, "raw_status": "new",
         "order_date": None, "already": False, "account_id": account.id, "account_name": "Кабинет"},
        {"order_id": "bad-2", "barcode": "111", "quantity": 1, "raw_status": "new",
         "order_date": None, "already": False, "account_id": account.id, "account_name": "Кабинет"},
    ]
    monkeypatch.setattr(testing_router, "_fetch_real_orders", lambda *a, **kw: (rows, ""))

    def _half_broken(db, order, account, warehouse_pending, **kwargs):
        from app.models import ProcessedOrder

        if order.order_id == "bad-2":
            db.add(ProcessedOrder(account_id=account.id, order_id="dup", uid_1c="u1", quantity=1))
            db.commit()
            db.add(ProcessedOrder(account_id=account.id, order_id="dup", uid_1c="u1", quantity=1))
            db.commit()                     # IntegrityError: сессия сломана
        db.commit()
        return {"status": "processed", "ftp_task_id": None, "new_stock": 9}

    monkeypatch.setattr(testing_router, "process_new_order", _half_broken)

    logged_in_client.post("/testing/backfill",
                          data={"uid_1c": "u1", "account_id": "all", "start_date": "2026-08-07"})

    messages = [e.message for e in web_db.query(TestLogEntry).all()]
    assert any("ok-1" in m for m in messages)
    assert any("bad-2" in m and "IntegrityError" in m for m in messages)

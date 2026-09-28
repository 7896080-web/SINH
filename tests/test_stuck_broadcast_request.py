"""Просьба «включить трансляцию» не должна повисать молча.

Просьба — это обещание: оператор одним файлом задаёт дату, факт, кабинеты и
«Трансляция = Да», а включает строку тот, кто имеет право, ровно тогда, когда
включила бы и страница. Обещание сдерживается почти всегда — и потому
несдержанное особенно незаметно: человек считает работу сделанной и к этим
строкам не возвращается, а товар молчит. Остаток наружу не уходит, продаж нет,
и нигде не горит ни одной ошибки.

Здесь закрыты обе стороны: невыполнимую просьбу не запоминаем вовсе, а
зависшую по другой причине показываем в отчёте.
"""
import io
from datetime import date, timedelta

from openpyxl import Workbook

from app.models import Barcode, Product
from app.report import _check_stuck_broadcast_requests
from app.timeutils import now_utc
from tests.factories import make_account

DAY = date(2026, 8, 7)


def _file(headers, *rows):
    wb = Workbook(); ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def _import(client, content):
    return client.post("/products/import", files={"file": ("t.xlsx", content)},
                       follow_redirects=True)


def _seed(db, **kw):
    fields = dict(article="A-1", name="Товар", stock_on_hand=20, reserve=0)
    fields.update(kw)
    db.add(Product(uid_1c="u1", **fields))
    db.add(Barcode(barcode="bc-u1", uid_1c="u1"))
    db.commit()


# ------------------------------------------------ невыполнимую не запоминаем

def test_a_request_without_a_fact_is_refused_up_front(logged_in_client, web_db):
    """«Ждём 1С» рассосётся само — но рассосётся оно в «нужен факт», если факта
    нет ни у товара, ни в файле. А факт вводит человек: просьба повисла бы
    навсегда, причём молча — оператор ведь считает, что всё указал файлом."""
    account = make_account(web_db, name="ИП ЯВОРСКАЯ")
    _seed(web_db)

    r = _import(logged_in_client, _file(
        ["ID_1С", "Дата расчёта", "Трансляция",
         f"{account.name} ({account.platform.value.upper()}) — Синхронизировать"],
        ["u1", "2026-08-07", "Да", "Да"]))

    web_db.expire_all()
    product = web_db.query(Product).one()
    assert product.broadcast_requested_at is None, "обещание, которое некому выполнить"
    assert "Факт на дату" in r.text


def test_a_request_with_a_fact_is_still_remembered(logged_in_client, web_db):
    """Правка не должна закрыть главный сценарий: файл с датой, фактом и
    кабинетами обязан по-прежнему ставить просьбу."""
    account = make_account(web_db, name="ИП ЯВОРСКАЯ")
    _seed(web_db)

    _import(logged_in_client, _file(
        ["ID_1С", "Дата расчёта", "Факт на дату", "Трансляция",
         f"{account.name} ({account.platform.value.upper()}) — Синхронизировать"],
        ["u1", "2026-08-07", 18, "Да", "Да"]))

    web_db.expire_all()
    assert web_db.query(Product).one().broadcast_requested_at is not None


# ------------------------------------------------ зависшую показываем

def test_a_fresh_request_is_not_a_finding(db):
    """Расчёт идёт минутами, ответ 1С — до десяти минут. Ругаться на свежую
    просьбу значит приучить оператора пролистывать отчёт."""
    db.add(Product(uid_1c="u1", article="A", name="Товар", stock_on_hand=5,
                   broadcast_enabled=False, broadcast_requested_at=now_utc()))
    db.commit()

    assert _check_stuck_broadcast_requests(db) is None


def test_a_request_older_than_a_day_is_a_finding(db):
    db.add(Product(uid_1c="u1", article="A", name="Товар", stock_on_hand=5,
                   broadcast_enabled=False,
                   broadcast_requested_at=now_utc() - timedelta(hours=30)))
    db.commit()

    finding = _check_stuck_broadcast_requests(db)

    assert finding is not None
    assert finding.count == 1
    assert "не уходит" in finding.consequence


def test_an_enabled_row_is_not_a_finding(db):
    """Просьба исполнена — строка включена. Остаток просьбы в такой строке был бы
    нашей же недоработкой, а не расхождением."""
    db.add(Product(uid_1c="u1", article="A", name="Товар", stock_on_hand=5,
                   broadcast_enabled=True,
                   broadcast_requested_at=now_utc() - timedelta(hours=30)))
    db.commit()

    assert _check_stuck_broadcast_requests(db) is None

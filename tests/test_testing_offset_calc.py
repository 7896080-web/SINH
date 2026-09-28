"""Расчёт порога на странице «Тестирование».

Тот же механизм, что на «Товарах и остатках», перенесён сюда ради одной задачи:
проверить старт задним числом ЧИСЛАМИ, а не на глаз. Оператор видит, сколько
уходило бы на саму дату старта и сколько уходит сейчас — после всех проведённых
бэкфиллом заказов и движений склада.

Формула здесь не дублируется: страница зовёт тот же `recompute_offset`. Двух
реализаций одной формулы в проекте уже было достаточно — расчёт отправки
разъезжался трижды, пока его не свели в `app/transmit.py`.
"""
from datetime import date, timedelta

from app.models import (Barcode, Platform, PlatformAccount, Product, StockDateRow,
                        StockDateSnapshot, StockDateStatus)
from app.timeutils import now_utc, today_local

DAY = date(2026, 8, 7)


def _product(web_db, uid="u1", stock=11, reserve=0) -> Product:
    p = Product(uid_1c=uid, article="A-1", name="Товар", stock_on_hand=stock,
                reserve=reserve, broadcast_enabled=True)
    web_db.add(p)
    web_db.add(Barcode(barcode="111", uid_1c=uid))
    web_db.commit()
    return p


def _account(web_db) -> PlatformAccount:
    a = PlatformAccount(platform=Platform.wb, name="WB-1", warehouse_id="wh")
    web_db.add(a)
    web_db.commit()
    web_db.refresh(a)
    return a


def _snapshot(web_db, rows, day=DAY):
    snap = StockDateSnapshot(snapshot_date=day, status=StockDateStatus.done, rows_count=len(rows))
    web_db.add(snap)
    web_db.commit()
    web_db.refresh(snap)
    for uid, qty in rows:
        web_db.add(StockDateRow(snapshot_id=snap.id, uid_1c=uid, quantity=qty))
    web_db.commit()
    return snap


def _calc(client, **kw):
    data = {"uid_1c": "u1", "account_id": "", "base_date": "2026-08-07", "fact": ""}
    data.update(kw)
    return client.post("/testing/offset-calc", data=data)


# ------------------------------------------------ считает то же самое

def test_the_same_formula_as_on_the_products_page(logged_in_client, web_db):
    _snapshot(web_db, [("u1", 10)])
    product = _product(web_db, reserve=2)

    _calc(logged_in_client, fact="8")

    web_db.expire_all()
    assert web_db.query(Product).first().broadcast_offset == 4


def test_the_card_shows_both_numbers(logged_in_client, web_db):
    """Ради этого расчёт сюда и перенесён: «уходило бы на дату старта» и
    «уходит сейчас» рядом — по ним и видно, отработал ли бэкфилл."""
    _snapshot(web_db, [("u1", 10)])
    _product(web_db, stock=11, reserve=2)
    _calc(logged_in_client, fact="8")

    page = logged_in_client.get("/testing?uid_1c=u1")

    assert "Уходило бы на дату старта" in page.text
    assert "Уходит сейчас" in page.text


def test_an_empty_fact_falls_back_to_the_1c_number(logged_in_client, web_db):
    _snapshot(web_db, [("u1", 10)])
    _product(web_db, reserve=2)

    _calc(logged_in_client, fact="")

    web_db.expire_all()
    product = web_db.query(Product).first()
    assert product.fact_at_date is None
    assert product.broadcast_offset == 2        # порог сводится к брони


# ------------------------------------------------ выгрузку просим сами

def test_a_date_without_a_snapshot_orders_one_from_1c(logged_in_client, web_db):
    """Без этого связка дырявая: оператор задал дату, а попросить срез у 1С
    должен не забыть руками на другой странице."""
    _product(web_db)

    _calc(logged_in_client)

    assert web_db.query(StockDateSnapshot).filter(
        StockDateSnapshot.snapshot_date == DAY).count() == 1


def test_a_second_request_for_the_same_day_is_not_created(logged_in_client, web_db):
    """Одна открытая заявка на дату: 1С называет файл по дате, и второй заявке
    ответа не досталось бы — висела бы до таймаута и выглядела бы как сбой."""
    _product(web_db)
    _calc(logged_in_client)

    _calc(logged_in_client, fact="3")

    assert web_db.query(StockDateSnapshot).count() == 1


def test_nothing_is_ordered_when_the_answer_is_already_here(logged_in_client, web_db):
    _snapshot(web_db, [("u1", 10)])
    _product(web_db)

    _calc(logged_in_client, fact="8")

    assert web_db.query(StockDateSnapshot).count() == 1      # только готовый снимок


def test_waiting_is_explained_on_the_page(logged_in_client, web_db):
    _product(web_db)
    _calc(logged_in_client)

    page = logged_in_client.get("/testing?uid_1c=u1")

    assert "ждём ответа" in page.text


# ------------------------------------------------ ввод проверяется

def test_a_future_date_is_refused(logged_in_client, web_db):
    product = _product(web_db)
    tomorrow = (today_local() + timedelta(days=1)).isoformat()

    _calc(logged_in_client, base_date=tomorrow)

    web_db.expire_all()
    assert web_db.query(Product).first().offset_base_date is None
    assert web_db.query(StockDateSnapshot).count() == 0


def test_a_comma_in_the_fact_does_not_pass_silently(logged_in_client, web_db):
    _snapshot(web_db, [("u1", 10)])
    product = _product(web_db)

    r = _calc(logged_in_client, fact="12,5")

    assert "введите целое число" in r.text
    web_db.expire_all()
    assert web_db.query(Product).first().fact_at_date is None

"""Календарный день, названный человеком, — местный, а не UTC.

19.09 в 00:10 МСК на бою упал `test_a_future_date_is_refused`: `/testing`
сверял дату с местным `date.today()`, а «Товары», массовые действия и «Остатки
на дату» — с `now_utc().date()`. У Москвы UTC+3, и с полуночи до трёх часов
местное число уже на день больше UTC-шного. В эти три часа СЕГОДНЯШНЯЯ дата
отвергалась словами «остатков на будущую дату в 1С нет», а календарь на
«Остатках на дату» упирался максимумом во вчера — заказать срез на сегодня было
нельзя вовсе. 1С стоит на той же машине и живёт по её часам, так что правильное
«сегодня» тут местное.

Тесты ниже проверяют обе стороны: сегодняшнее местное число принимают ВСЕ четыре
точки ввода (это и ломалось ночью), а завтрашнее — не принимает ни одна.
"""
import pathlib
import re
from datetime import timedelta

from app.models import Barcode, Product, StockDateSnapshot
from app.timeutils import today_local

TODAY = today_local()
TOMORROW = TODAY + timedelta(days=1)


def _product(web_db, uid="u1") -> Product:
    p = Product(uid_1c=uid, article="A-1", name="Товар", stock_on_hand=11, reserve=0)
    web_db.add(p)
    web_db.add(Barcode(barcode="111", uid_1c=uid))
    web_db.commit()
    return p


# --------------------------------------------- сегодня принимают все точки ввода

def test_products_page_accepts_today(logged_in_client, web_db):
    product = _product(web_db)

    logged_in_client.post("/products/u1/base-date", data={"value": TODAY.isoformat()})

    web_db.refresh(product)
    assert product.offset_base_date == TODAY


def test_bulk_accepts_today(logged_in_client, web_db):
    product = _product(web_db)

    logged_in_client.post("/products/bulk", data={
        "action": "set_base_date", "uids": ["u1"], "date_value": TODAY.isoformat()})

    web_db.refresh(product)
    assert product.offset_base_date == TODAY


def test_testing_page_accepts_today(logged_in_client, web_db):
    _product(web_db)

    logged_in_client.post("/testing/offset-calc", data={
        "uid_1c": "u1", "account_id": "", "base_date": TODAY.isoformat(), "fact": ""})

    web_db.expire_all()
    assert web_db.query(Product).first().offset_base_date == TODAY


def test_stock_on_date_accepts_today(logged_in_client, web_db):
    logged_in_client.post("/stock-on-date/request", data={"value": TODAY.isoformat()},
                          follow_redirects=True)

    assert web_db.query(StockDateSnapshot).filter(
        StockDateSnapshot.snapshot_date == TODAY).count() == 1


def test_the_date_picker_offers_today_as_its_maximum(logged_in_client):
    """Календарь на «Остатках на дату» подставлял и ограничивал вчерашним
    числом — руками сегодняшнее было не ввести."""
    page = logged_in_client.get("/stock-on-date")

    assert f'max="{TODAY.isoformat()}"' in page.text


# --------------------------------------------- завтра не принимает ни одна

def test_no_entry_point_accepts_tomorrow(logged_in_client, web_db):
    product = _product(web_db)
    t = TOMORROW.isoformat()

    logged_in_client.post("/products/u1/base-date", data={"value": t})
    logged_in_client.post("/products/bulk", data={
        "action": "set_base_date", "uids": ["u1"], "date_value": t})
    logged_in_client.post("/testing/offset-calc", data={
        "uid_1c": "u1", "account_id": "", "base_date": t, "fact": ""})
    logged_in_client.post("/stock-on-date/request", data={"value": t},
                          follow_redirects=True)

    web_db.expire_all()
    assert web_db.query(Product).first().offset_base_date is None
    assert web_db.query(StockDateSnapshot).count() == 0


# --------------------------------------------- и правило зафиксировано в исходниках

def test_no_calendar_day_is_taken_from_utc():
    """Поведенческие тесты выше ловят расхождение только те три часа в сутки,
    когда местная дата и UTC-шная разошлись, — то есть почти никогда на CI.
    Поэтому само правило закреплено здесь: календарный день берётся только
    через `today_local()`. Отметки времени (`now_utc()`) под запрет не попадают
    — в UTC им и место, запрещено ровно взятие ДАТЫ из UTC-времени.
    """
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"now_utc\(\)\s*\.date\(\)|\bdate\.today\(\)", text):
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(app_dir.parent)}:{line}: {m.group(0)}")

    assert not offenders, (
        "календарный день берётся мимо today_local():\n  " + "\n  ".join(offenders))

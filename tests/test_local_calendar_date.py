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

# Дата берётся В МОМЕНТ ТЕСТА, а не при импорте модуля, и это не педантизм.
# Сбор тестов идёт раньше их выполнения, и прогон, пересёкший полночь, сравнивал
# бы вчерашнее число с сегодняшним: «завтра» становится «сегодня» и начинает
# приниматься, а календарь предлагает уже не то число. Поймано 22.09 в 00:04 UTC
# ровно так — два падения на полном прогоне и восемь зелёных при повторе минутой
# позже.
#
# Цена не в путанице: FINISH наката гоняет `pytest` ПЕРЕД перезапуском служб, и
# накат, начатый под местную полночь, упёрся бы в красные тесты и отказался
# перезапускать бой. Файл про полуночные дефекты обязан переживать полночь сам.
def _today():
    return today_local()


def _tomorrow():
    return today_local() + timedelta(days=1)

# Взятие ДАТЫ из UTC-времени. Имя класса не фиксируем: в клиентах площадок он
# зовётся `_dt`, а импорт `from datetime import datetime as _dt` — обычное дело.
FROM_UTC = (r"now_utc\(\)\s*\.date\(\)"
            r"|\bdate\.today\(\)"
            r"|\.now\(\s*timezone\.utc\s*\)\s*\.date\(\)"
            r"|\.fromtimestamp\([^\n]*\)\s*\.date\(\)"
            r"|\.fromisoformat\([^\n]*\)\s*\.date\(\)")

# Сборка момента из местного числа через UTC-полночь.
UTC_MIDNIGHT = r"\w+\([^()\n]*\.year[^()\n]*tzinfo\s*=\s*timezone\.utc[^()\n]*\)"


def _product(web_db, uid="u1") -> Product:
    p = Product(uid_1c=uid, article="A-1", name="Товар", stock_on_hand=11, reserve=0)
    web_db.add(p)
    web_db.add(Barcode(barcode="111", uid_1c=uid))
    web_db.commit()
    return p


# --------------------------------------------- сегодня принимают все точки ввода

def test_products_page_accepts_today(logged_in_client, web_db):
    product = _product(web_db)

    logged_in_client.post("/products/u1/base-date", data={"value": _today().isoformat()})

    web_db.refresh(product)
    assert product.offset_base_date == _today()


def test_bulk_accepts_today(logged_in_client, web_db):
    product = _product(web_db)

    logged_in_client.post("/products/bulk", data={
        "action": "set_base_date", "uids": ["u1"], "date_value": _today().isoformat()})

    web_db.refresh(product)
    assert product.offset_base_date == _today()


def test_testing_page_accepts_today(logged_in_client, web_db):
    _product(web_db)

    logged_in_client.post("/testing/offset-calc", data={
        "uid_1c": "u1", "account_id": "", "base_date": _today().isoformat(), "fact": ""})

    web_db.expire_all()
    assert web_db.query(Product).first().offset_base_date == _today()


def test_stock_on_date_accepts_today(logged_in_client, web_db):
    logged_in_client.post("/stock-on-date/request", data={"value": _today().isoformat()},
                          follow_redirects=True)

    assert web_db.query(StockDateSnapshot).filter(
        StockDateSnapshot.snapshot_date == _today()).count() == 1


def test_the_date_picker_offers_today_as_its_maximum(logged_in_client):
    """Календарь на «Остатках на дату» подставлял и ограничивал вчерашним
    числом — руками сегодняшнее было не ввести."""
    page = logged_in_client.get("/stock-on-date")

    assert f'max="{_today().isoformat()}"' in page.text


# --------------------------------------------- завтра не принимает ни одна

def test_no_entry_point_accepts_tomorrow(logged_in_client, web_db):
    product = _product(web_db)
    t = _tomorrow().isoformat()

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
        if path.name == "timeutils.py":
            continue                      # сами преобразования живут там
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(FROM_UTC, text):
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(app_dir.parent)}:{line}: {m.group(0)}")

    assert not offenders, (
        "календарный день берётся мимо today_local()/local_date_of():\n  "
        + "\n  ".join(offenders))


def test_no_local_day_is_turned_into_a_moment_through_utc_midnight():
    """Обратная сторона того же: местное число → момент времени.

    `datetime(год, месяц, число, tzinfo=timezone.utc)` выглядит как «начало
    этого дня», а даёт 03:00 по Москве. Все три клиента площадок так и
    спрашивали ленту заказов от базовой даты — и продажи первых трёх часов
    суток оставались за границей запроса, не мешая при этом поставить
    «актуализирован». Правильное начало местных суток считает
    `timeutils.local_day_start_utc`.
    """
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path.name == "timeutils.py":
            continue
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(UTC_MIDNIGHT, text):
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(app_dir.parent)}:{line}: {m.group(0)}")

    assert not offenders, (
        "начало местных суток берётся мимо local_day_start_utc():\n  "
        + "\n  ".join(offenders))

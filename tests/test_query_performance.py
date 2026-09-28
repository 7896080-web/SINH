"""Отклик страниц на каталоге в 152 тысячи позиций.

21.09 замер на боевом каталоге (152 235 товаров, 154 232 баркода, 276 856 строк
сверки) не дошёл дальше второй строки: поиск на «Товарах» не отвечал вовсе.
Причина — коррелированный подзапрос по баркодам, выполняемый НА КАЖДЫЙ товар,
при том что `barcodes.uid_1c` индекса не имел. То есть на каждый из 152 тысяч
товаров читались все 154 тысячи баркодов.

Здесь закреплено то, что легко потерять обратно и незаметно: сами индексы и
форма запроса. На тестовой базе в десяток строк любая из этих правок «работает»
одинаково — дефект виден только на объёме, то есть только на бою.
"""

import pathlib

from sqlalchemy import inspect

from app.models import Barcode, Product


# ------------------------------------------------------------------ индексы

def _indexed_columns(db, table: str) -> set[tuple]:
    return {tuple(i["column_names"]) for i in inspect(db.bind).get_indexes(table)}


def test_barcodes_are_indexed_by_product(db):
    """Самый дорогой из всех: по `uid_1c` баркоды ищут и поиск, и строка товара,
    и гашение аномалий, и выбор ключа отправки."""
    assert ("uid_1c",) in _indexed_columns(db, "barcodes")


def test_the_dispatch_queue_is_indexed_by_pair_and_status(db):
    """Очередь спрашивают ровно двумя способами: по паре товар+кабинет
    (постановка, отзыв, «было ли что отзывать») и по статусу (рассылка берёт
    pending, отчёт считает error)."""
    indexes = _indexed_columns(db, "dispatch_queue")

    assert ("uid_1c", "account_id") in indexes
    assert ("status",) in indexes


def test_the_reconciliation_log_is_indexed(db):
    """Самая большая таблица системы и растёт каждый час. Отчёт берёт из неё
    сутки по `checked_at`."""
    indexes = _indexed_columns(db, "reconciliation_log")

    assert ("checked_at",) in indexes
    assert ("uid_1c",) in indexes


def test_1c_tasks_are_indexed_by_status(db):
    """По статусу их разбирают постоянно: «в пути» для остатка, `timeout` для
    повтора, `pending` для сборки файла."""
    assert ("status",) in _indexed_columns(db, "ftp_tasks")


# ------------------------------------------------------- форма запроса поиска

def _search_block() -> str:
    """Кусок `_base_query`, отвечающий за поиск по строке. Именно он и был
    дефектом; соседние фильтры к делу не относятся и свой `exists` имеют по
    праву."""
    source = pathlib.Path("app/routers/products.py").read_text(encoding="utf-8")
    body = source.split("def _base_query", 1)[1].split("def ", 1)[0]
    return body.split("if q:", 1)[1].split("if only_proposals:", 1)[0]


def test_the_barcode_search_asks_once_not_per_product():
    """Коррелированный подзапрос тут и был дефектом: на объёме он превращается в
    произведение двух таблиц. `IN (подзапрос)` SQLite считает один раз."""
    block = _search_block()

    assert "Product.uid_1c.in_(" in block
    assert ".exists()" not in block, "вернулся коррелированный подзапрос по баркодам"


def test_a_search_without_digits_does_not_touch_barcodes():
    """Поиск «куртка» по таблице баркодов — это чтение 154 тысяч строк ради
    заведомо пустого результата."""
    assert "isdigit()" in _search_block()


def test_the_list_does_not_join_all_barcodes():
    """Строке нужен от баркодов один факт — есть он или нет. `joinedload` тянул
    к каждому товару все его баркоды и размножал результат."""
    source = pathlib.Path("app/routers/products.py").read_text(encoding="utf-8")
    body = source.split("def _base_query", 1)[1].split("def ", 1)[0]
    # Именно строка запроса, а не упоминание в комментарии о том, почему убрали.
    options = [line for line in body.splitlines() if ".options(" in line]

    assert options, "запрос списка потерялся — проверка ничего не проверяет"
    assert all("Product.barcodes" not in line for line in options)


# --------------------------------------------------------- поведение прежнее

def test_a_product_is_still_found_by_a_full_barcode(logged_in_client, web_db):
    web_db.add(Product(uid_1c="u1", article="AAA111", name="Куртка", stock_on_hand=5))
    web_db.add(Barcode(barcode="2000932310763", uid_1c="u1"))
    web_db.commit()

    assert "AAA111" in logged_in_client.get("/products?q=2000932310763").text


def test_a_word_search_still_finds_the_name(logged_in_client, web_db):
    web_db.add(Product(uid_1c="u1", article="AAA111", name="Куртка", stock_on_hand=5))
    web_db.commit()

    assert "AAA111" in logged_in_client.get("/products?q=Куртка").text


def test_a_product_without_a_barcode_is_still_marked(logged_in_client, web_db):
    """Признак «нет баркода» теперь приезжает отдельным запросом на страницу, а
    не через join. Строка обязана показывать его по-прежнему."""
    web_db.add(Product(uid_1c="u1", article="БЕЗБАРКОДА", name="Товар", stock_on_hand=5))
    web_db.commit()

    page = logged_in_client.get("/products?q=БЕЗБАРКОДА").text

    assert "БЕЗБАРКОДА" in page
    assert "нет баркода" in page.lower()


def test_a_product_with_a_barcode_is_not_marked(logged_in_client, web_db):
    web_db.add(Product(uid_1c="u1", article="СБАРКОДОМ", name="Товар", stock_on_hand=5))
    web_db.add(Barcode(barcode="4600000000001", uid_1c="u1"))
    web_db.commit()

    page = logged_in_client.get("/products?q=СБАРКОДОМ").text

    assert "СБАРКОДОМ" in page
    assert "нет баркода" not in page.lower()


# ------------------------------------------------------------ вес страницы

def test_the_filtered_page_is_capped(logged_in_client, web_db):
    """Строка весит около 6 КБ. Пять тысяч строк — это 32 МБ разметки: столько
    не успевает ни сервер собрать, ни браузер разложить. Массовой правке потолок
    не мешает — галочка в шапке берёт весь отбор, и страница об этом пишет."""
    from app.routers.products import FILTERED_LIMIT

    assert FILTERED_LIMIT <= 1000, "на этом объёме страница снова станет неподъёмной"


def test_the_page_says_how_many_it_hid(logged_in_client, web_db):
    for i in range(12):
        web_db.add(Product(uid_1c=f"u{i}", article=f"ART{i:03d}", name="Куртка",
                           stock_on_hand=1))
    web_db.commit()

    import app.routers.products as products
    original = products.FILTERED_LIMIT
    products.FILTERED_LIMIT = 5
    try:
        page = logged_in_client.get("/products?q=Куртка").text
    finally:
        products.FILTERED_LIMIT = original

    assert "Показано 5 из 12" in page

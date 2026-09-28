"""Поиск по штрихкоду и работа только с отмеченными строками.

Штрихкод — то, чем товар называют снаружи: он виден в кабинете площадки, он
же на сканере. Пока поиск его не знал, оператор шёл в «Мэппинг», там узнавал
артикул и возвращался сюда искать заново.

Фильтр «только отмеченные» — про другой сценарий: отметил нужные строки,
включил фильтр, работаешь со списком из них одних, не боясь задеть соседние.
Отметки живут в браузере, поэтому и фильтр браузерный — проверяется разметка
и скрипт, как и для восстановления отметок.
"""

from app.models import Barcode, Product


def _seed(web_db):
    web_db.add(Product(uid_1c="u1", article="TC26-2735", name="БлекВинил Куртка",
                       stock_on_hand=5))
    web_db.add(Barcode(barcode="2000932310763", uid_1c="u1"))
    web_db.add(Product(uid_1c="u2", article="D86321", name="Даунтлесс Куртка",
                       stock_on_hand=3))
    web_db.add(Barcode(barcode="4600000000001", uid_1c="u2"))
    web_db.commit()


# ------------------------------------------------------ поиск по штрихкоду

def test_a_product_is_found_by_its_barcode(logged_in_client, web_db):
    _seed(web_db)

    page = logged_in_client.get("/products?q=2000932310763")

    assert "TC26-2735" in page.text
    assert "D86321" not in page.text


def test_a_partial_barcode_works_too(logged_in_client, web_db):
    """Со сканера приходит целиком, а руками набирают хвост — по нему и ищут."""
    _seed(web_db)

    page = logged_in_client.get("/products?q=932310763")

    assert "TC26-2735" in page.text


def test_searching_by_article_still_works(logged_in_client, web_db):
    _seed(web_db)

    page = logged_in_client.get("/products?q=D86321")

    assert "Даунтлесс" in page.text
    assert "БлекВинил" not in page.text


def test_searching_by_name_still_works(logged_in_client, web_db):
    _seed(web_db)

    page = logged_in_client.get("/products?q=БлекВинил")

    assert "TC26-2735" in page.text


def test_a_product_is_not_duplicated_by_several_barcodes(logged_in_client, web_db):
    """У размер-цвета несколько баркодов. JOIN размножил бы строку товара по
    числу совпавших — поэтому поиск идёт коррелированным EXISTS."""
    _seed(web_db)
    web_db.add(Barcode(barcode="2000932310764", uid_1c="u1"))
    web_db.add(Barcode(barcode="2000932310765", uid_1c="u1"))
    web_db.commit()

    page = logged_in_client.get("/products?q=200093231076")

    assert page.text.count("TC26-2735") == page.text.count('id="row-u1"')


def test_the_search_field_mentions_the_barcode(logged_in_client, web_db):
    _seed(web_db)

    page = logged_in_client.get("/products")

    assert "штрихкод" in page.text


def test_the_barcode_search_survives_a_bulk_edit(logged_in_client, web_db):
    """Оператор сузил список штрихкодом именно затем, чтобы работать с ним."""
    _seed(web_db)

    r = logged_in_client.post("/products/bulk", data={
        "action": "set_reserve", "int_value": "1", "uids": ["u1"],
        "q": "2000932310763",
    }, follow_redirects=False)

    assert r.status_code == 303
    assert "2000932310763" in r.headers["location"]


# ------------------------------------------------ фильтр «только отмеченные»

def test_the_selection_filter_is_offered(logged_in_client, web_db):
    _seed(web_db)

    page = logged_in_client.get("/products")

    assert 'id="only-selected"' in page.text
    assert "Только отмеченные товары" in page.text


def test_the_selection_filter_is_not_sent_to_the_server(logged_in_client, web_db):
    """Отметки живут в браузере, и отправить их запросом нельзя: отмеченных
    бывают сотни, адрес строки такого не выдержит. Значит у фильтра не должно
    быть имени — иначе он молча уедет в запрос и ничего там не найдёт."""
    _seed(web_db)

    page = logged_in_client.get("/products")
    block = page.text.split('id="only-selected"', 1)[0][-200:]

    assert 'name="only_selected"' not in page.text
    assert "checkbox" in block


def test_the_filter_hides_rows_and_reacts_to_changes():
    """Проверяем скрипт: JS в этом проекте не исполняется ни в одном тесте, и
    выбор здесь — либо такая проверка, либо никакой.

    Важнее всего пересборка при СМЕНЕ отметок: снятая галочка обязана убрать
    строку из отфильтрованного списка сразу, иначе её продолжат править,
    считая отмеченной."""
    page = open("app/templates/products.html", encoding="utf-8").read()

    assert "applyOnlySelected" in page
    handler = page.split("function applyOnlySelected", 1)[1][:400]
    assert "hidden" in handler, "строки прячутся, а не удаляются"
    assert 'e.target.id === "only-selected"' in page
    assert 'e.target.name === "uids"' in page, "снятие отметки обязано перерисовать список"


def test_the_filter_is_restored_after_a_redraw():
    """Строка перерисовывается htmx'ом поодиночке. Если фильтр не применить
    заново, спрятанная строка вернётся на экран посреди отфильтрованного
    списка."""
    page = open("app/templates/products.html", encoding="utf-8").read()

    restore = page.split("function restore()", 1)[1][:300]
    assert "applyOnlySelected()" in restore


# ------------------------------- Excel не должен быть обходным путём

def test_excel_cannot_switch_broadcast_on_without_the_calculation(logged_in_client, web_db):
    """Интерфейс включить трансляцию у строки без расчёта не даёт вовсе: у
    такого товара остаток ничем не сверен, и на площадки уедет завышённое
    число. Через файл это делалось бы сразу пачкой — и молча."""
    import io
    from openpyxl import Workbook

    web_db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10))
    web_db.commit()

    wb = Workbook()
    ws = wb.active
    ws.append(["ID_1С", "Трансляция"])
    ws.append(["u1", "Да"])
    buf = io.BytesIO()
    wb.save(buf)

    r = logged_in_client.post("/products/import",
                              files={"file": ("t.xlsx", buf.getvalue())},
                              follow_redirects=False)

    assert r.status_code == 303
    product = web_db.query(Product).filter(Product.uid_1c == "u1").one()
    assert product.broadcast_enabled is False, "файл обошёл проверку страницы"


def test_excel_can_always_switch_broadcast_off(logged_in_client, web_db):
    """Выключение не ограничено ничем и никогда — снять с трансляции должно
    быть можно в любой момент."""
    import io
    from openpyxl import Workbook

    web_db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10,
                       broadcast_enabled=True))
    web_db.commit()

    wb = Workbook()
    ws = wb.active
    ws.append(["ID_1С", "Трансляция"])
    ws.append(["u1", "Нет"])
    buf = io.BytesIO()
    wb.save(buf)

    logged_in_client.post("/products/import", files={"file": ("t.xlsx", buf.getvalue())})

    assert web_db.query(Product).filter(Product.uid_1c == "u1").one().broadcast_enabled is False


# ------------------------------------------- поле «число» и его две кнопки

def test_the_number_field_is_visibly_tied_to_its_buttons():
    """21.09 оператор спросил про поле «число»: «за ним пустое поле, зачем оно».

    Подпись и подсказка у него были, а вот рамки не было: поле и две кнопки,
    которые его применяют, стояли в общем ряду с тем же зазором, что и всё
    остальное. Читалось как самостоятельное поле неизвестно для чего. Рамка
    отвечает на вопрос без единого слова: внутри неё — одно действие.
    """
    page = open("app/templates/products.html", encoding="utf-8").read()
    rule = page.split(".pr-group{", 1)[1].split("}", 1)[0]

    assert "border" in rule, "без рамки группа снова сольётся с рядом"


def test_the_label_points_at_the_buttons():
    """«число →» читается слева направо: число пять → Резерв (бронь) = ."""
    page = open("app/templates/products.html", encoding="utf-8").read()

    assert "число →" in page


def test_the_number_field_still_says_what_it_feeds():
    page = open("app/templates/products.html", encoding="utf-8").read()
    block = page.split('id="bulk-int"', 1)[1][:300]

    assert "title=" in block
    assert "бронь" in block or "факт" in block.lower()

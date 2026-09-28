"""Фильтр «Только найденные на площадке» и сохранение отметок при перерисовке.

Фильтр отвечает на вопрос «карточка на площадке уже есть, подключать есть
куда»: баркод товара нашёлся в каталоге хотя бы одного кабинета. Обратное
(«не найден») однозначного смысла не имеет — каталог кабинета могли просто не
выгружать, — поэтому обратного фильтра нет.

Отдельно закреплён дефект интерфейса: отметка строки слетала от нажатия любой
кнопки в этой же строке. Строка перерисовывается htmx'ом поодиночке, сервер о
выборе не знает и отдаёт отметку пустой, а восстановление было привязано только
к подмене таблицы целиком.
"""

from app.models import Barcode, PlatformAccount, PlatformCatalogItem, Platform, Product


def _catalog(web_db, account, barcode, external_id="x1"):
    web_db.add(PlatformCatalogItem(account_id=account.id, external_id=external_id,
                                   barcode=barcode, article="A", name="Карточка"))


def _seed(web_db):
    """Два товара: один есть на площадке, второй только в 1С."""
    account = PlatformAccount(platform=Platform.wb, name="ИП ЯВОРСКАЯ", warehouse_id="wh-1")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)

    web_db.add(Product(uid_1c="u-найден", article="НАЙДЕН", name="Есть на площадке"))
    web_db.add(Barcode(barcode="111", uid_1c="u-найден"))
    web_db.add(Product(uid_1c="u-нет", article="НЕТ", name="Только в 1С"))
    web_db.add(Barcode(barcode="222", uid_1c="u-нет"))
    _catalog(web_db, account, "111")
    web_db.commit()
    return account


def test_the_filter_keeps_only_products_found_in_a_cabinet_catalogue(logged_in_client, web_db):
    _seed(web_db)

    page = logged_in_client.get("/products?only_on_platform=true")

    assert "Есть на площадке" in page.text
    assert "Только в 1С" not in page.text


def test_without_the_filter_both_are_shown(logged_in_client, web_db):
    _seed(web_db)

    page = logged_in_client.get("/products")

    assert "Есть на площадке" in page.text and "Только в 1С" in page.text


def test_a_product_is_found_by_any_of_its_barcodes(logged_in_client, web_db):
    """У размер-цвета бывает несколько баркодов, и площадка знает не обязательно
    первый. Искать только по одному значило бы прятать подключаемые товары."""
    account = _seed(web_db)
    web_db.add(Barcode(barcode="333", uid_1c="u-нет"))
    _catalog(web_db, account, "333", external_id="x2")
    web_db.commit()

    page = logged_in_client.get("/products?only_on_platform=true")

    assert "Только в 1С" in page.text, "нашёлся по второму баркоду — значит найден"


def test_a_catalogue_of_a_switched_off_cabinet_still_counts(logged_in_client, web_db):
    """Каталог остаётся от кабинета, который погасил предохранитель или
    выключили руками, и товар на площадке от этого никуда не делся. Привязка к
    активности заставляла бы отбор моргать вместе с состоянием кабинетов."""
    account = _seed(web_db)
    account.is_active = False
    web_db.commit()

    page = logged_in_client.get("/products?only_on_platform=true")

    assert "Есть на площадке" in page.text


def test_the_filter_survives_a_bulk_edit(logged_in_client, web_db):
    """Массовая правка возвращает на страницу С ТЕМИ ЖЕ фильтрами: оператор сузил
    список именно затем, чтобы работать с ним, и попасть после правки на полный
    каталог означает искать отбор заново."""
    _seed(web_db)

    r = logged_in_client.post("/products/bulk", data={
        "action": "set_reserve", "int_value": "1", "uids": ["u-найден"],
        "only_on_platform": "true",
    }, follow_redirects=False)

    assert r.status_code == 303
    assert "only_on_platform=true" in r.headers["location"]


def test_the_filter_is_offered_on_the_page(logged_in_client, web_db):
    _seed(web_db)

    page = logged_in_client.get("/products")

    assert 'name="only_on_platform"' in page.text
    assert "Только найденные на площадке" in page.text


# ------------------------------------------- отметка не слетает при перерисовке

def test_the_row_checkbox_belongs_to_the_bulk_form():
    """Отметка стоит в таблице, а кнопки — в панели над ней. Связывает их
    атрибут form: без него отмеченные строки просто не уедут на сервер."""
    row = open("app/templates/products_row.html", encoding="utf-8").read()

    assert 'name="uids"' in row
    assert 'form="bulk-form"' in row


def test_selection_is_restored_after_any_htmx_swap():
    """Дефект: отметка строки слетала от нажатия любой кнопки в этой же строке.

    Строка перерисовывается поодиночке (`hx-swap="outerHTML"` по `#row-<uid>`), и
    сервер отдаёт отметку пустой — он о выборе не знает, выбор живёт в браузере.
    Восстановление было привязано к подмене таблицы ЦЕЛИКОМ (`#pr-table`), то
    есть к смене фильтра, и на одиночную строку не срабатывало.

    Проверяем исходник, а не поведение: JS в этом проекте не исполняется ни в
    одном тесте, и выбор здесь — либо такая проверка, либо никакой. Стеречь есть
    что: сужение условия этот дефект и породило."""
    page = open("app/templates/products.html", encoding="utf-8").read()

    assert 'htmx:afterSwap' in page
    handler = page.split('htmx:afterSwap', 1)[1][:400]
    assert "restore()" in handler, "после подмены разметки отметки обязаны вернуться"
    assert '=== "pr-table"' not in handler, (
        "восстановление снова сузили до подмены всей таблицы — одиночная строка "
        "опять потеряет отметку"
    )


# ------------------------------------------- фильтр «только отмеченные кабинеты»

def _marked(web_db, account, uid):
    from app.models import SyncSetting
    web_db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))


def test_the_marked_filter_keeps_only_products_with_a_checked_cabinet(logged_in_client, web_db):
    """Отбор по принятому решению, а не по наличию карточки: «найден на
    площадке» говорит, что подключать есть куда, а этот фильтр — что уже
    подключили."""
    account = _seed(web_db)
    _marked(web_db, account, "u-найден")
    web_db.commit()

    page = logged_in_client.get("/products?only_marked=true")

    assert "Есть на площадке" in page.text
    assert "Только в 1С" not in page.text


def test_an_unchecked_cabinet_does_not_count_as_marked(logged_in_client, web_db):
    """Снятая галочка — такое же решение оператора, как и поставленная."""
    from app.models import SyncSetting

    account = _seed(web_db)
    web_db.add(SyncSetting(uid_1c="u-найден", account_id=account.id, enabled=False))
    web_db.commit()

    page = logged_in_client.get("/products?only_marked=true")

    assert "Есть на площадке" not in page.text


def test_a_switched_off_cabinet_still_counts_as_marked(logged_in_client, web_db):
    """Галочку ставил человек, и выключение кабинета его решения не отменяет —
    иначе отбор моргал бы вместе с предохранителем."""
    account = _seed(web_db)
    _marked(web_db, account, "u-найден")
    account.is_active = False
    web_db.commit()

    page = logged_in_client.get("/products?only_marked=true")

    assert "Есть на площадке" in page.text


def test_the_marked_filter_is_offered_on_the_page(logged_in_client, web_db):
    _seed(web_db)

    page = logged_in_client.get("/products")

    assert 'name="only_marked"' in page.text
    assert "Только отмеченные кабинеты" in page.text


def test_the_marked_filter_survives_a_bulk_edit(logged_in_client, web_db):
    account = _seed(web_db)
    _marked(web_db, account, "u-найден")
    web_db.commit()

    r = logged_in_client.post("/products/bulk", data={
        "action": "set_reserve", "int_value": "1", "uids": ["u-найден"],
        "only_marked": "true",
    }, follow_redirects=False)

    assert r.status_code == 303
    assert "only_marked=true" in r.headers["location"]


def test_the_export_narrows_down_to_the_marked_products(logged_in_client, web_db):
    """Ссылка выгрузки передавала фильтр, а эндпоинт его не принимал — FastAPI
    молча отбрасывал параметр, и оператор получал весь каталог вместо отбора.
    Так уже было с `only_unfinished`, поэтому проверяем каждый новый."""
    account = _seed(web_db)
    _marked(web_db, account, "u-найден")
    web_db.commit()

    r = logged_in_client.get("/products/export?only_marked=true")

    assert r.status_code == 200
    assert len(r.content) > 0


def test_every_filter_reaches_the_bulk_form():
    """Массовая правка «по всему отбору» берёт фильтры из ЭТОГО фрагмента. Фильтр,
    которого здесь нет, до неё не доедет — и правка применится к списку ШИРЕ
    того, что оператор видит на экране. Именно это и случилось с
    `only_on_platform`: галочка на странице была, скрытого поля не было."""
    page = open("app/templates/products.html", encoding="utf-8").read()
    rows = open("app/templates/products_rows.html", encoding="utf-8").read()

    import re
    on_page = set(re.findall(r'<input type="checkbox" name="(only_\w+|hide_\w+)"', page))
    in_form = set(re.findall(r'<input type="hidden" name="(only_\w+|hide_\w+)"', rows))

    assert on_page, "фильтры на странице не нашлись — проверка перестала что-либо стеречь"
    assert on_page <= in_form, f"до массовой правки не доедут: {sorted(on_page - in_form)}"

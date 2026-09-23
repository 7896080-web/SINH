"""Массовая правка обязана изменить ровно то, что обещала страница.

23.09 на бою: отбор — 1739 строк, показано 500 (`FILTERED_LIMIT`), под таблицей
написано «Галочка в шапке выбирает весь текущий отбор — все 1739… Показано 500 —
остальные под отбор тоже попадают и тоже будут изменены, хотя их не видно».
Нажатие дало «Массовая правка (по отмеченным): изменено строк — 500».

Причин было две, и обе про одно: состояние отбора живёт в браузере, а уезжает на
сервер разметкой.

1. Галочка «весь отбор» стоит В ШАПКЕ ТАБЛИЦЫ, а таблицу htmx подменяет целиком
   при каждом изменении фильтра — и приходит она с сервера всегда снятой.
   Отметки строк из хранилища восстанавливались, эта галочка нет: она гасла,
   `all_filtered` на сервер не уходил, и правка шла по показанным строкам.
2. Отмеченных бывает БОЛЬШЕ, чем показано: отбор переживает смену фильтра.
   Счётчик показывал 849, а форма отправляла только те чекбоксы, что есть в
   разметке, то есть 500. Число на экране и число в запросе расходились.

Цена — не в неудобстве. «Изменено 500» выглядит как успех: оператор считает, что
настроил весь отбор, и про оставшиеся 1239 строк не узнает никогда — они просто
молчат.
"""

import re
from pathlib import Path

from app.models import Product
from app.routers.products import BULK_LIMIT, UIDS_CHUNK

PAGE = Path(__file__).resolve().parent.parent / "app" / "templates" / "products.html"
ROWS = Path(__file__).resolve().parent.parent / "app" / "templates" / "products_rows.html"


def _seed(web_db, count, reserve=0):
    for i in range(count):
        web_db.add(Product(uid_1c=f"u{i:05d}", article=f"A{i:05d}",
                           name="Куртка", stock_on_hand=5, reserve=reserve))
    web_db.commit()


# --------------------------------------------- что уходит на сервер, то и меняется

def test_marked_rows_that_are_not_on_screen_are_edited_too(logged_in_client, web_db):
    """Отмечено больше, чем помещается на страницу, — меняются все отмеченные.

    Это и есть та половина дефекта, что жила в браузере: сервер список принимал,
    но форма его не отправляла."""
    _seed(web_db, 700)

    uids = [f"u{i:05d}" for i in range(700)]
    logged_in_client.post("/products/bulk",
                          data={"action": "set_reserve", "int_value": "3", "uids": uids})

    changed = web_db.query(Product).filter(Product.reserve == 3).count()
    assert changed == 700


def test_the_whole_filter_is_edited_even_though_only_a_page_is_shown(logged_in_client, web_db):
    """`all_filtered` правит весь отбор, а не показанные строки.

    Именно это обещает текст под таблицей, и именно это не срабатывало, когда
    галочка в шапке гасла при подмене разметки."""
    _seed(web_db, 700)

    logged_in_client.post("/products/bulk",
                          data={"action": "set_reserve", "int_value": "4",
                                "all_filtered": "true"})

    assert web_db.query(Product).filter(Product.reserve == 4).count() == 700


def test_the_filter_is_respected_when_editing_the_whole_selection(logged_in_client, web_db):
    """«Весь отбор» — это отбор, а не каталог: фильтр обязан сужать правку."""
    _seed(web_db, 5)
    web_db.add(Product(uid_1c="other", article="Z99", name="Ботинки", stock_on_hand=5))
    web_db.commit()

    logged_in_client.post("/products/bulk",
                          data={"action": "set_reserve", "int_value": "7",
                                "all_filtered": "true", "q": "Куртка"})

    assert web_db.query(Product).filter(Product.reserve == 7).count() == 5
    assert web_db.query(Product).filter(Product.uid_1c == "other").first().reserve == 0


# ------------------------------------------------------------------ пределы

def test_a_list_longer_than_the_limit_is_refused_and_nothing_is_changed(
        logged_in_client, web_db, monkeypatch):
    """Предел стоит на ОБОИХ путях, а не на одном.

    У правки по всему отбору он был с самого начала, у правки по отметкам — нет,
    и список любой длины уходил прямо в `IN (...)`. Предел подменяется, как и в
    близнеце про правку по отбору: настоящие двадцать тысяч полей в запросе
    проверяли бы скорость разбора формы, а не то, ради чего написан тест."""
    import app.routers.products as products_router

    monkeypatch.setattr(products_router, "BULK_LIMIT", 2)
    _seed(web_db, 3)

    page = logged_in_client.post("/products/bulk",
                                 data={"action": "set_reserve", "int_value": "9",
                                       "uids": ["u00000", "u00001", "u00002"]},
                                 follow_redirects=True)

    assert "больше предела" in page.text
    web_db.expire_all()
    assert web_db.query(Product).filter(Product.reserve == 9).count() == 0


def test_a_list_far_past_the_sqlite_parameter_limit_still_works(
        logged_in_client, web_db, monkeypatch):
    """Длинный список отметок разбирается порциями и доходит до конца.

    Порция подменяется на заведомо маленькую: на машине разработки SQLite новый
    (32766 параметров), и полторы тысячи отметок прошли бы одним запросом даже
    без порций — то есть тест проверял бы не то, ради чего написан."""
    import app.routers.products as products_router

    monkeypatch.setattr(products_router, "UIDS_CHUNK", 7)
    _seed(web_db, 60)

    uids = [f"u{i:05d}" for i in range(60)]
    logged_in_client.post("/products/bulk",
                          data={"action": "set_reserve", "int_value": "2", "uids": uids})

    web_db.expire_all()
    assert web_db.query(Product).filter(Product.reserve == 2).count() == 60


def test_the_chunk_is_small_enough_for_the_oldest_sqlite_we_may_meet():
    """Порция — предмет правки, а не деталь реализации.

    Предел в 999 параметров живёт в сборках до 3.32, и на машине разработки его
    не увидеть: там сборка новая. Единственное, что защищает от него, — это
    число, поэтому оно и закреплено."""
    assert UIDS_CHUNK <= 900, "порция больше не переживёт SQLite до 3.32"


# -------------------------------------------------- то, что живёт в браузере

def test_the_whole_selection_survives_the_table_being_replaced():
    """Флаг «весь отбор» хранится рядом с самим отбором и восстанавливается.

    Без этого он жил ровно до первого изменения фильтра — а фильтром тут и
    пользуются, прежде чем править."""
    page = PAGE.read_text(encoding="utf-8")

    assert "pr-all-filtered" in page, "флаг «весь отбор» больше не запоминается"
    assert "saveWhole" in page and "loadWhole" in page
    # Восстановление идёт в том же месте, что и восстановление отметок строк:
    # именно оно зовётся на каждую подмену разметки (htmx:afterSwap).
    restore = re.search(r"function restore\(\) \{(.+?)\n    \}", page, re.S)
    assert restore is not None, "функция restore() исчезла — проверка ослепла"
    assert "paint()" in restore.group(1)
    paint = re.search(r"function paint\(\) \{(.+?)\n    \}", page, re.S)
    assert "all.checked = whole" in paint.group(1), \
        "галочка в шапке больше не восстанавливается из хранилища"


def test_selected_rows_missing_from_the_page_are_added_to_the_request():
    """Отмеченная строка, которой нет на экране, дописывается скрытым полем.

    Иначе счётчик показывает 849, а уезжает 500 — и расхождение видно только по
    числу «изменено строк», которое выглядит как успех."""
    page = PAGE.read_text(encoding="utf-8")

    submit = re.search(r'addEventListener\("submit", function \(\) \{(.+?)\n    \}\);',
                       page, re.S)
    assert submit is not None, "обработчик отправки формы исчез"
    body = submit.group(1)
    assert 'extra.name = "uids"' in body, "недостающие отметки больше не дописываются"
    assert "if (whole) { return; }" in body, \
        "при «весь отбор» список отметок отправлять не нужно — его собирает сервер"
    assert "data-extra-uid" in body and ".remove()" in body, \
        "дописанные поля обязаны сниматься перед следующей отправкой"


def test_the_counter_names_the_number_of_rows_in_the_selection():
    """«Весь отбор» без числа не говорит, КАКОЙ отбор он сейчас значит.

    Флаг переживает смену фильтра, и число — единственное, по чему видно, что
    под галочкой стало другое множество."""
    rows = ROWS.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")

    assert 'id="pr-total"' in rows and "data-total" in rows, \
        "фрагмент таблицы больше не отдаёт число строк отбора"
    assert "totalText()" in page
    paint = re.search(r"function paint\(\) \{(.+?)\n    \}", page, re.S).group(1)
    assert "весь отбор — " in paint, "счётчик перестал называть число"


def test_unchecking_a_row_drops_the_whole_selection_visibly():
    """Снятая галочка строки при включённом «весь отбор» — противоречие.

    Оставить «весь отбор» значило бы изменить строку, которую человек только что
    исключил. Переходим на отметки — и счётчик тут же меняет число, так что
    сужение видно в момент, когда происходит."""
    page = PAGE.read_text(encoding="utf-8")

    assert "if (!t.checked && whole) { whole = false; saveWhole(whole); }" in page


# ---------------------------------------- сообщение обязано доехать до страницы

def test_the_no_cabinets_banner_does_not_eat_the_answer_to_an_action(
        logged_in_client, web_db, monkeypatch):
    """Постоянное состояние установки не вытесняет ответ на действие.

    «Пока нет ни одного активного кабинета» ехало тем же флешем, что и итоги
    правки, и ставилось на КАЖДОЙ загрузке `/products` — то есть затирало то,
    ради чего человек сюда и вернулся. Видно это только на свежей установке, где
    кабинета ещё нет: ровно тогда, когда идёт настройка и сообщения нужнее всего.
    """
    import app.routers.products as products_router

    monkeypatch.setattr(products_router, "BULK_LIMIT", 2)
    _seed(web_db, 3)

    page = logged_in_client.post("/products/bulk",
                                 data={"action": "set_reserve", "int_value": "9",
                                       "uids": ["u00000", "u00001", "u00002"]},
                                 follow_redirects=True)

    assert "больше предела" in page.text
    # Следующая загрузка уже без своего сообщения — баннер возвращается.
    assert "нет ни одного активного кабинета" in logged_in_client.get("/products").text


def test_the_confirmation_says_the_same_number_the_counter_does():
    """Вопрос перед отправкой нуля берёт текст счётчика, а не считает сам.

    Два счёта одного и того же однажды разошлись бы, и разошлись бы именно там,
    где цена ошибки — обнулённые живые карточки."""
    page = PAGE.read_text(encoding="utf-8")

    assert 'var count = document.getElementById("pr-count").textContent;' in page

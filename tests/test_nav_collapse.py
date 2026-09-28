"""Сворачивание левого меню.

Меню занимает 220 пикселей на каждой странице, а таблицы здесь широкие —
товары с девятью колонками, расхождения, каталоги кабинетов. В свёрнутом виде
меню оставляет узкую полосу с иконками, и ширину забирает основная область.

JS в этом проекте не исполняется ни в одном тесте, поэтому проверяется
разметка и стили — как и для восстановления отметок на странице товаров.
Стеречь тут есть что: половина требований (иконки, подписи, запоминание) легко
теряется при любой правке шапки.
"""

import re

BASE = "app/templates/base.html"
CSS = "app/static/style.css"


def _base():
    """Исходник шаблона — для того, что не зависит от конкретной страницы."""
    return open(BASE, encoding="utf-8").read()


def _css():
    return open(CSS, encoding="utf-8").read()


def _rendered(client):
    """Отрисованная страница: пункты меню собираются циклом, и в шаблоне их
    физически один. Смотреть надо на то, что уходит в браузер."""
    return client.get("/diagnostics").text


def test_every_menu_item_has_an_icon(logged_in_client):
    """В свёрнутом меню подписи не видно — без иконки пункт станет пустым
    местом, и попасть в нужный раздел можно будет только угадыванием."""
    page = _rendered(logged_in_client)

    items = re.findall(r'<a class="nav__item[^>]*>\s*<svg class="nav__icon"[^>]*>'
                       r'\s*<use href="#([a-z-]+)"', page, re.S)

    assert len(items) >= 9, f"иконки нашлись не у всех пунктов: {items}"
    assert len(set(items)) == len(items), "две страницы с одной иконкой неразличимы"


def test_icons_are_local_and_not_a_font(logged_in_client):
    """Боевой сервер живёт без интернета. Внешняя иконочная библиотека дала бы
    девять одинаковых пустых квадратов — и именно в свёрнутом виде, где кроме
    них ничего нет."""
    page = _base()

    assert "<symbol id=" in page, "иконки обязаны лежать своим спрайтом"
    assert "cdn" not in page.split("<body")[0].lower() or "htmx.min.js" in page


def test_every_item_keeps_a_title_for_the_collapsed_state(logged_in_client):
    """Подпись в `title` — единственное, что объясняет иконку, когда меню
    свёрнуто."""
    page = _rendered(logged_in_client)
    nav = page.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]

    links = re.findall(r'<a class="nav__item.*?</a>', nav, re.S)

    assert links, "пункты меню не нашлись — проверка перестала что-либо стеречь"
    for link in links:
        assert "title=" in link, f"пункт без подписи для свёрнутого меню: {link[:80]}"


def test_the_toggle_button_exists(logged_in_client):
    page = _rendered(logged_in_client)

    assert 'id="nav-toggle"' in page
    assert "Свернуть меню" in page


def test_the_state_is_restored_before_the_first_paint():
    """Страницы здесь обычные, не SPA: каждая перерисовывается целиком. Восстанови
    состояние в конце body — и меню будет прыгать шириной на КАЖДОМ переходе."""
    page = _base()
    head = page.split("</head>", 1)[0]

    assert "nav-collapsed" in head, "состояние обязано восстанавливаться в <head>"
    assert "localStorage" in head


def test_a_blocked_local_storage_does_not_break_the_page():
    """В приватном окне и при запрете на хранение обращение к localStorage
    бросает исключение. Непойманное — оно убьёт весь скрипт в <head>."""
    page = _base()
    head = page.split("</head>", 1)[0]

    assert "try {" in head and "catch" in head


def test_the_collapsed_menu_is_narrow_and_hides_labels():
    css = _css()

    assert ".nav-collapsed .nav {" in css
    width = re.search(r"\.nav-collapsed \.nav \{[^}]*width:\s*(\d+)px", css)
    assert width and int(width.group(1)) <= 80, "свёрнутое меню должно быть узким"
    assert ".nav-collapsed .nav__label" in css and "display: none" in css


def test_the_main_area_takes_the_freed_width():
    """Ширину забирает сам .main (flex: 1) — если у него появится фиксированная
    ширина, сворачивание меню оставит справа пустую полосу."""
    css = _css()
    main = css.split(".main {", 1)[1].split("}", 1)[0]

    assert "flex: 1" in main


def test_the_active_page_stays_visible_when_collapsed():
    """Без подписей активный пункт — единственный ориентир «где я». Полоска
    слева в узком меню не помещается, поэтому она обязана переехать."""
    css = _css()

    assert ".nav-collapsed .nav__item.active" in css
    block = css.split(".nav-collapsed .nav__item.active", 1)[1].split("}", 1)[0]
    assert "border-bottom-color" in block


def test_the_narrow_screen_layout_is_left_alone():
    """На узком экране меню горизонтальное и ширины не занимает: сворачивать
    там нечего, а узкая полоса с иконками поверх страницы только мешала бы."""
    css = _css()
    media = css.split("@media (max-width: 1100px)", 1)[1]

    assert ".nav__toggle { display: none; }" in media
    assert ".nav-collapsed .nav__label { display: inline; }" in media


def test_the_menu_still_renders_on_a_real_page(logged_in_client):
    page = logged_in_client.get("/diagnostics")

    assert page.status_code == 200
    assert 'id="nav-toggle"' in page.text
    assert 'href="/products"' in page.text
    assert "Товары и остатки" in page.text

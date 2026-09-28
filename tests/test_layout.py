"""Вёрстка: страница целиком не уезжает вбок, таблицы прокручиваются внутри.

Жалоба была прямая — «ничего не обрезалось, ничего не терялось». Причина
оказалась одна и общая для всех страниц: `.main` — flex-элемент, а у такого по
умолчанию `min-width: auto`, то есть он НЕ МОЖЕТ стать уже своего содержимого.
Широкая таблица распирала `.main`, следом `.shell`, и горизонтально ехала вся
страница вместе с боковым меню — оно уползало за левый край.

Теперь `.main` имеет `min-width: 0` и остаётся в пределах экрана, а прокручивается
сама таблица внутри `.table-scroll`. Проверяем оба конца: правило есть в стилях,
и ни одна страница не отдаёт таблицу без обёртки.
"""
import pathlib

import pytest

PAGES = ["/products", "/mapping", "/diagnostics", "/anomalies", "/api-keys",
         "/testing", "/stock-on-date", "/platform-matching"]

CSS = pathlib.Path("app/static/style.css").read_text(encoding="utf-8")


@pytest.mark.parametrize("url", PAGES)
def test_every_page_opens(logged_in_client, url):
    assert logged_in_client.get(url).status_code == 200


@pytest.mark.parametrize("url", PAGES)
def test_no_table_escapes_the_scroll_wrapper(logged_in_client, url):
    """Незавёрнутая таблица возвращает прежнее поведение: страница едет вбок
    целиком. Проверять глазами каждую новую таблицу никто не станет."""
    body = logged_in_client.get(url).text
    if "<table" in body:
        assert "table-scroll" in body, f"{url}: таблица без обёртки прокрутки"


def test_main_can_shrink_below_its_content():
    """Само правило. Без него всё остальное бессмысленно: обёртка прокрутки не
    поможет, если контейнер по-прежнему распирает страницу."""
    main = CSS.split(".main {", 1)[1].split("}", 1)[0]

    assert "min-width: 0" in main


def test_narrow_screens_get_a_horizontal_menu():
    """Меню в 220px на ноутбуке 13\" съедало треть ширины у таблиц."""
    assert "@media (max-width: 1100px)" in CSS
    assert ".shell { flex-direction: column; }" in CSS

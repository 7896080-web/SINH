"""Меню: пункт обязан вести на живую страницу, страница — подсвечивать свой пункт.

Повод конкретный. «Есть на складе — нет на площадке» существовала с самого
начала, работала и была нужна, но в меню не входила НИ РАЗУ: попасть на неё
можно было только кнопкой в шапке «Расхождений». Пока пунктов было мало, это
сходило с рук; когда рядом появился пункт «Расхождения со складом», страницу
стали искать в меню — и не нашли. Для человека страница, к которой ведёт
один-единственный элемент на другой странице, равна отсутствующей.

Отсюда две проверки, и вторая не менее важна первой. Пункт, ведущий в никуда,
— немой отказ: нажал, получил ошибку или чужую страницу. Страница, не
подсветившая свой пункт, — отказ того же рода, только тише: меню утверждает,
что ты в другом разделе, и человек считает, что перешёл не туда. Ровно это тут
и было — `/report/missing-cards` подсвечивала «Расхождения».
"""

import re

import pytest


def _nav(client) -> str:
    """Меню отрисованной страницы, а не исходник шаблона.

    Смотреть надо на то, что уходит в браузер: `active` проставляет обработчик,
    и по шаблону его не видно вовсе.
    """
    page = client.get("/diagnostics").text
    return page.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]


def _hrefs(nav: str) -> list[str]:
    return re.findall(r'<a class="nav__item[^>]*href="([^"]+)"', nav)


def test_the_menu_lists_the_pages_we_think_it_lists(logged_in_client):
    """Страховка самой проверки: перестань разбор находить пункты — обе
    проверки ниже замолчат, ничего при этом не стерегя."""
    hrefs = _hrefs(_nav(logged_in_client))

    assert len(hrefs) >= 10, f"пункты меню не нашлись: {hrefs}"
    assert len(set(hrefs)) == len(hrefs), "один адрес двумя пунктами"


def test_the_missing_cards_page_is_in_the_menu(logged_in_client):
    """Та самая страница, которую искали в меню и не находили."""
    assert "/report/missing-cards" in _hrefs(_nav(logged_in_client))


def test_every_menu_item_leads_to_a_live_page(logged_in_client):
    """Пункт, ведущий в никуда, человек нажмёт ровно один раз."""
    dead = {href: logged_in_client.get(href).status_code
            for href in _hrefs(_nav(logged_in_client))}

    assert {h: c for h, c in dead.items() if c != 200} == {}


def test_the_open_page_highlights_its_own_item(logged_in_client):
    """Меню обязано показывать, где человек находится.

    Подсветка чужого пункта — не косметика: по ней решают, туда ли перешли, и
    расхождение читается как «ссылка не сработала».
    """
    wrong = {}
    for href in _hrefs(_nav(logged_in_client)):
        nav = logged_in_client.get(href).text
        nav = nav.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]
        active = re.findall(r'<a class="nav__item active"[^>]*href="([^"]+)"', nav)
        if active != [href]:
            wrong[href] = active
    assert wrong == {}, f"страница подсветила не свой пункт: {wrong}"

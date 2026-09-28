"""Складская учётная запись видит ТОЛЬКО возвраты. Запрет по умолчанию.

Это не про опрятность интерфейса. На остальных страницах кнопки, которые одним
нажатием двигают боевые остатки по всему каталогу: массовая правка на «Товарах»
(нажатие по отбору в 1739 строк уже случалось), «Переотправить остаток» на
«Диагностике», токены площадок на «API-ключах», отправка на живую площадку с
«Тестирования», переподвязка баркодов на «Мэппинге».

Главная проверка здесь — предпоследняя: **ни один адрес приложения не открыт
складу случайно**. Она и стережёт будущее: страницу заведут через полгода, про
роли не вспомнят, и единственное, что не даст ей открыться, — запрет по
умолчанию плюс этот тест.
"""

import pytest

from app.models import User, UserRole
from app.security import hash_password


@pytest.fixture()
def warehouse_client(client, web_db):
    web_db.add(User(username="sklad", password_hash=hash_password("secret123"),
                    role=UserRole.warehouse))
    web_db.commit()
    client.post("/login", data={"username": "sklad", "password": "secret123"})
    return client


def test_the_warehouse_sees_returns(warehouse_client):
    assert warehouse_client.get("/returns").status_code == 200


@pytest.mark.parametrize("url", ["/products", "/api-keys", "/diagnostics", "/mapping",
                                 "/testing", "/discrepancies", "/report",
                                 "/notifications", "/anomalies"])
def test_the_warehouse_is_refused_everywhere_else(warehouse_client, url):
    """Отказ, а не редирект: молча увести человека на другую страницу значит
    оставить его гадать, нажалась ли ссылка."""
    r = warehouse_client.get(url, follow_redirects=False)

    assert r.status_code == 403, f"{url} открылся складу"


def test_the_refusal_says_what_to_do(warehouse_client):
    page = warehouse_client.get("/products").text

    assert "Этой страницы у вашей учётной записи нет" in page
    assert "/returns" in page, "отказ обязан давать выход, а не тупик"


def test_no_page_of_the_app_is_open_to_the_warehouse_by_accident(warehouse_client):
    """ГЛАВНАЯ проверка: перебираем ВСЕ адреса приложения.

    Разрешение перечисляется, запрет — нет. Значит страница, заведённая через
    полгода без единой мысли о ролях, обязана оказаться закрытой; если она
    открылась, список разрешённого расширили молча, и узнать об этом больше
    неоткуда.
    """
    from app.main import app
    from app import access

    opened = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "GET" not in methods or "{" in path:      # параметрические — отдельно
            continue
        if access.is_public(path) or access.allowed("warehouse", path):
            continue
        r = warehouse_client.get(path, follow_redirects=False)
        if r.status_code == 403:
            continue
        # Указатель, а не страница: корень сам спрашивает роль и ведёт человека
        # на ЕГО страницу. Признаём его по этому свойству, а не по имени —
        # начни он вести куда-то ещё, проверка обязана упасть.
        if r.status_code == 303 and r.headers.get("location") == access.home_for("warehouse"):
            continue
        opened.append((path, r.status_code))

    assert opened == [], f"складу открылись чужие страницы: {opened}"


def test_a_lookalike_prefix_is_not_the_returns_section(warehouse_client):
    """`/returns` не должен открывать `/returnsomething`.

    Голый `startswith` открыл бы складу любую будущую страницу, чьё имя
    начинается с тех же букв, — а такие заводят не думая.
    """
    from app import access

    assert access.allowed("warehouse", "/returns")
    assert access.allowed("warehouse", "/returns/list")
    assert not access.allowed("warehouse", "/returnsecret")


def test_the_menu_shows_only_what_opens(warehouse_client):
    """Прятать пункт — не защита, а вежливость. Но предлагать то, что не
    откроется, — прямой обман."""
    nav = warehouse_client.get("/returns").text.split('<nav class="nav">', 1)[1].split("</nav>", 1)[0]

    assert "/returns" in nav
    for foreign in ("/products", "/api-keys", "/diagnostics", "/mapping", "/testing"):
        assert f'href="{foreign}"' not in nav, f"меню предлагает {foreign}"


def test_login_sends_the_warehouse_to_its_own_page(client, web_db):
    """Отправь мы кладовщика на общий `/mapping`, он получил бы отказ сразу
    после успешного входа и решил бы, что учётная запись не работает."""
    web_db.add(User(username="sklad", password_hash=hash_password("secret123"),
                    role=UserRole.warehouse))
    web_db.commit()

    r = client.post("/login", data={"username": "sklad", "password": "secret123"},
                    follow_redirects=False)

    assert r.headers["location"] == "/returns"


def test_the_admin_keeps_everything(logged_in_client):
    """Роли не должны отнять доступ у тех, кто работал до их появления."""
    assert logged_in_client.get("/products").status_code == 200
    assert logged_in_client.get("/returns").status_code == 200


def test_an_existing_user_is_an_admin(web_db):
    """Миграция проставляет `admin` умолчанием: молча отнять доступ у живого
    человека хуже, чем дать лишний."""
    user = User(username="старый", password_hash=hash_password("x"))
    web_db.add(user)
    web_db.commit()

    assert user.role is UserRole.admin

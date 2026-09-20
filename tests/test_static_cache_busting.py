"""Стили не должны приезжать из вчерашнего кэша.

20.09 после наката оператор открыл страницу и увидел разъехавшуюся вёрстку:
разметка пришла новая, а `style.css` браузер взял из кэша — вчерашний. Иконки
меню без своих правил растянулись во всю ширину, сворачивание не сработало
вовсе. Само по себе лечится `Ctrl+F5`, но рассчитывать на то, что каждый
оператор после каждого наката догадается это сделать, нельзя: он увидит
сломанную страницу и решит, что сломали мы.

Поэтому в адресе статики стоит версия, считаемая из времени правки самих
файлов. Накат их переписывает — адрес меняется — браузер идёт за новым.
"""

import importlib

from app import templating


def test_the_stylesheet_link_carries_a_version(logged_in_client):
    page = logged_in_client.get("/diagnostics").text

    assert "/static/style.css?v=" in page, "без версии браузер возьмёт файл из кэша"


def test_htmx_carries_a_version_too(logged_in_client):
    """htmx меняется реже, но его протухшая версия страшнее: интерактив
    перестанет работать МОЛЧА — галочки не сохраняются, ошибок нет."""
    page = logged_in_client.get("/diagnostics").text

    assert "/static/htmx.min.js?v=" in page


def test_the_version_changes_when_the_file_changes(tmp_path, monkeypatch):
    """Метка обязана зависеть от самих файлов. Прибитая константа означала бы
    ровно ту же поломку, только вспомнить о ней надо было бы вручную при каждом
    накате — то есть однажды забыть."""
    first = templating._static_version()

    css = tmp_path / "style.css"
    css.write_text("body{}")
    (tmp_path / "htmx.min.js").write_text("//")
    monkeypatch.setattr(templating, "STATIC_DIR", str(tmp_path))
    changed = templating._static_version()

    assert changed != first
    import os
    os.utime(css, (0, 0))
    assert templating._static_version() != changed


def test_a_missing_file_does_not_break_the_app(tmp_path, monkeypatch):
    """Без шаблонизатора не поднимется ВСЁ приложение. Отсутствующий файл
    статики — повод отдать версию без него, а не уронить сервер."""
    monkeypatch.setattr(templating, "STATIC_DIR", str(tmp_path))

    assert templating._static_version()


def test_the_version_is_stable_while_files_are_untouched():
    """Если бы метка менялась сама собой, кэш перестал бы работать вовсе и
    каждая страница тянула бы стили заново."""
    assert templating._static_version() == templating._static_version()


def test_every_page_uses_the_shared_templates():
    """Шаблонизатор один на приложение: глобальные функции (`static_url`)
    объявлены в нём. Свой `Jinja2Templates` в роутере снова остался бы без них,
    и страница вернулась бы к ссылке без версии — молча."""
    import pathlib

    own = [p.name for p in pathlib.Path("app/routers").glob("*.py")
           if "Jinja2Templates(directory=" in p.read_text()]

    assert own == [], f"свой шаблонизатор остался в: {own}"


def test_static_files_are_still_served(logged_in_client):
    """Версия — это query-параметр, а не часть пути: файл обязан отдаваться и
    по адресу с ней, и без неё."""
    plain = logged_in_client.get("/static/style.css")
    versioned = logged_in_client.get(f"/static/style.css?v={templating.STATIC_VERSION}")

    assert plain.status_code == 200
    assert versioned.status_code == 200
    assert plain.content == versioned.content


def test_the_login_page_gets_the_version_too(client):
    """Страница входа не наследует базовый шаблон и легко остаётся забытой: её
    открывают первой, и именно на ней протухшие стили создают впечатление
    сломанной системы ещё до входа."""
    page = client.get("/login").text

    assert "/static/style.css?v=" in page


def test_no_template_links_static_without_a_version():
    """Любой шаблон, сославшийся на статику напрямую, вернёт проблему обратно —
    молча и только на своей странице."""
    import pathlib
    import re

    bad = []
    for p in pathlib.Path("app/templates").glob("*.html"):
        for line in p.read_text(encoding="utf-8").splitlines():
            if re.search(r'(href|src)="/static/', line):
                bad.append(f"{p.name}: {line.strip()[:70]}")

    assert bad == [], f"ссылки на статику без версии: {bad}"

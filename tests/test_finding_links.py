"""Ссылка «Разобрать →» обязана приводить К НАХОДКЕ, а не на страницу.

21.09 на бою: оператор жмёт «Разобрать» у находки — и «обновляется страница
диагностики, и всё». Так и было. Находки живут НА «Диагностике» же, а ссылка
вела на `/diagnostics` целиком: с той же страницы на ту же страницу. Даже с
другой страницы это значило «вот тебе длинный список карточек, ищи сам»: что
именно разбирать — задание 1С, кабинет, воркер — ссылка не говорила.

Теперь каждая находка ведёт на свою карточку якорем, а карточка подсвечивается
по `:target` — иначе прокрутка посреди длинной страницы снова выглядит как
«ничего не произошло».
"""

import pathlib
import re

import app.report as report


def _links() -> list[str]:
    """Все ссылки находок — из исходника: собрать их вызовом нельзя, для каждой
    нужно своё состояние базы, а проверяем мы адрес, а не условие находки."""
    source = pathlib.Path("app/report.py").read_text(encoding="utf-8")
    return re.findall(r'link="([^"]+)"', source)


def test_no_finding_points_at_the_diagnostics_page_as_a_whole():
    bare = [link for link in _links() if link == "/diagnostics"]

    assert bare == [], ("находка ведёт на «Диагностику» целиком — а находки там же "
                        "и показаны, то есть ссылка никуда не ведёт")


def test_every_diagnostics_link_names_a_card_that_exists():
    """Якорь на несуществующий id — тот же «ничего не произошло», только молча."""
    page = pathlib.Path("app/templates/diagnostics.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([a-z-]+)"', page))

    missing = [link for link in _links()
               if link.startswith("/diagnostics#") and link.split("#", 1)[1] not in ids]

    assert missing == [], f"якорь есть, карточки нет: {missing}"


def test_the_target_card_is_highlighted():
    """Страница длинная. Без подсветки переход — это прыжок в неизвестность."""
    css = pathlib.Path("app/static/style.css").read_text(encoding="utf-8")

    assert ".card:target" in css


def test_every_link_is_absolute():
    """Относительный адрес в находке зависел бы от того, с какой страницы её
    открыли, — а показывают её и на «Расхождениях», и на «Диагностике»."""
    bad = [link for link in _links() if not link.startswith("/")]

    assert bad == [], f"ссылка не от корня: {bad}"


def test_the_diagnostics_page_serves_those_anchors(logged_in_client):
    page = logged_in_client.get("/diagnostics").text

    assert 'id="stuck-tasks"' in page
    assert 'id="accounts"' in page
    assert 'id="workers"' in page
    assert 'id="reconciliation"' in page

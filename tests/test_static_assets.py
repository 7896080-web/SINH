"""Админка не зависит от внешних сайтов.

htmx грузился с cdnjs.cloudflare.com. Вся интерактивность страниц — галочки
кабинетов, поля порога и брони, перерисовка строк — держится на нём, а значит
зависела от того, доступен ли Cloudflare из браузера оператора. Если нет,
страница открывалась как обычно, но переставала работать МОЛЧА: оператор
щёлкает галочку, ничего не происходит, ошибки нет. Для системы, управляющей
остатками на площадках, тихий отказ хуже явного.

Теперь файл лежит рядом и отдаётся самим приложением.
"""
import pathlib

BASE = pathlib.Path("app/templates/base.html").read_text(encoding="utf-8")
HTMX = pathlib.Path("app/static/htmx.min.js")


def test_htmx_is_served_locally():
    assert '<script src="/static/htmx.min.js"></script>' in BASE


def test_no_external_scripts_or_styles_at_all():
    """Не только htmx: любая внешняя ссылка в шапке вернула бы ту же зависимость."""
    for marker in ("http://", "https://"):
        head = BASE.split("</head>", 1)[0]
        assert marker not in head, f"в шапке страницы осталась внешняя ссылка ({marker})"


def test_the_file_is_really_there():
    """Ссылка без файла — та же мёртвая страница, только теперь по своей вине."""
    assert HTMX.exists(), "app/static/htmx.min.js отсутствует"
    assert HTMX.stat().st_size > 40000, "файл подозрительно мал — не тот файл?"


def test_it_is_actually_htmx():
    body = HTMX.read_text(encoding="utf-8", errors="replace")

    assert "htmx" in body[:400].lower() or "hx-" in body[:4000]


def test_a_missing_htmx_is_announced_not_silent():
    """Если файл всё же потеряется при обновлении, страница обязана сказать об
    этом вслух, а не притворяться рабочей."""
    assert "htmx не загрузился" in BASE
    assert 'typeof window.htmx !== "undefined"' in BASE


def test_the_page_serves_the_file(logged_in_client):
    r = logged_in_client.get("/static/htmx.min.js")

    assert r.status_code == 200
    assert len(r.content) > 40000

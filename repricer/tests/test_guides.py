"""Инструкции на страницах: «что делаем, что и куда уходит». Один источник
(templates/_guides.html) на страницу и на «Справку»."""
from tests import factories as F


def _setup(db):
    a = F.account(db, "wb", "ИП 1")
    F.rule(db, a)
    F.manual_rate(db)
    return a


def test_every_price_page_has_its_guide(client, db):
    a = _setup(db)
    pages = {
        "/sku-prices": "«Цены товаров»: основной путь",
        "/prices?view=rules": "«Правила площадок»",
        f"/prices?view=products&account_id={a.id}": "«Текущие и ручные цены»",
        "/prices?view=proposals": "«Пересчёт всех цен»",
        "/prices?view=log": "«Журнал отправок»",
        "/prices?view=compare": "«Сравнение площадок»",
        "/rate": "«Курс $»",
        "/attention": "«Внимание»",
    }
    for url, title in pages.items():
        html = client.get(url).text
        assert 'class="guide"' in html and title in html, url
        assert "Уходит на площадки?" in html or url in ("/prices?view=log", "/prices?view=compare", "/attention"), url


def test_help_has_flow_and_all_guides(client, db):
    _setup(db)
    html = client.get("/help/prices").text
    assert "Как цена попадает на площадку" in html
    assert html.count('class="guide"') == 8


def test_guides_name_buttons_that_exist(client, db):
    """Инструкция обязана называть кнопки, которые страница действительно даёт:
    посоветуй она кнопку, которой нет, человек ищет её и решает, что страница сломана."""
    a = _setup(db)
    checks = {
        "/sku-prices": ["Установить", "2. Передать на площадки", "Снять отметки", "Экспорт отбора в Excel"],
        "/prices?view=rules": ["Что будет, если", "Что вернётся по диапазонам", "Вернуть цены по диапазонам"],
        f"/prices?view=products&account_id={a.id}": ["Загрузить текущие цены со всех площадок", "Применить"],
        "/prices?view=proposals": ["Рассчитать"],
        "/rate": ["Обновить у ЦБ"],
    }
    for url, buttons in checks.items():
        html = client.get(url).text
        for b in buttons:
            assert html.count(b) >= 2 or b == "Применить", (url, b)    # в инструкции и на кнопке

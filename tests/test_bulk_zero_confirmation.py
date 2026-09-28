"""Массовые действия, отправляющие на площадки ноль, спрашивают подтверждение.

«Трансляция выкл» и «Снять кабинет» не просто гасят строки: по каждой паре, куда
мы реально отправляли непустой остаток, уходит ОТЗЫВ — ноль на живую карточку,
и она перестаёт продавать. Сразу по всему отбору.

Отбор при этом живёт в браузере (sessionStorage) и переживает смену фильтра, так
что отмеченные строки могут быть уже не видны на экране — поэтому в вопросе стоит
ЧИСЛО, а не просто «у отмеченных». Включение трансляции подтверждения не требует:
оно ничего не обнуляет, а проходит через те же ворота, что и галочка в строке.
"""
import re

from app.models import Product


def _page(client, db):
    db.add(Product(uid_1c="u1", article="A-1", name="Товар", stock_on_hand=5))
    db.commit()
    return client.get("/products").text


def _button(page: str, action: str) -> str:
    match = re.search(r'<button[^>]*value="%s"[^>]*>' % action, page, re.S)
    assert match, f"кнопка {action} не найдена"
    return match.group(0)


def test_switching_broadcast_off_asks_first(logged_in_client, web_db):
    page = _page(logged_in_client, web_db)

    button = _button(page, "broadcast_off")

    assert "data-confirm" in button
    assert "ноль" in button


def test_removing_an_account_asks_first(logged_in_client, web_db):
    page = _page(logged_in_client, web_db)

    assert "data-confirm" in _button(page, "cabinet_off")


def test_switching_broadcast_on_does_not_ask(logged_in_client, web_db):
    """Лишний вопрос на безопасном действии приучает жать «Да» не читая — и тогда
    вопрос на опасном тоже перестаёт работать."""
    page = _page(logged_in_client, web_db)

    assert "data-confirm" not in _button(page, "broadcast_on")


def test_the_question_carries_the_count(logged_in_client, web_db):
    """Без числа «выключить у отмеченных» не говорит человеку ничего: отбор
    переживает смену фильтра, и строки могут быть не видны."""
    page = _page(logged_in_client, web_db)

    assert "%COUNT%" in _button(page, "broadcast_off")
    assert 'button.dataset.confirm.replace("%COUNT%"' in page


def test_the_warning_mentions_the_hidden_selection(logged_in_client, web_db):
    """Самое неочевидное последствие — отмеченные строки вне текущего фильтра."""
    assert "не видны при текущем фильтре" in _button(
        _page(logged_in_client, web_db), "broadcast_off")

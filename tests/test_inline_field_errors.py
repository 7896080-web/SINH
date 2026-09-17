"""Находка 18 аудита: сообщение об ошибке ввода не доходило до оператора.

Ответ на правку поля — HTMX-фрагмент (одна строка таблицы), а флеш-сообщение
снимается только при полной перезагрузке страницы. Поэтому строка молча
перерисовывалась прежним значением, оператор считал, что ввод принят, а плашка
всплывала позже и уже без контекста. У числовых полей было ещё хуже: браузер
отдаёт пустую строку вместо «12,5», FastAPI с `int = Form(...)` отвечал 422, и
htmx при таком ответе строку не подменяет вовсе — не было даже перерисовки.

Теперь ошибка возвращается В САМОЙ строке, рядом с тем полем, куда вводили.
"""
import pytest

from app.models import Barcode, Platform, PlatformAccount, Product, SyncSetting


def _product(web_db, uid: str = "u1", stock: int = 10, reserve: int = 3) -> Product:
    p = Product(uid_1c=uid, article="A-1", name="Товар", stock_on_hand=stock,
                reserve=reserve, broadcast_enabled=True)
    web_db.add(p)
    web_db.add(Barcode(barcode="111", uid_1c=uid))
    web_db.commit()
    return p


def _account(web_db, name: str = "Кабинет") -> PlatformAccount:
    account = PlatformAccount(platform=Platform.wb, name=name, warehouse_id="wh")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    return account


# --------------------------------------------- порог трансляции (сценарий аудита)

def test_offset_with_comma_shows_the_error_in_the_row(logged_in_client, web_db):
    """Сценарий из аудита дословно: ввод «12,5» в порог трансляции."""
    _product(web_db)

    r = logged_in_client.post("/products/u1/offset", data={"value": "12,5"})

    assert r.status_code == 200
    assert "Порог: введите целое число" in r.text


def test_offset_with_comma_does_not_change_the_value(logged_in_client, web_db):
    product = _product(web_db)
    product.broadcast_offset = 11
    web_db.commit()

    logged_in_client.post("/products/u1/offset", data={"value": "12,5"})

    web_db.refresh(product)
    assert product.broadcast_offset == 11       # прежнее значение не тронуто


def test_valid_offset_has_no_error(logged_in_client, web_db):
    _product(web_db)

    r = logged_in_client.post("/products/u1/offset", data={"value": "11"})

    assert "pr-error" not in r.text
    assert web_db.query(Product).first().broadcast_offset == 11


# --------------------------------------------- резерв и порог кабинета

def test_reserve_with_comma_answers_with_the_row_and_an_error(logged_in_client, web_db):
    """Раньше здесь был 422 и htmx не подменял строку вообще — оператор не видел
    ни ошибки, ни своего значения."""
    product = _product(web_db, reserve=3)

    r = logged_in_client.post("/products/u1/reserve", data={"reserve": "12,5"})

    assert r.status_code == 200                 # не 422
    assert "Резерв: введите целое число" in r.text
    web_db.refresh(product)
    assert product.reserve == 3


def test_threshold_with_comma_answers_with_the_row_and_an_error(logged_in_client, web_db):
    product = _product(web_db)
    account = _account(web_db)
    setting = SyncSetting(uid_1c="u1", account_id=account.id, enabled=True, min_threshold=5)
    web_db.add(setting)
    web_db.commit()

    r = logged_in_client.post(f"/products/u1/{account.id}/threshold",
                              data={"min_threshold": "12,5"})

    assert r.status_code == 200
    assert "Порог кабинета: введите целое число" in r.text
    web_db.refresh(setting)
    assert setting.min_threshold == 5


def test_empty_reserve_is_an_error_not_a_silent_zero(logged_in_client, web_db):
    """Пустое поле — тоже нечисловой ввод (браузер так отдаёт «12,5»). Молча
    ставить 0 нельзя: это уменьшило бы резерв без ведома оператора."""
    product = _product(web_db, reserve=3)

    r = logged_in_client.post("/products/u1/reserve", data={"reserve": ""})

    assert "Резерв: введите целое число" in r.text
    web_db.refresh(product)
    assert product.reserve == 3


# --------------------------------------------- дата старта

def test_bad_date_shows_the_error_in_the_row(logged_in_client, web_db):
    _product(web_db)

    r = logged_in_client.post("/products/u1/active-since", data={"value": "07.08.2026"})

    assert "Дата должна быть в формате" in r.text
    assert web_db.query(Product).first().broadcast_active_since is None


# --------------------------------------------- ошибка стоит у нужного поля

# Подсказки (title) полей строки — по ним находим, у какого именно поля встала ошибка.
FIELD_ANCHORS = {
    "offset": "Постоянное расхождение между учётом 1С",
    "reserve": "Сколько штук держим у себя",
    "active_since": "Дата старта задним числом",
}


@pytest.mark.parametrize("url,data,field", [
    ("/products/u1/offset", {"value": "12,5"}, "offset"),
    ("/products/u1/reserve", {"reserve": "12,5"}, "reserve"),
    ("/products/u1/active-since", {"value": "07.08.2026"}, "active_since"),
])
def test_error_is_placed_next_to_its_own_field(logged_in_client, web_db, url, data, field):
    """Ошибка должна стоять у того поля, куда вводили: полей в строке больше
    восьми, и сообщение «введите целое число» без привязки бесполезно."""
    _product(web_db)

    r = logged_in_client.post(url, data=data)

    anchor_pos = r.text.find(FIELD_ANCHORS[field])
    error_pos = r.text.find("pr-error")
    assert anchor_pos != -1 and error_pos != -1
    # ошибка идёт сразу за подсказкой своего поля, а не где-то ещё в строке
    assert 0 < error_pos - anchor_pos < 400
    # и у чужих полей её нет
    for other, other_anchor in FIELD_ANCHORS.items():
        if other == field:
            continue
        other_pos = r.text.find(other_anchor)
        assert not (0 < error_pos - other_pos < 400), f"ошибка встала у поля {other}"


def test_threshold_error_is_placed_at_the_right_cabinet(logged_in_client, web_db):
    """Кабинетов в строке несколько — ошибка обязана стоять у того, где вводили.
    Порядок кабинетов в строке алфавитный, а не порядок создания, поэтому
    сравниваем не позиции в тексте, а блоки кабинетов."""
    _product(web_db)
    first = _account(web_db, name="Кабинет ПЕРВЫЙ")
    second = _account(web_db, name="Кабинет ВТОРОЙ")
    for account in (first, second):
        web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    r = logged_in_client.post(f"/products/u1/{second.id}/threshold",
                              data={"min_threshold": "12,5"})

    blocks = r.text.split('<div class="pr-cab">')[1:]
    touched = [b for b in blocks if "Кабинет ВТОРОЙ" in b]
    other = [b for b in blocks if "Кабинет ПЕРВЫЙ" in b]
    assert touched and other
    assert "pr-error" in touched[0]
    assert "pr-error" not in other[0]


# --------------------------------------------- флеш больше не всплывает потом

def test_error_does_not_leak_into_the_next_full_page(logged_in_client, web_db):
    """Раньше сообщение уходило во флеш и всплывало при следующей полной
    загрузке страницы — без контекста и уже непонятно к чему."""
    _product(web_db)
    logged_in_client.post("/products/u1/offset", data={"value": "12,5"})

    page = logged_in_client.get("/products")

    assert "Порог: введите целое число" not in page.text

"""Сообщение о результате импорта обязано доехать до страницы.

Флеш едет в cookie сессии: подписанный JSON в base64. Браузеры отбрасывают
cookie длиннее 4096 байт ЦЕЛИКОМ и молча — не обрезают, а выбрасывают вместе с
сессией. Замер 21.09: импорт двенадцати строк с ошибками давал cookie в 7106
байт, то есть оператор видел страницу вообще без сообщения — ни сколько строк
применилось, ни сколько не применилось и почему. Чем хуже проходил импорт, тем
вернее пропадало сообщение.

Ловушка в счёте: JSON сессии кодируется с экранированием не-ASCII, поэтому
каждая кириллическая буква занимает ШЕСТЬ байт, а после base64 — 8,76. Первая
попытка поставить предел в тысячу символов дала 6674 байта, то есть не помогла
вовсе. Поэтому тест меряет НАСТОЯЩИЙ размер cookie, а не длину строки.
"""
import io

from openpyxl import Workbook

from app.flash import MAX_FLASH_CHARS, TRUNCATION_NOTE
from app.models import Barcode, Product
from tests.factories import make_account

COOKIE_LIMIT = 4096


def _file(headers, rows):
    wb = Workbook(); ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def _cookie_value(response) -> str:
    raw = response.headers.get("set-cookie", "")
    return raw.split("=", 1)[1].split(";")[0] if "=" in raw else ""


def _import_with_many_errors(client, db, count=12):
    account = make_account(db, name="ИП ЯВОРСКАЯ")
    label = f"{account.name} ({account.platform.value.upper()})"
    for i in range(count):
        db.add(Product(uid_1c=f"u{i}", article=f"A-{i}", name="Товар",
                       stock_on_hand=5, reserve=0))
        db.add(Barcode(barcode=f"bc{i}", uid_1c=f"u{i}"))
    db.commit()
    rows = [[f"u{i}", "2026-08-07", "Да", "Да"] for i in range(count)]
    content = _file(["ID_1С", "Дата расчёта", "Трансляция",
                     f"{label} — Синхронизировать"], rows)
    return client.post("/products/import", files={"file": ("t.xlsx", content)},
                       follow_redirects=False)


def test_a_bad_import_still_fits_in_the_cookie(logged_in_client, web_db):
    r = _import_with_many_errors(logged_in_client, web_db)

    assert len(_cookie_value(r)) < COOKIE_LIMIT, (
        "cookie длиннее 4096 байт браузер отбросит целиком — вместе с сессией")


def test_the_message_survives_and_says_it_was_cut(logged_in_client, web_db):
    """Обрезка без пометки — та же потеря: человек прочитает оборванную фразу и
    решит, что это всё."""
    _import_with_many_errors(logged_in_client, web_db)

    page = logged_in_client.get("/products")

    assert TRUNCATION_NOTE in page.text


def test_a_short_message_is_untouched(logged_in_client, web_db):
    """Обычный путь не задет: короткое сообщение доезжает как есть."""
    from app.flash import set_flash

    class FakeRequest:
        session: dict = {}

    request = FakeRequest()
    set_flash(request, "Обновлено 3 строки.", "good")

    assert request.session["flash"]["message"] == "Обновлено 3 строки."


def test_the_limit_leaves_room_for_the_rest_of_the_session(logged_in_client, web_db):
    """Предел посчитан от 4096 байт минус подпись, логин и атрибуты cookie.
    Сторожим само число: поднимут его «чтобы влезало больше» — вернётся пропажа."""
    assert MAX_FLASH_CHARS * 8.76 < COOKIE_LIMIT - 200

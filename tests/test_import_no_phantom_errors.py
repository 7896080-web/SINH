"""Пустая ячейка не должна давать ОШИБКУ импорта — она не просит ничего менять.

Найдено аудитом 22.09. `_cell_intent` отдаёт пустую ячейку как ("skip", ""), но
в колонках «Факт на дату» и «Порог трансляции» число разбиралось ДО проверки
намерения: `float("")` бросает ValueError, и в ошибки уходило «некорректный факт
на дату» / «некорректный порог трансляции». Значение при этом не менялось —
поэтому поведенческие тесты на поля дефект не ловили вовсе.

Цена не в самой строчке, а в том, что она вытесняет. Выгрузка пишет в эти
колонки пустоту у каждой ненастроенной строки (products.py собирает их как ""),
так что штатный круг «выгрузка → правка → импорт» давал до ДВУХ ложных ошибок на
строку — на файле в двадцать тысяч строк до сорока тысяч. Показываются первые
пять плюс «и ещё N», и всё это режется по `MAX_FLASH_CHARS` = 440. Настоящие
ошибки — «трансляцию включить нельзя, нужен Факт на дату», «остатков на будущую
дату в 1С нет», расхождение порога кабинета — в сообщение не попадали никогда.

А оператор видел жёлтую плашку «Ошибок: 40000» на полностью успешном импорте.
Отчёт, который ругается всегда, перестают читать — и тогда он не работает вовсе.
"""
import io

import pytest
from openpyxl import Workbook

from app.models import Platform, PlatformAccount, Product


def _account(web_db):
    """Активный кабинет обязателен В КАЖДОМ тесте, и это не декорация.

    Без него страница товаров ставит СВОЙ флеш («нет ни одного активного
    кабинета») и затирает им сообщение импорта. Проверка «ошибки нет» тогда
    проходит всегда — в том числе на сломанном коде. Поймано ровно так: два
    теста на настоящий мусор упали, а пять «зелёных» оказались пустыми.
    """
    a = PlatformAccount(platform=Platform.wb, name="WB-1", warehouse_id="wh")
    web_db.add(a)
    web_db.commit()
    return a


def _product(web_db, uid="u1", **kw):
    p = Product(uid_1c=uid, article=f"A-{uid}", name=f"Товар {uid}", size="M",
                stock_on_hand=10, reserve=0, **kw)
    web_db.add(p)
    web_db.commit()
    return p


def _file(headers, *rows):
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _import(client, data):
    return client.post(
        "/products/import",
        files={"file": ("f.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=True).text


@pytest.mark.parametrize("column", ["Факт на дату", "Порог трансляции"])
def test_an_empty_cell_is_not_an_error(logged_in_client, web_db, column):
    """Ровно то, что отдаёт наша же выгрузка по ненастроенной строке."""
    _account(web_db)
    _product(web_db)

    page = _import(logged_in_client, _file(["ID_1С", column], ["u1", ""]))

    assert "некорректн" not in page


def test_both_empty_columns_together_give_no_errors(logged_in_client, web_db):
    """Штатный круг целиком: выгрузили, ничего не правили, залили обратно."""
    _account(web_db)
    _product(web_db)

    page = _import(logged_in_client, _file(
        ["ID_1С", "Факт на дату", "Порог трансляции"], ["u1", "", ""]))

    assert "некорректн" not in page
    assert "Ошибок" not in page


def test_an_empty_cell_still_changes_nothing(logged_in_client, web_db):
    """Второе обязательное свойство: молчать — не значит применять. Пустая
    ячейка не должна ни стирать факт (он получен пересчётом склада руками), ни
    обнулять порог (это вернуло бы на площадки ПОЛНЫЙ остаток)."""
    _account(web_db)
    _product(web_db, fact_at_date=7, broadcast_offset=3)

    _import(logged_in_client, _file(
        ["ID_1С", "Факт на дату", "Порог трансляции"], ["u1", "", ""]))

    web_db.expire_all()
    product = web_db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.fact_at_date == 7 and product.broadcast_offset == 3


@pytest.mark.parametrize("column,text", [
    ("Факт на дату", "некорректный факт"),
    ("Порог трансляции", "некорректный порог"),
])
def test_real_garbage_is_still_an_error(logged_in_client, web_db, column, text):
    """Обратная сторона: молчать про ВСЁ нельзя. «абв» в числовой колонке —
    настоящая ошибка, и проглотить её значило бы применить файл наполовину."""
    _account(web_db)
    _product(web_db)

    page = _import(logged_in_client, _file(["ID_1С", column], ["u1", "абв"]))

    assert text in page


def test_a_dash_still_clears_the_value(logged_in_client, web_db):
    """И третье: явное «-» по-прежнему снимает значение. Пустота и «-» обязаны
    различаться — на этом построена вся семантика ячейки."""
    _account(web_db)
    _product(web_db, fact_at_date=7)

    _import(logged_in_client, _file(["ID_1С", "Факт на дату"], ["u1", "-"]))

    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first().fact_at_date is None

"""Пустая ячейка в файле импорта НИЧЕГО НЕ МЕНЯЕТ.

Раньше каждая колонка понимала пустоту как самое разрушительное из возможных
значений, и последствия у этого разные по тяжести, но одинаковые по механике:
файл применяет правку сразу ко всему каталогу, молча.

  «Трансляция» и «<кабинет> — Синхронизировать» читались как «Нет» — а это не
  просто выключение, а ОТЗЫВ остатка: ноль на живую карточку площадки, по всем
  кабинетам товара сразу. «<кабинет> — Порог» становился нулём. «Порог
  трансляции» стирался — и на площадки возвращался ПОЛНЫЙ остаток. «Дата
  расчёта» снимала расчёт вместе с ФАКТОМ, который человек получил, пересчитав
  склад руками: восстановить его нечем.

Выгрузка все эти ячейки заполняет. Пустой ячейка становится ровно в двух
случаях — её стёрли или файл собран не из нашей выгрузки, — и ни один из них не
значит «примени самое опасное». Снять значение по-прежнему можно, но сказав об
этом вслух: «-» в ячейке.
"""
import io
from datetime import date

from openpyxl import Workbook

from app.models import Barcode, DispatchQueueItem, Product, SyncSetting
from tests.factories import make_account

DAY = date(2026, 8, 7)


def _file(headers, *rows):
    wb = Workbook(); ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def _import(client, content):
    return client.post("/products/import", files={"file": ("t.xlsx", content)},
                       follow_redirects=False)


def _seed(db, **kwargs):
    fields = dict(article="A-1", name="Товар", stock_on_hand=20, reserve=3)
    fields.update(kwargs)
    product = Product(uid_1c="u1", **fields)
    db.add(product)
    db.add(Barcode(barcode="bc-u1", uid_1c="u1"))
    db.commit()
    return product


def _label(account):
    return f"{account.name} ({account.platform.value.upper()})"


# --------------------------------------------------------- отзыв остатка

def test_an_empty_broadcast_cell_does_not_withdraw_the_stock(logged_in_client, web_db):
    """Худший случай: пустая ячейка выключала трансляцию, а выключение отзывает
    остаток — ноль уезжает на карточку, по которой идут продажи."""
    account = make_account(web_db, name="ИП ЯВОРСКАЯ")
    product = _seed(web_db, broadcast_enabled=True, offset_base_date=DAY,
                    offset_base_stock=20, fact_at_date=20)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True,
                           last_nonzero_sent_at=date(2026, 9, 1)))
    web_db.commit()

    _import(logged_in_client, _file(["ID_1С", "Трансляция"], ["u1", ""]))

    web_db.expire_all()
    assert web_db.query(Product).one().broadcast_enabled is True
    assert web_db.query(DispatchQueueItem).count() == 0, "отзыв не должен ставиться"


def test_an_empty_account_cell_does_not_untick_the_account(logged_in_client, web_db):
    """То же по кабинету: снятие галочки тоже отзывает остаток."""
    account = make_account(web_db, name="ИП ЯВОРСКАЯ")
    _seed(web_db, broadcast_enabled=True)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True,
                           min_threshold=5, last_nonzero_sent_at=date(2026, 9, 1)))
    web_db.commit()

    _import(logged_in_client, _file(
        ["ID_1С", f"{_label(account)} — Синхронизировать", f"{_label(account)} — Порог"],
        ["u1", "", ""]))

    web_db.expire_all()
    setting = web_db.query(SyncSetting).one()
    assert setting.enabled is True
    assert setting.min_threshold == 5, "пустая ячейка не обнуляет порог кабинета"
    assert web_db.query(DispatchQueueItem).count() == 0


def test_a_dash_is_still_an_explicit_switch_off(logged_in_client, web_db):
    """Снять по-прежнему можно — сказав об этом вслух. Иначе правка превратила бы
    файл в путь, которым ничего нельзя выключить."""
    account = make_account(web_db, name="ИП ЯВОРСКАЯ")
    _seed(web_db, broadcast_enabled=True)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    _import(logged_in_client, _file(["ID_1С", "Трансляция"], ["u1", "-"]))

    web_db.expire_all()
    assert web_db.query(Product).one().broadcast_enabled is False


# --------------------------------------------------------- труд оператора

def test_an_empty_fact_cell_does_not_erase_the_fact(logged_in_client, web_db):
    """Факт — это пересчитанный руками склад. Стереть его молча по всему файлу
    значит выбросить работу, которую не восстановить."""
    _seed(web_db, offset_base_date=DAY, offset_base_stock=20, fact_at_date=18)

    _import(logged_in_client, _file(["ID_1С", "Факт на дату"], ["u1", ""]))

    web_db.expire_all()
    assert web_db.query(Product).one().fact_at_date == 18


def test_an_empty_date_cell_does_not_drop_the_calculation(logged_in_client, web_db):
    """Снятие даты уносит с собой и факт — см. `set_base_date`."""
    _seed(web_db, offset_base_date=DAY, offset_base_stock=20, fact_at_date=18)

    _import(logged_in_client, _file(["ID_1С", "Дата расчёта"], ["u1", ""]))

    web_db.expire_all()
    product = web_db.query(Product).one()
    assert product.offset_base_date == DAY
    assert product.fact_at_date == 18


def test_an_empty_threshold_cell_does_not_return_the_full_stock(logged_in_client, web_db):
    """Стёртый порог — это полный остаток на площадках, то есть ровно то, от чего
    порог и защищает."""
    _seed(web_db, broadcast_offset=7)

    _import(logged_in_client, _file(["ID_1С", "Порог трансляции"], ["u1", ""]))

    web_db.expire_all()
    assert web_db.query(Product).one().broadcast_offset == 7


def test_an_empty_reserve_cell_does_not_zero_the_reserve(logged_in_client, web_db):
    """Бронь — это то, что держим у себя; обнулив её, отдаём в продажу чужое."""
    _seed(web_db, reserve=4)

    _import(logged_in_client, _file(["ID_1С", "Резерв"], ["u1", ""]))

    web_db.expire_all()
    assert web_db.query(Product).one().reserve == 4


# --------------------------------------------------------- массовая правка

def test_bulk_date_with_an_empty_field_is_refused(logged_in_client, web_db):
    """Та же дверь на странице: пустое поле даты снимало расчёт и факт сразу по
    всему отбору. Для чисел отказ при пустом поле стоял всегда — дата была
    единственной, где пустота проходила молча."""
    _seed(web_db, offset_base_date=DAY, offset_base_stock=20, fact_at_date=18)

    logged_in_client.post("/products/bulk", data={
        "action": "set_base_date", "uids": ["u1"], "date_value": ""})

    web_db.expire_all()
    product = web_db.query(Product).one()
    assert product.offset_base_date == DAY
    assert product.fact_at_date == 18

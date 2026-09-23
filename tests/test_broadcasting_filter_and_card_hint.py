"""Фильтр «только с включённой трансляцией» и подсказка про карточки в выгрузке.

Два разных вопроса, которые оператор задаёт на одной и той же странице.

Первый: что сейчас РЕАЛЬНО уходит наружу. Соседний фильтр «только отмеченные
кабинеты» на него не отвечает — галочка кабинета говорит, КУДА передавать, а
трансляция товара говорит, передаём ли вообще, и выключенная перекрывает любые
галочки. На каталоге в 152 тысячи SKU включённых строк сотни, и найти их среди
остальных было нечем.

Второй: какой кабинет вообще можно отметить. Отметить кабинет, где карточки
товара нет, — значит завести пару, по которой остаток не уедет никогда: рассылка
закроет её как «нет карточки в каталоге кабинета». Раньше это выяснялось
поштучно на «Мэппинге», а массовую правку тем и делают, что поштучно долго.

Отдельно закреплено, что выгрузка изменилась РОВНО одной колонкой: файл правят в
Excel и заливают обратно, и сдвиг остальных колонок стоил бы оператору данных.
"""
import io

from openpyxl import load_workbook

from app.models import (Barcode, Platform, PlatformAccount, PlatformCatalogItem,
                        Product, SyncSetting)


def _account(web_db, name="WB-1", platform=Platform.wb, active=True):
    a = PlatformAccount(platform=platform, name=name, warehouse_id="wh", is_active=active)
    web_db.add(a)
    web_db.commit()
    return a


def _product(web_db, uid, broadcast=False, **kw):
    p = Product(uid_1c=uid, article=f"A-{uid}", name=f"Товар {uid}", size="M",
                stock_on_hand=10, reserve=0, broadcast_enabled=broadcast, **kw)
    web_db.add(p)
    web_db.commit()
    return p


def _uids(resp):
    ws = load_workbook(io.BytesIO(resp.content)).active
    return [r[0].value for r in ws.iter_rows(min_row=2) if r[0].value]


def _sheet(resp):
    ws = load_workbook(io.BytesIO(resp.content)).active
    rows = list(ws.iter_rows(values_only=True))
    return rows[0], rows[1:]


# ------------------------------------------------------------------ фильтр

def test_the_filter_keeps_only_broadcasting_rows(logged_in_client, web_db):
    _product(web_db, "u1", broadcast=True)
    _product(web_db, "u2", broadcast=False)

    page = logged_in_client.get("/products?only_broadcasting=true")

    assert "Товар u1" in page.text
    assert "Товар u2" not in page.text


def test_without_the_filter_both_are_shown(logged_in_client, web_db):
    """Фильтр должен что-то менять — иначе тест выше проходил бы и на пустом месте."""
    _product(web_db, "u1", broadcast=True)
    _product(web_db, "u2", broadcast=False)

    page = logged_in_client.get("/products")

    assert "Товар u1" in page.text and "Товар u2" in page.text


def test_a_marked_cabinet_does_not_make_a_product_broadcasting(web_db, logged_in_client):
    """Главное отличие от соседнего фильтра «только отмеченные кабинеты»:
    отмеченный кабинет при выключенной трансляции наружу не отправляет ничего."""
    account = _account(web_db)
    _product(web_db, "u1", broadcast=False)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    assert "Товар u1" in logged_in_client.get("/products?only_marked=true").text
    assert "Товар u1" not in logged_in_client.get("/products?only_broadcasting=true").text


def test_the_export_honours_the_filter(logged_in_client, web_db):
    """Выгрузка обязана отдавать ровно то, что отобрано на странице. Ровно этим
    уже обжигались: `only_unfinished` ссылка передавала, а эндпоинт не принимал,
    и FastAPI молча отбрасывал параметр — в файл уходил весь каталог."""
    _product(web_db, "u1", broadcast=True)
    _product(web_db, "u2", broadcast=False)

    assert _uids(logged_in_client.get("/products/export?only_broadcasting=true")) == ["u1"]


def test_bulk_edit_over_the_filter_touches_only_broadcasting_rows(logged_in_client, web_db):
    """Массовая правка «по всему отбору» считает отбор заново на сервере. Не
    знай он про фильтр — правка ушла бы по всему каталогу, а оператор решил бы,
    что тронул отобранное."""
    _product(web_db, "u1", broadcast=True)
    _product(web_db, "u2", broadcast=False)

    logged_in_client.post("/products/bulk", data={
        "action": "set_reserve", "int_value": "7",
        "only_broadcasting": "true", "all_filtered": "true"})

    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first().reserve == 7
    assert web_db.query(Product).filter(Product.uid_1c == "u2").first().reserve == 0


def test_the_filter_survives_a_bulk_edit(logged_in_client, web_db):
    """Возврат после правки — С ТЕМИ ЖЕ фильтрами: иначе оператор попадает на
    полный список, где отобранных строк уже не найти."""
    _product(web_db, "u1", broadcast=True)

    r = logged_in_client.post("/products/bulk", data={
        "action": "set_reserve", "int_value": "1", "uids": ["u1"],
        "only_broadcasting": "true"}, follow_redirects=False)

    assert "only_broadcasting=true" in r.headers["location"]


# ------------------------------------------- подсказка про карточки в выгрузке

def _with_card(web_db, uid, account, barcode):
    _product(web_db, uid)
    web_db.add(Barcode(barcode=barcode, uid_1c=uid))
    web_db.add(PlatformCatalogItem(account_id=account.id, external_id="100:1",
                                   barcode=barcode, article="A", name="Карточка"))
    web_db.commit()


def _hint(resp, uid):
    header, rows = _sheet(resp)
    col = header.index("Карточка есть в кабинетах")
    for row in rows:
        if row[0] == uid:
            return row[col]
    raise AssertionError(f"строки {uid} нет в выгрузке")


def test_the_hint_names_the_cabinet_that_has_the_card(logged_in_client, web_db):
    account = _account(web_db, name="ИП ЯВОРСКАЯ")
    _with_card(web_db, "u1", account, "111")

    assert _hint(logged_in_client.get("/products/export"), "u1") == "ИП ЯВОРСКАЯ (WB)"


def test_a_product_without_a_card_gets_an_empty_hint(logged_in_client, web_db):
    """Пусто, а не «нет»: колонка справочная, и любое слово в ней импорт мог бы
    когда-нибудь принять за значение."""
    _account(web_db)
    _product(web_db, "u1")
    web_db.add(Barcode(barcode="111", uid_1c="u1"))
    web_db.commit()

    # Пустую строку openpyxl кладёт в файл как пустую ячейку и читает как None.
    assert _hint(logged_in_client.get("/products/export"), "u1") in (None, "")


def test_several_cabinets_are_all_named(logged_in_client, web_db):
    """У товара бывает карточка в нескольких кабинетах — подсказка обязана
    назвать все: оператор по ней и решает, какой кабинет отметить."""
    wb = _account(web_db, name="ИП ЯВОРСКАЯ")
    kit = _account(web_db, name="КИТ", platform=Platform.kit)
    _product(web_db, "u1")
    web_db.add(Barcode(barcode="111", uid_1c="u1"))
    web_db.add(PlatformCatalogItem(account_id=wb.id, external_id="100:1",
                                   barcode="111", article="A", name="К"))
    web_db.add(PlatformCatalogItem(account_id=kit.id, external_id="v-1",
                                   barcode="111", article="A", name="К"))
    web_db.commit()

    assert _hint(logged_in_client.get("/products/export"), "u1") == "ИП ЯВОРСКАЯ (WB), КИТ (KIT)"


def test_a_disabled_cabinet_still_counts(logged_in_client, web_db):
    """Каталог остаётся от кабинета, погашенного предохранителем или выключенного
    руками, и карточка на площадке от этого никуда не делась. Та же логика, что
    на странице «Есть на складе — нет на площадке»."""
    account = _account(web_db, name="СПЯЩИЙ", active=False)
    _with_card(web_db, "u1", account, "111")

    assert _hint(logged_in_client.get("/products/export"), "u1") == "СПЯЩИЙ (WB)"


# ------------------------------- и больше в файле не изменилось ничего

def test_the_export_layout_changed_by_exactly_one_column(logged_in_client, web_db):
    """Файл правят в Excel и заливают обратно. Сдвинь мы остальные колонки или
    поменяй их порядок — оператор залил бы данные не в те поля."""
    account = _account(web_db, name="ИП ЯВОРСКАЯ")
    _with_card(web_db, "u1", account, "111")

    header, _ = _sheet(logged_in_client.get("/products/export"))

    assert list(header) == [
        "ID_1С", "Артикул", "Размер", "Цвет", "Наименование", "Остаток ЦС",
        "Дата расчёта", "Остаток ЦС на дату", "Резерв", "Факт на дату",
        "Расхождение (справочно)",
        "Порог трансляции", "Трансляция", "Уходит на площадки",
        "Карточка есть в кабинетах",
        "ИП ЯВОРСКАЯ (WB) — Синхронизировать", "ИП ЯВОРСКАЯ (WB) — Порог",
    ]


def test_the_yes_no_dropdown_still_lands_on_the_right_columns(logged_in_client, web_db):
    """Проверка Excel ставится по НОМЕРУ колонки, а новая колонка номера
    сдвинула. Промахнись она — «Да/Нет» оказалось бы выпадающим списком на
    чужом поле, а на своём его бы не было, и опечатка снова молча выключала бы
    трансляцию."""
    account = _account(web_db, name="ИП ЯВОРСКАЯ")
    _with_card(web_db, "u1", account, "111")

    resp = logged_in_client.get("/products/export")
    ws = load_workbook(io.BytesIO(resp.content)).active
    header, _ = _sheet(resp)
    from openpyxl.utils import get_column_letter

    # Диапазон из одной строки openpyxl нормализует в «L2» без двоеточия.
    ranges = {str(dv.sqref) for dv in ws.data_validations.dataValidation}
    for name in ("Трансляция", "ИП ЯВОРСКАЯ (WB) — Синхронизировать"):
        letter = get_column_letter(header.index(name) + 1)
        assert {f"{letter}2", f"{letter}2:{letter}2"} & ranges, \
            f"нет списка на колонке «{name}», есть {sorted(ranges)}"


def test_the_import_ignores_the_hint_column(logged_in_client, web_db):
    """Колонка справочная. Файл заливают обратно целиком, и импорт обязан её
    пропустить, а не принять за значение и не выругаться на неё."""
    account = _account(web_db, name="ИП ЯВОРСКАЯ")
    _with_card(web_db, "u1", account, "111")

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["ID_1С", "Карточка есть в кабинетах", "Резерв"])
    ws.append(["u1", "ИП ЯВОРСКАЯ (WB)", 4])
    buf = io.BytesIO()
    wb.save(buf)

    page = logged_in_client.post(
        "/products/import",
        files={"file": ("f.xlsx", buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=True)

    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first().reserve == 4
    assert "ошибк" not in page.text.lower() or "0" in page.text

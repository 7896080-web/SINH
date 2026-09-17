"""Страница «Товары и остатки»: расчёт порога от даты.

Порог больше не вводится руками — оператор задаёт дату, видит остаток ЦС на неё
из выгрузки 1С, вписывает факт, и порог считается сам. Здесь проверяется, что
страница действительно так себя ведёт: и по одной строке, и массово.
"""
from datetime import date, timedelta

from app.models import (Platform, PlatformAccount, Product, StockDateRow, StockDateSnapshot,
                        StockDateStatus)
from app.timeutils import now_utc

DAY = date(2026, 8, 7)


def _product(web_db, uid="u1", stock=11, reserve=0) -> Product:
    p = Product(uid_1c=uid, article="A-1", name="Товар", stock_on_hand=stock,
                reserve=reserve, broadcast_enabled=True)
    web_db.add(p)
    web_db.commit()
    return p


def _account(web_db, name="WB-1") -> PlatformAccount:
    """Кабинет нужен не сам по себе: без единого активного кабинета страница
    показывает своё сообщение «добавьте кабинет» и перекрывает им результат
    импорта. На боевом кабинеты есть всегда."""
    a = PlatformAccount(platform=Platform.wb, name=name, warehouse_id="wh")
    web_db.add(a)
    web_db.commit()
    web_db.refresh(a)
    return a


def _snapshot(web_db, rows, day=DAY):
    snap = StockDateSnapshot(snapshot_date=day, status=StockDateStatus.done, rows_count=len(rows))
    web_db.add(snap)
    web_db.commit()
    web_db.refresh(snap)
    for uid, qty in rows:
        web_db.add(StockDateRow(snapshot_id=snap.id, uid_1c=uid, quantity=qty))
    web_db.commit()
    return snap


# ------------------------------------------------ по одной строке

def test_setting_a_date_pulls_the_stock_and_computes(logged_in_client, web_db):
    _snapshot(web_db, [("u1", 10)])
    product = _product(web_db, reserve=2)

    logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})
    logged_in_client.post("/products/u1/fact", data={"value": "8"})

    web_db.refresh(product)
    assert product.offset_base_stock == 10
    assert product.broadcast_offset == 4


def test_the_row_shows_the_arithmetic(logged_in_client, web_db):
    """Оператор должен видеть не только число, но и из чего оно вышло — иначе
    порог выглядит как магия и ему нечем верить."""
    _snapshot(web_db, [("u1", 10)])
    _product(web_db, reserve=2)
    logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})

    r = logged_in_client.post("/products/u1/fact", data={"value": "8"})

    assert "10 − (8 − бронь 2)" in r.text


def test_waiting_for_1c_is_visible_in_the_row(logged_in_client, web_db):
    """Снимка на дату ещё нет. Строка обязана объяснить, что происходит, а не
    молча показывать пустой порог."""
    _product(web_db)

    r = logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})

    assert "ждём выгрузку 1С" in r.text


def test_future_date_is_refused(logged_in_client, web_db):
    product = _product(web_db)
    tomorrow = (now_utc().date() + timedelta(days=1)).isoformat()

    r = logged_in_client.post("/products/u1/base-date", data={"value": tomorrow})

    assert "на будущую дату" in r.text
    web_db.refresh(product)
    assert product.offset_base_date is None


def test_changing_the_reserve_moves_the_threshold(logged_in_client, web_db):
    """Ради этого три величины и хранятся."""
    _snapshot(web_db, [("u1", 10)])
    product = _product(web_db, reserve=2)
    logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})
    logged_in_client.post("/products/u1/fact", data={"value": "8"})

    logged_in_client.post("/products/u1/reserve", data={"reserve": "5"})

    web_db.refresh(product)
    assert product.broadcast_offset == 7


def test_clearing_the_fact_falls_back_to_the_1c_number(logged_in_client, web_db):
    _snapshot(web_db, [("u1", 10)])
    product = _product(web_db, reserve=2)
    logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})
    logged_in_client.post("/products/u1/fact", data={"value": "8"})

    logged_in_client.post("/products/u1/fact", data={"value": ""})

    web_db.refresh(product)
    assert product.fact_at_date is None
    assert product.broadcast_offset == 2        # порог сводится к брони


def test_clearing_the_threshold_removes_the_whole_calculation(logged_in_client, web_db):
    """«Сбросить порог» должно действительно сбрасывать: если оставить дату,
    первая же правка брони вернёт порог, и оператор решит, что кнопка не работает."""
    _snapshot(web_db, [("u1", 10)])
    product = _product(web_db, reserve=2)
    logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})

    logged_in_client.post("/products/u1/offset", data={"clear": "1"})
    logged_in_client.post("/products/u1/reserve", data={"reserve": "4"})

    web_db.refresh(product)
    assert product.offset_base_date is None
    assert product.broadcast_offset is None


# ------------------------------------------------ массовые действия

def test_bulk_date_applies_to_the_selected_rows(logged_in_client, web_db):
    _snapshot(web_db, [("u1", 10), ("u2", 4)])
    a, b = _product(web_db, uid="u1", reserve=2), _product(web_db, uid="u2", reserve=1)

    logged_in_client.post("/products/bulk", data={
        "action": "set_base_date", "uids": ["u1", "u2"], "date_value": "2026-08-07"})

    web_db.refresh(a), web_db.refresh(b)
    assert (a.offset_base_stock, a.broadcast_offset) == (10, 2)
    assert (b.offset_base_stock, b.broadcast_offset) == (4, 1)


def test_bulk_fact_from_stock_does_not_overwrite_typed_values(logged_in_client, web_db):
    """Кнопка «Факт = остаток ЦС» заполняет только пустые: затирать введённое
    руками массовым действием нельзя."""
    _snapshot(web_db, [("u1", 10), ("u2", 4)])
    typed, empty = _product(web_db, uid="u1"), _product(web_db, uid="u2")
    logged_in_client.post("/products/bulk", data={
        "action": "set_base_date", "uids": ["u1", "u2"], "date_value": "2026-08-07"})
    logged_in_client.post("/products/u1/fact", data={"value": "3"})

    logged_in_client.post("/products/bulk", data={
        "action": "fact_from_stock", "uids": ["u1", "u2"]})

    web_db.refresh(typed), web_db.refresh(empty)
    assert typed.fact_at_date == 3              # не тронут
    assert empty.fact_at_date == 4              # подставлен остаток ЦС


def test_bulk_future_date_is_refused(logged_in_client, web_db):
    product = _product(web_db)
    tomorrow = (now_utc().date() + timedelta(days=1)).isoformat()

    logged_in_client.post("/products/bulk", data={
        "action": "set_base_date", "uids": ["u1"], "date_value": tomorrow})

    web_db.refresh(product)
    assert product.offset_base_date is None


# ------------------------------------------------ фильтр незавершённых

def test_unfinished_filter_finds_rows_waiting_for_1c(logged_in_client, web_db):
    """На каталоге в 152 тысячи SKU недоделанные строки иначе не найти."""
    _product(web_db, uid="u1")
    _product(web_db, uid="u2")
    logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})

    r = logged_in_client.get("/products/rows?only_unfinished=true")

    assert "row-u1" in r.text
    assert "row-u2" not in r.text


def test_unfinished_filter_finds_rows_without_a_fact(logged_in_client, web_db):
    _snapshot(web_db, [("u1", 10), ("u2", 4)])
    _product(web_db, uid="u1")
    _product(web_db, uid="u2")
    logged_in_client.post("/products/bulk", data={
        "action": "set_base_date", "uids": ["u1", "u2"], "date_value": "2026-08-07"})
    logged_in_client.post("/products/u1/fact", data={"value": "8"})

    r = logged_in_client.get("/products/rows?only_unfinished=true")

    assert "row-u2" in r.text
    assert "row-u1" not in r.text


def test_untouched_rows_are_not_called_unfinished(logged_in_client, web_db):
    """Товар, которому дату не задавали вовсе, в незавершённые не попадает —
    иначе фильтр вернул бы весь каталог и был бы бесполезен."""
    _product(web_db, uid="u1")

    r = logged_in_client.get("/products/rows?only_unfinished=true")

    assert "row-u1" not in r.text


# ------------------------------------------------ Excel

def _xlsx(headers, rows) -> bytes:
    import io
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(client, payload):
    return client.post("/products/import", files={"file": (
        "p.xlsx", payload,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


def _export_row(client):
    import io
    from openpyxl import load_workbook

    r = client.get("/products/export")
    ws = load_workbook(io.BytesIO(r.content)).active
    headers = [c.value for c in ws[1]]
    return headers, dict(zip(headers, [c.value for c in ws[2]]))


def test_export_carries_the_whole_calculation(logged_in_client, web_db):
    """В файле должно быть видно всё, из чего вышел порог, — иначе оператор не
    сможет ни проверить цифру, ни осмысленно её поправить."""
    _snapshot(web_db, [("u1", 10)])
    _product(web_db, reserve=2)
    logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})
    logged_in_client.post("/products/u1/fact", data={"value": "8"})

    headers, values = _export_row(logged_in_client)

    assert "Дата расчёта" in headers and "Остаток ЦС на дату" in headers
    assert "Факт на дату" in headers
    assert values["Дата расчёта"] == "2026-08-07"
    assert values["Остаток ЦС на дату"] == 10
    assert values["Факт на дату"] == 8
    assert values["Порог трансляции"] == 4


def test_import_sets_the_date_and_the_fact(logged_in_client, web_db):
    """Главный сценарий массового ввода: выгрузили, проставили факт тысяче строк
    в Excel, залили обратно."""
    _snapshot(web_db, [("u1", 10)])
    product = _product(web_db, reserve=2)

    _upload(logged_in_client, _xlsx(
        ["ID_1С", "Дата расчёта", "Резерв", "Факт на дату"],
        [["u1", "2026-08-07", 2, 8]]))

    web_db.expire_all()
    product = web_db.query(Product).first()
    assert product.offset_base_date == DAY
    assert product.offset_base_stock == 10
    assert product.broadcast_offset == 4


def test_import_reads_a_date_cell_as_a_date(logged_in_client, web_db):
    """Excel сам превращает «2026-08-07» в дату при правке — файл вернётся с
    датой в ячейке, а не со строкой, и это не должно ломать импорт."""
    from datetime import datetime

    _snapshot(web_db, [("u1", 10)])
    _product(web_db, reserve=0)

    _upload(logged_in_client, _xlsx(
        ["ID_1С", "Дата расчёта", "Факт на дату"],
        [["u1", datetime(2026, 8, 7), 6]]))

    web_db.expire_all()
    assert web_db.query(Product).first().offset_base_date == DAY


def test_import_ignores_the_threshold_but_says_so(logged_in_client, web_db):
    """Порог у товара с датой — расчётный. Принять его из файла значило бы
    завести второй источник правды; промолчать — обмануть оператора, он решил
    бы, что правка применилась."""
    _account(web_db)
    _snapshot(web_db, [("u1", 10)])
    product = _product(web_db, reserve=2)
    logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})
    logged_in_client.post("/products/u1/fact", data={"value": "8"})

    # Ответ на загрузку — это уже перерисованная страница: флеш снимается ею,
    # а не следующим запросом.
    r = _upload(logged_in_client, _xlsx(["ID_1С", "Порог трансляции"], [["u1", 99]]))

    web_db.expire_all()
    assert web_db.query(Product).first().broadcast_offset == 4      # не 99
    assert "правьте «Факт на дату»" in r.text


def test_a_plain_round_trip_does_not_complain(logged_in_client, web_db):
    """Обычный случай: выгрузили и залили обратно, ничего не меняя. Порог в файле
    совпадает с расчётным — ругаться не на что, иначе каждый круг давал бы
    ложную ошибку и оператор перестал бы читать сообщения."""
    _account(web_db)
    _snapshot(web_db, [("u1", 10)])
    _product(web_db, reserve=2)
    logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})
    logged_in_client.post("/products/u1/fact", data={"value": "8"})

    r = _upload(logged_in_client, _xlsx(
        ["ID_1С", "Дата расчёта", "Остаток ЦС на дату", "Резерв", "Факт на дату",
         "Порог трансляции"],
        [["u1", "2026-08-07", 10, 2, 8, 4]]))

    assert "Ошибок" not in r.text
    web_db.expire_all()
    assert web_db.query(Product).first().broadcast_offset == 4


def test_import_still_honours_a_hand_typed_threshold_without_a_date(logged_in_client, web_db):
    """Товары, настроенные до этой правки, продолжают править через Excel
    по-старому: там порог живёт как введённое руками число."""
    _product(web_db)

    _upload(logged_in_client, _xlsx(["ID_1С", "Порог трансляции"], [["u1", 11]]))

    web_db.expire_all()
    assert web_db.query(Product).first().broadcast_offset == 11


def test_import_refuses_a_future_date(logged_in_client, web_db):
    product = _product(web_db)
    tomorrow = (now_utc().date() + timedelta(days=1)).isoformat()

    _upload(logged_in_client, _xlsx(["ID_1С", "Дата расчёта"], [["u1", tomorrow]]))

    web_db.expire_all()
    assert web_db.query(Product).first().offset_base_date is None


def test_import_recomputes_once_after_all_three_values(logged_in_client, web_db):
    """Дата, бронь и факт приезжают одной строкой. Пересчитать порог надо после
    всех трёх — иначе он посчитается по половине данных."""
    _snapshot(web_db, [("u1", 10)])
    _product(web_db, reserve=0)

    _upload(logged_in_client, _xlsx(
        ["ID_1С", "Дата расчёта", "Резерв", "Факт на дату"],
        [["u1", "2026-08-07", 5, 8]]))

    web_db.expire_all()
    product = web_db.query(Product).first()
    assert product.reserve == 5
    assert product.broadcast_offset == 7        # 10 − (8 − 5), а не 10 − 8


def test_moving_the_date_clears_the_fact_on_the_page(logged_in_client, web_db):
    """Со стороны страницы то же правило: факт всегда «на дату». Перенесли дату —
    старое пересчитанное количество к новому числу отношения не имеет, и молча
    считать порог по нему нельзя."""
    _snapshot(web_db, [("u1", 10)])
    _snapshot(web_db, [("u1", 12)], day=date(2026, 9, 1))
    product = _product(web_db, reserve=2)
    logged_in_client.post("/products/u1/base-date", data={"value": "2026-08-07"})
    logged_in_client.post("/products/u1/fact", data={"value": "8"})

    logged_in_client.post("/products/u1/base-date", data={"value": "2026-09-01"})

    web_db.refresh(product)
    assert product.fact_at_date is None
    assert product.offset_base_stock == 12
    assert product.broadcast_offset == 2        # сводится к брони, пока факт не введён


def test_bulk_date_reads_the_snapshot_once(logged_in_client, web_db):
    """Массовая простановка даты не должна ходить в базу за каждым товаром: на
    каталоге в 152 тысячи SKU это минуты ожидания и лишняя нагрузка на боевую
    базу. Считаем запросы — они не должны расти вместе с числом строк."""
    from sqlalchemy import event
    from app.database import engine

    rows = [(f"u{i:03d}", 10 + i) for i in range(20)]
    _snapshot(web_db, rows)
    for uid, _ in rows:
        _product(web_db, uid=uid, reserve=1)

    seen = []
    listener = lambda conn, cur, stmt, *a: seen.append(stmt)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        logged_in_client.post("/products/bulk", data={
            "action": "set_base_date", "uids": [uid for uid, _ in rows],
            "date_value": "2026-08-07"})
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    selects = [q for q in seen if q.lstrip().upper().startswith("SELECT")
               and "stock_date" in q.lower()]
    assert len(selects) <= 4, f"чтений снимка {len(selects)} на 20 товаров — растёт со строками"
    web_db.expire_all()
    p = web_db.query(Product).filter(Product.uid_1c == "u005").first()
    assert p.offset_base_stock == 15 and p.broadcast_offset == 1

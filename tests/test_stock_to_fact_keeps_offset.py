"""«Записать остаток ЦС на дату» не затирает порог, поставленный человеком.

23.09 на бою по товару 27643 (`dcaedaa6-…`), по журналу с точностью до минуты:

  15:07:14  дата расчёта сдвинута назад, 07.08 → 06.07;
  15:07:51  механизм удержания СРАБОТАЛ — порог 11 сохранён, под него подобран
            факт 32, на площадки ушло 11 из 22 («порог≈11» в очереди);
  15:10:44  «Записать остаток ЦС на дату» по 44 отмеченным — факт стал равен
            учёту (43), расхождение 0, порог 0, наружу уехало 22;
  15:12 / 15:41 / 15:47  то же самое по 500 и дважды по 849 строкам.

Реальный склад по этому SKU меньше учёта на 11 — это и показал физический
пересчёт. То есть на обе площадки уехало вдвое больше, чем есть: прямой
оверселл, сразу по всему отбору и без единого слова в сообщении.

Соседняя ветка того же цикла (`fact_from_stock`) это правило соблюдала и прямо
о нём говорила — «затирать введённые руками цифры массовой кнопкой нельзя». Две
соседние ветки утверждали противоположное, и выиграла та, у которой не было
теста.
"""

from datetime import date, datetime

from app.models import (Product, StockDateRow, StockDateSnapshot,
                        StockDateStatus)


def _product(web_db, uid, **kw):
    fields = dict(article="27643", name="Свитшот", size="L", color="LACIVERT/RED",
                  stock_on_hand=22, reserve=0)
    fields.update(kw)
    product = Product(uid_1c=uid, **fields)
    web_db.add(product)
    web_db.commit()
    return product


def _fresh(web_db, uid):
    web_db.expire_all()
    return web_db.query(Product).filter(Product.uid_1c == uid).first()


# --------------------------------------------------- порог человека не трогаем

def test_a_threshold_set_by_a_recount_survives_the_button(logged_in_client, web_db):
    """Ровно случай 27643: факт 32 при учёте 43, порог 11."""
    _product(web_db, "u1", offset_base_date=date(2026, 7, 6),
             offset_base_stock=43, fact_at_date=32, broadcast_offset=11)

    logged_in_client.post("/products/bulk",
                          data={"action": "stock_to_fact", "uids": ["u1"]})

    product = _fresh(web_db, "u1")
    assert product.fact_at_date == 32, "факт пересчёта склада затёрт учётом 1С"
    assert product.broadcast_offset == 11, "порог схлопнут — наружу уйдёт больше, чем есть"


def test_a_threshold_typed_by_hand_survives_too(logged_in_client, web_db):
    """Порог, не равный броне, — тоже работа человека, даже без факта."""
    _product(web_db, "u1", reserve=2, offset_base_date=date(2026, 7, 6),
             offset_base_stock=43, fact_at_date=None, broadcast_offset=9)

    logged_in_client.post("/products/bulk",
                          data={"action": "stock_to_fact", "uids": ["u1"]})

    assert _fresh(web_db, "u1").broadcast_offset == 9


def test_the_message_says_how_many_rows_kept_their_threshold(logged_in_client, web_db):
    """Пропустить молча — тот же дефект, только с другой стороны.

    Оператор решил бы, что кнопка прошлась по всем, и не узнал бы, что часть
    строк осталась с прежним порогом."""
    _product(web_db, "u1", offset_base_date=date(2026, 7, 6),
             offset_base_stock=43, fact_at_date=32, broadcast_offset=11)

    page = logged_in_client.post("/products/bulk",
                                 data={"action": "stock_to_fact", "uids": ["u1"]},
                                 follow_redirects=True)

    assert "Порог сохранён у 1" in page.text
    assert "Сбросить порог" in page.text, "не сказано, как всё-таки схлопнуть"


# ------------------------------------------- а где порога нет, кнопка работает

def test_a_row_without_a_threshold_is_filled_as_before(logged_in_client, web_db):
    """Ради этого кнопка и нужна: настройка пачкой не должна сломаться."""
    _product(web_db, "u1", offset_base_date=date(2026, 7, 6),
             offset_base_stock=43, fact_at_date=None, broadcast_offset=None)

    logged_in_client.post("/products/bulk",
                          data={"action": "stock_to_fact", "uids": ["u1"]})

    product = _fresh(web_db, "u1")
    assert product.fact_at_date == 43
    assert product.broadcast_offset == 0


def test_a_threshold_equal_to_the_reserve_is_not_someones_work(logged_in_client, web_db):
    """Порог, равный броне, формула даёт сама — держаться за него нечего.

    Иначе кнопка перестала бы работать почти везде: у строки без факта порог
    всегда равен броне."""
    _product(web_db, "u1", reserve=3, offset_base_date=date(2026, 7, 6),
             offset_base_stock=43, fact_at_date=None, broadcast_offset=3)

    logged_in_client.post("/products/bulk",
                          data={"action": "stock_to_fact", "uids": ["u1"]})

    product = _fresh(web_db, "u1")
    assert product.fact_at_date == 43
    assert product.broadcast_offset == 3        # 43 − (43 − 3)


def test_resetting_the_threshold_first_lets_the_button_through(logged_in_client, web_db):
    """Схлопнуть можно, сказав это вслух: «Сбросить порог», затем кнопка.

    «Сбросить порог» снимает расчёт целиком — и порог, и дату, и все три числа, —
    поэтому дату кнопке приходится задать заново, а снимок 1С на неё должен быть
    готов. Он тут и заводится: без него строка честно уходит в «ждём выгрузку»."""
    _product(web_db, "u1", offset_base_date=date(2026, 7, 6),
             offset_base_stock=43, fact_at_date=32, broadcast_offset=11)
    snapshot = StockDateSnapshot(snapshot_date=date(2026, 7, 6),
                                 status=StockDateStatus.done, rows_count=1)
    web_db.add(snapshot)
    web_db.flush()
    web_db.add(StockDateRow(snapshot_id=snapshot.id, uid_1c="u1", quantity=43))
    web_db.commit()

    logged_in_client.post("/products/bulk", data={"action": "clear_offset", "uids": ["u1"]})
    logged_in_client.post("/products/bulk",
                          data={"action": "stock_to_fact", "uids": ["u1"],
                                "date_value": "2026-07-06"})

    product = _fresh(web_db, "u1")
    assert product.fact_at_date == 43
    assert product.broadcast_offset == 0


# ------------------------------------------------- одно правило на двух хозяев

def test_the_date_move_and_the_button_ask_the_same_question():
    """`offset_is_established` спрашивают ОБА, и разойтись им нельзя.

    Сдвиг даты назад решает по нему, что удерживать; кнопка — чего не затирать.
    Разойдись они, один механизм сохранял бы порог, а второй тут же схлопывал:
    ровно то, что и произошло 23.09, когда условие было только у первого."""
    import inspect

    from app import offset_base
    from app.routers import products

    assert "offset_is_established(product)" in inspect.getsource(offset_base.set_base_date)
    assert "offset_is_established(p)" in inspect.getsource(products.bulk_edit)

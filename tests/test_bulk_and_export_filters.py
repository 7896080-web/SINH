"""Массовый выбор и выгрузка подчиняются тому, что оператор видит на экране.

Три дефекта одной природы — интерфейс и сервер расходились в том, какой отбор
считать текущим:

1. Ссылка «Экспорт в Excel» собиралась при загрузке страницы, а фильтры
   применяются живьём (htmx подменяет только таблицу). Оператор менял отбор,
   видел новые строки, жал «Экспорт» — и получал файл по ПРЕЖНЕМУ фильтру.
2. Фильтр «только незавершённый расчёт» ссылка передавала, а эндпоинт выгрузки
   его не принимал: FastAPI молча отбрасывал неизвестный параметр, и в файл
   уходил весь каталог.
3. Галочки отмечают только видимые 300 строк. На каталоге в 152 тысячи SKU
   «отметить все» после фильтра означало бы «первые 300 из подходящих», и
   оператор решил бы, что обработал весь отбор.
"""
import io
from datetime import date

from openpyxl import load_workbook

from app.models import Platform, PlatformAccount, Product


def _product(web_db, uid, name="Товар", size="M", broadcast=True, **kw):
    p = Product(uid_1c=uid, article=f"A-{uid}", name=name, size=size, stock_on_hand=10,
                reserve=0, broadcast_enabled=broadcast, **kw)
    web_db.add(p)
    web_db.commit()
    return p


def _account(web_db):
    a = PlatformAccount(platform=Platform.wb, name="WB-1", warehouse_id="wh")
    web_db.add(a)
    web_db.commit()
    return a


def _rows_of(resp):
    ws = load_workbook(io.BytesIO(resp.content)).active
    return [r[0].value for r in ws.iter_rows(min_row=2) if r[0].value]


# ------------------------------------------------ выгрузка слушается фильтров

def test_export_honours_the_unfinished_filter(logged_in_client, web_db):
    """Тот самый молча отброшенный параметр."""
    _product(web_db, "u1")
    _product(web_db, "u2", offset_base_date=date(2026, 8, 7))   # незавершённый расчёт

    uids = _rows_of(logged_in_client.get("/products/export?only_unfinished=true"))

    assert uids == ["u2"]


def test_export_honours_the_search(logged_in_client, web_db):
    _product(web_db, "u1", name="Платье")
    _product(web_db, "u2", name="Джинсы")

    assert _rows_of(logged_in_client.get("/products/export?q=Джинс")) == ["u2"]


def test_export_honours_hide_size_u(logged_in_client, web_db):
    _product(web_db, "u1", size="U")
    _product(web_db, "u2", size="M")

    assert _rows_of(logged_in_client.get("/products/export?hide_size_u=true")) == ["u2"]


def test_export_button_lives_inside_the_filters_form(logged_in_client, web_db):
    """Гарантия от повторения: пока кнопка — часть формы фильтров, браузер
    собирает значения полей в момент нажатия, и разойтись им не с чем. Отдельная
    ссылка со значениями, подставленными при рендере, сюда вернуться не должна."""
    body = logged_in_client.get("/products").text

    assert 'formaction="/products/export"' in body
    assert 'href="/products/export' not in body


# ------------------------------------------------ отметить все

def test_select_all_checkbox_is_there(logged_in_client, web_db):
    _product(web_db, "u1")

    assert 'id="pr-all"' in logged_in_client.get("/products").text


def test_select_all_means_the_whole_selection(logged_in_client, web_db):
    """Чекбокс в шапке — это и есть «весь отбор»: он сам уходит в форму как
    all_filtered. Отдельного переключателя нет, чтобы не было двух смыслов у
    одного действия."""
    _product(web_db, "u1")

    body = logged_in_client.get("/products").text

    assert 'id="pr-all"' in body and 'name="all_filtered"' in body
    assert "весь текущий отбор" in body


def test_bulk_applies_to_everything_matching_the_filter(logged_in_client, web_db):
    """Ключевой сценарий: строк больше, чем на странице, оператор просит
    применить ко всему отбору."""
    from app.routers.products import PAGE_LIMIT

    for i in range(PAGE_LIMIT + 5):
        _product(web_db, f"u{i:04d}")

    logged_in_client.post("/products/bulk", data={
        "action": "broadcast_off", "all_filtered": "true", "q": ""})

    web_db.expire_all()
    assert web_db.query(Product).filter(Product.broadcast_enabled.is_(True)).count() == 0


def test_filter_wide_bulk_respects_the_filter(logged_in_client, web_db):
    """И не трогает то, что под фильтр не попало."""
    _product(web_db, "u1", name="Платье")
    _product(web_db, "u2", name="Джинсы")

    logged_in_client.post("/products/bulk", data={
        "action": "broadcast_off", "all_filtered": "true", "q": "Джинс"})

    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first().broadcast_enabled is True
    assert web_db.query(Product).filter(Product.uid_1c == "u2").first().broadcast_enabled is False


def test_checked_rows_still_work_without_the_switch(logged_in_client, web_db):
    _product(web_db, "u1")
    _product(web_db, "u2")

    logged_in_client.post("/products/bulk", data={
        "action": "broadcast_off", "uids": ["u1"], "q": ""})

    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first().broadcast_enabled is False
    assert web_db.query(Product).filter(Product.uid_1c == "u2").first().broadcast_enabled is True


def test_nothing_selected_is_refused(logged_in_client, web_db):
    _account(web_db)          # иначе страница перекроет флеш своим «добавьте кабинет»
    _product(web_db, "u1")

    r = logged_in_client.post("/products/bulk", data={"action": "broadcast_off", "q": ""})

    assert "Не выбрано ни одной строки" in r.text
    web_db.expire_all()
    assert web_db.query(Product).first().broadcast_enabled is True


def test_a_huge_filter_is_refused_with_a_reason(logged_in_client, web_db, monkeypatch):
    """Правка всего каталога разом надолго заняла бы SQLite, в которую в это же
    время пишет планировщик. Отказ должен объяснять, что делать."""
    import app.routers.products as products_router

    monkeypatch.setattr(products_router, "BULK_LIMIT", 1)
    _account(web_db)
    _product(web_db, "u1")
    _product(web_db, "u2")

    r = logged_in_client.post("/products/bulk", data={
        "action": "broadcast_off", "all_filtered": "true", "q": ""})

    assert "больше предела" in r.text
    web_db.expire_all()
    assert web_db.query(Product).filter(Product.broadcast_enabled.is_(True)).count() == 2


def test_blocked_filter_refuses_filter_wide_edit(logged_in_client, web_db):
    """«Только те, где уходит 0» считается построчно по лестнице приоритетов уже
    в Python — SQL-отбор для него приблизительный, и массово править по нему
    нельзя: задело бы не те строки."""
    _account(web_db)
    _product(web_db, "u1")

    r = logged_in_client.post("/products/bulk", data={
        "action": "broadcast_off", "all_filtered": "true",
        "only_blocked": "true", "q": ""})

    assert "отметьте нужные строки" in r.text.lower()
    web_db.expire_all()
    assert web_db.query(Product).first().broadcast_enabled is True


# ------------------------------------------------ возврат сохраняет отбор

def test_after_a_bulk_edit_the_filter_survives(logged_in_client, web_db):
    """Оператор отобрал незавершённые, поправил — и должен остаться в том же
    отборе, а не на полном списке, где отобранного уже не найти."""
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7))

    r = logged_in_client.post("/products/bulk", data={
        "action": "broadcast_off", "uids": ["u1"], "q": "", "only_unfinished": "true"},
        follow_redirects=False)

    assert "only_unfinished=true" in r.headers["location"]


# ------------------------------------------------ под фильтром виден весь отбор

def test_a_filter_shows_everything_it_matched(logged_in_client, web_db):
    """Оператор сужает список именно затем, чтобы увидеть его целиком. Отдавать
    первые 300 из отбора значит заставить его гадать, что осталось за краем, —
    и «выбрать все» применится к тому, чего он не видел."""
    from app.routers.products import PAGE_LIMIT

    for i in range(PAGE_LIMIT + 40):
        _product(web_db, f"u{i:04d}", name="Платье")
    _product(web_db, "other", name="Джинсы")

    body = logged_in_client.get("/products/rows?q=Платье").text

    assert body.count('name="uids"') == PAGE_LIMIT + 40
    assert "other" not in body


def test_without_filters_the_page_stays_a_shop_window(logged_in_client, web_db):
    """Без отбора смысла в полном списке нет: «первые N из 152 тысяч по алфавиту»
    ничего не значат, а строить их браузеру дорого."""
    from app.routers.products import PAGE_LIMIT

    for i in range(PAGE_LIMIT + 40):
        _product(web_db, f"u{i:04d}")

    body = logged_in_client.get("/products/rows").text

    assert body.count('name="uids"') == PAGE_LIMIT


def test_the_page_says_when_even_the_filter_is_truncated(logged_in_client, web_db, monkeypatch):
    """Если отбор всё равно не помещается, оператор обязан это увидеть: правка
    «по отбору» затронет и те строки, которых на экране нет."""
    import app.routers.products as products_router

    monkeypatch.setattr(products_router, "FILTERED_LIMIT", 5)
    for i in range(9):
        _product(web_db, f"u{i:04d}", name="Платье")

    body = logged_in_client.get("/products/rows?q=Платье").text

    assert "тоже будут изменены" in body


# ------------------------------------------------ «записать остаток ЦС на дату»

def _snapshot(web_db, rows, day=date(2026, 8, 7)):
    from app.models import StockDateRow, StockDateSnapshot, StockDateStatus

    snap = StockDateSnapshot(snapshot_date=day, status=StockDateStatus.done, rows_count=len(rows))
    web_db.add(snap)
    web_db.commit()
    web_db.refresh(snap)
    for uid, qty in rows:
        web_db.add(StockDateRow(snapshot_id=snap.id, uid_1c=uid, quantity=qty))
    web_db.commit()
    return snap


def test_one_click_does_the_whole_calculation(logged_in_client, web_db):
    """Самый частый случай: цифра 1С верна, порог надо просто закрепить на дату.
    Раньше это было два действия — поставить дату, потом «факт = остаток ЦС»."""
    _account(web_db)
    _snapshot(web_db, [("u1", 14)])
    product = _product(web_db, "u1")
    product.reserve = 2
    web_db.commit()

    logged_in_client.post("/products/bulk", data={
        "action": "stock_to_fact", "uids": ["u1"], "date_value": "2026-08-07", "q": ""})

    web_db.expire_all()
    p = web_db.query(Product).first()
    assert p.offset_base_stock == 14
    assert p.fact_at_date == 14          # цифра подтверждена, а не «не введена»
    assert p.broadcast_offset == 2       # порог сводится к брони


def test_it_uses_the_date_already_set_when_no_new_one_given(logged_in_client, web_db):
    _account(web_db)
    _snapshot(web_db, [("u1", 14)])
    _product(web_db, "u1")
    logged_in_client.post("/products/bulk", data={
        "action": "set_base_date", "uids": ["u1"], "date_value": "2026-08-07", "q": ""})

    logged_in_client.post("/products/bulk", data={
        "action": "stock_to_fact", "uids": ["u1"], "q": ""})

    web_db.expire_all()
    assert web_db.query(Product).first().fact_at_date == 14


def test_rows_still_waiting_for_1c_are_counted_as_skipped(logged_in_client, web_db):
    """Записывать нечего, пока 1С не ответила. Это не ошибка — строка досчитается
    сама, — но промолчать нельзя: оператор решит, что обработаны все."""
    _account(web_db)
    _product(web_db, "u1")

    r = logged_in_client.post("/products/bulk", data={
        "action": "stock_to_fact", "uids": ["u1"], "date_value": "2026-08-07", "q": ""})

    assert "Пропущено 1" in r.text
    web_db.expire_all()
    assert web_db.query(Product).first().fact_at_date is None


# ------------------------------------------------ индикатор готовности строки

def test_a_row_without_a_date_says_the_calculation_has_not_started(logged_in_client, web_db):
    _product(web_db, "u1")

    assert "расчёт не начат" in logged_in_client.get("/products/rows").text


def test_a_row_waiting_for_1c_says_so(logged_in_client, web_db):
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7))

    assert "ждём 1С" in logged_in_client.get("/products/rows").text


def test_a_row_with_no_fact_is_not_called_done(logged_in_client, web_db):
    """Пустой факт выглядит одинаково и когда его не вводили, и когда решили, что
    учёт 1С верен. Пока человек цифру не подтвердил — строка не обработана."""
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7), offset_base_stock=14)

    body = logged_in_client.get("/products/rows").text

    assert "нужен факт" in body
    assert "готово" not in body


def test_a_row_with_a_threshold_but_no_catch_up_is_not_ready(logged_in_client, web_db):
    """Порог посчитан — но отгрузки площадок за период в 1С ещё не проведены,
    остаток ЦС завышен. Включать трансляцию рано: уедет число больше реального.
    Раньше такая строка называлась «готово», и это было опасное враньё."""
    _product(web_db, "u1", broadcast=False, offset_base_date=date(2026, 8, 7),
             offset_base_stock=14, fact_at_date=14)

    body = logged_in_client.get("/products/rows").text

    assert "нужен расчёт" in body
    assert "можно включать трансляцию" not in body


def test_a_row_after_the_catch_up_says_it_is_ready(logged_in_client, web_db):
    from app.timeutils import now_utc

    _product(web_db, "u1", broadcast=False, offset_base_date=date(2026, 8, 7),
             offset_base_stock=14, fact_at_date=14, recalc_done_at=now_utc())

    body = logged_in_client.get("/products/rows").text

    assert "актуализирован" in body
    assert "перемещения в 1С созданы, можно включать трансляцию" in body


def test_an_already_broadcasting_row_is_not_told_to_switch_on(logged_in_client, web_db):
    from app.timeutils import now_utc

    _product(web_db, "u1", broadcast=True, offset_base_date=date(2026, 8, 7),
             offset_base_stock=14, fact_at_date=14, recalc_done_at=now_utc())

    body = logged_in_client.get("/products/rows").text

    assert "актуализирован" in body
    assert "можно включать трансляцию" not in body


# ------------------------------------------------ кнопка «Расчёт»

def test_recalc_creates_a_job_instead_of_working_in_the_request(logged_in_client, web_db):
    """По каждому товару надо опросить каждый его кабинет по историческим
    заказам — в запросе это минуты. Веб только заводит задание, работает воркер."""
    from app.models import RecalcItem, RecalcJob

    _account(web_db)
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7))

    r = logged_in_client.post("/products/bulk", data={
        "action": "recalc", "uids": ["u1"], "q": ""})

    assert "Расчёт запущен" in r.text
    assert web_db.query(RecalcJob).count() == 1
    assert [i.uid_1c for i in web_db.query(RecalcItem).all()] == ["u1"]


def test_recalc_skips_products_without_a_date_and_says_so(logged_in_client, web_db):
    from app.models import RecalcItem

    _account(web_db)
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7))
    _product(web_db, "u2")

    r = logged_in_client.post("/products/bulk", data={
        "action": "recalc", "uids": ["u1", "u2"], "q": ""})

    assert "Пропущено 1" in r.text
    assert [i.uid_1c for i in web_db.query(RecalcItem).all()] == ["u1"]


def test_recalc_refuses_when_nothing_has_a_date(logged_in_client, web_db):
    from app.models import RecalcJob

    _account(web_db)
    _product(web_db, "u1")

    r = logged_in_client.post("/products/bulk", data={
        "action": "recalc", "uids": ["u1"], "q": ""})

    assert "не задана дата расчёта" in r.text
    assert web_db.query(RecalcJob).count() == 0


def test_a_second_job_is_not_started_while_one_runs(logged_in_client, web_db):
    """Два задания шли бы по одним и тем же товарам и дублировали обращения к
    площадкам."""
    from app.models import RecalcJob

    _account(web_db)
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7))
    logged_in_client.post("/products/bulk", data={"action": "recalc", "uids": ["u1"], "q": ""})

    r = logged_in_client.post("/products/bulk", data={
        "action": "recalc", "uids": ["u1"], "q": ""})

    assert "уже идёт" in r.text
    assert web_db.query(RecalcJob).count() == 1


def test_the_page_shows_the_progress(logged_in_client, web_db):
    _account(web_db)
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7))
    logged_in_client.post("/products/bulk", data={"action": "recalc", "uids": ["u1"], "q": ""})

    body = logged_in_client.get("/products").text

    assert "Расчёт остатков" in body
    assert "страницу можно" in body      # работает планировщик, не браузер


def test_the_progress_fragment_keeps_polling_itself(logged_in_client, web_db):
    """Обёртка с опросом — часть фрагмента: htmx подменяет элемент целиком, и
    будь она снаружи, первая же подмена унесла бы hx-trigger, а прогресс замер бы
    на первом значении."""
    _account(web_db)
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7))
    logged_in_client.post("/products/bulk", data={"action": "recalc", "uids": ["u1"], "q": ""})

    fragment = logged_in_client.get("/products/recalc-progress").text

    assert 'hx-trigger="every 3s"' in fragment
    assert 'id="recalc-box"' in fragment

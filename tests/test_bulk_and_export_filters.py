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


def _with_cabinet(web_db, uid, covered=True, **kw):
    """Товар с отмеченным кабинетом. Состояния расчёта проверяются именно на
    таком: без кабинета строка честно пишет «не выбран кабинет» и всё остальное
    становится неважным — заказы спрашивать негде и транслировать некуда.

    `covered` — попал ли этот кабинет в прошедший расчёт. По умолчанию да: расчёт
    поднимает заказы с отмеченных кабинетов, так что «прошёл расчёт» и «кабинет
    им покрыт» — обычно одно и то же событие. Ложь означает «кабинет отметили
    ПОСЛЕ расчёта» — по нему заказы не поднимали, и остаток не сверен.
    """
    from app.models import SyncSetting

    account = _account(web_db)
    product = _product(web_db, uid, **kw)
    web_db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))
    if covered and kw.get("recalc_done_at") is not None:
        product.recalc_account_ids = str(account.id)
    web_db.commit()
    return product


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
    _with_cabinet(web_db, "u1")

    assert "расчёт не начат" in logged_in_client.get("/products/rows").text


def test_a_row_waiting_for_1c_says_so(logged_in_client, web_db):
    _with_cabinet(web_db, "u1", offset_base_date=date(2026, 8, 7))

    assert "ждём 1С" in logged_in_client.get("/products/rows").text


def test_a_row_with_no_fact_is_not_called_done(logged_in_client, web_db):
    """Пустой факт выглядит одинаково и когда его не вводили, и когда решили, что
    учёт 1С верен. Пока человек цифру не подтвердил — строка не обработана."""
    _with_cabinet(web_db, "u1", offset_base_date=date(2026, 8, 7), offset_base_stock=14)

    body = logged_in_client.get("/products/rows").text

    assert "нужен факт" in body
    assert "готово" not in body


def test_a_row_with_a_threshold_but_no_catch_up_is_not_ready(logged_in_client, web_db):
    """Порог посчитан — но отгрузки площадок за период в 1С ещё не проведены,
    остаток ЦС завышен. Включать трансляцию рано: уедет число больше реального.
    Раньше такая строка называлась «готово», и это было опасное враньё."""
    _with_cabinet(web_db, "u1", broadcast=False, offset_base_date=date(2026, 8, 7),
                  offset_base_stock=14, fact_at_date=14)

    body = logged_in_client.get("/products/rows").text

    assert "нужен расчёт" in body
    assert "можно включать трансляцию" not in body


def test_a_row_after_the_catch_up_says_it_is_ready(logged_in_client, web_db):
    from app.timeutils import now_utc

    _with_cabinet(web_db, "u1", broadcast=False, offset_base_date=date(2026, 8, 7),
                  offset_base_stock=14, fact_at_date=14, recalc_done_at=now_utc())

    body = logged_in_client.get("/products/rows").text

    assert "актуализирован" in body
    assert "перемещения в 1С созданы — можно включать трансляцию" in body


def test_an_already_broadcasting_row_is_not_told_to_switch_on(logged_in_client, web_db):
    from app.timeutils import now_utc

    _with_cabinet(web_db, "u1", broadcast=True, offset_base_date=date(2026, 8, 7),
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


def test_a_row_without_cabinets_says_so_first(logged_in_client, web_db):
    """Ни одного отмеченного кабинета: заказы спрашивать негде и транслировать
    некуда. Без этой подписи оператор видел «нужен расчёт», запускал — и получал
    молчаливый пустой проход, а причина оставалась в строке задания, которой на
    странице нет. Найдено на первом живом прогоне."""
    _account(web_db)          # кабинет в системе есть, но для товара НЕ отмечен
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7), offset_base_stock=14,
             fact_at_date=14)

    body = logged_in_client.get("/products/rows").text

    assert "не выбран кабинет" in body
    assert "нужен расчёт" not in body


def test_with_a_cabinet_the_normal_states_come_back(logged_in_client, web_db):
    from app.models import SyncSetting

    account = _account(web_db)
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7), offset_base_stock=14,
             fact_at_date=14)
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    body = logged_in_client.get("/products/rows").text

    assert "нужен расчёт" in body
    assert "не выбран кабинет" not in body


def test_enter_in_the_search_does_not_download_a_file(logged_in_client, web_db):
    """К форме фильтров привязана кнопка экспорта, и она единственная submit —
    значит Enter браузер трактовал как её нажатие: оператор вводил артикул, а
    вместо поиска сам собой скачивался файл выгрузки. Поиск живой (htmx по
    keyup), Enter в нём не нужен вовсе."""
    _product(web_db, "u1")

    body = logged_in_client.get("/products").text
    # элемент целиком, а не строка файла: атрибуты переносятся
    search = body.split('id="q"', 1)[1].split(">", 1)[0]

    assert "event.preventDefault()" in search


def test_export_and_import_stand_together(logged_in_client, web_db):
    """Пара кнопок не должна разъезжаться по разным углам страницы: оператор
    искал импорт там, где всегда был экспорт, и решил, что импорт пропал."""
    _product(web_db, "u1")

    body = logged_in_client.get("/products").text
    toolbar = body.split('<div class="toolbar">', 1)[1].split("</div>", 1)[0]

    assert "Экспорт в Excel" in toolbar
    assert "Импорт из Excel" in toolbar


def test_the_export_button_still_follows_the_live_filters(logged_in_client, web_db):
    """Кнопка стоит ВНЕ формы фильтров, но привязана к ней атрибутом form= —
    браузер соберёт текущие значения полей, а не те, что были при загрузке."""
    _product(web_db, "u1")

    body = logged_in_client.get("/products").text

    assert 'form="pr-filters"' in body
    assert 'id="pr-filters"' in body


def test_a_finished_job_tells_the_operator_to_refresh(logged_in_client, web_db):
    """Карточка прогресса обновляет себя опросом, а таблица под ней — нет: строки
    остаются от момента загрузки и показывают состояние ДО расчёта. Оператор
    видел «завершён, 7 заказов» и рядом «нужен расчёт» в строке — и решил, что
    расчёт не сработал. Найдено на живой работе."""
    from app.models import RecalcJob, RecalcStatus

    _account(web_db)
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7))
    web_db.add(RecalcJob(status=RecalcStatus.done, total=1, processed=1, orders_applied=7))
    web_db.commit()

    fragment = logged_in_client.get("/products/recalc-progress").text

    assert "Обновить страницу" in fragment


def test_a_running_job_does_not_nag_about_refreshing(logged_in_client, web_db):
    """Пока идёт — обновлять нечего, и лишняя строка только отвлекает."""
    from app.models import RecalcJob, RecalcStatus

    _account(web_db)
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7))
    web_db.add(RecalcJob(status=RecalcStatus.running, total=5, processed=2))
    web_db.commit()

    fragment = logged_in_client.get("/products/recalc-progress").text

    assert "Обновить страницу" not in fragment


def test_a_fresh_page_load_does_not_nag(logged_in_client, web_db):
    """На только что открытой странице строки и так свежие."""
    from app.models import RecalcJob, RecalcStatus

    _account(web_db)
    _product(web_db, "u1", offset_base_date=date(2026, 8, 7))
    web_db.add(RecalcJob(status=RecalcStatus.done, total=1, processed=1))
    web_db.commit()

    body = logged_in_client.get("/products").text

    assert "Обновить страницу" not in body


# ------------------------------------------------ прогноз вместо нуля

def test_a_cabinet_shows_what_would_be_sent_after_switching_on(logged_in_client, web_db):
    """Оператор смотрит в колонку кабинета ПЕРЕД включением. «0, потому что
    выключено» не отвечает на его вопрос — ему нужно число, которое уйдёт, если
    нажать «Вкл»."""
    from app.timeutils import now_utc

    product = _with_cabinet(web_db, "u1", broadcast=False,
                            offset_base_date=date(2026, 8, 7), offset_base_stock=30,
                            fact_at_date=58, broadcast_offset=-28,
                            recalc_done_at=now_utc())
    product.stock_on_hand = 18              # реального склада на 28 больше учёта
    web_db.commit()

    body = logged_in_client.get("/products/rows").text

    assert "46" in body                     # 18 − (−28)
    assert "после включения" in body


def test_an_unchecked_cabinet_gets_no_forecast(logged_in_client, web_db):
    """Туда не передают по решению оператора, а не из-за выключателя: включать
    нечего, и прогноз только запутал бы."""
    from app.models import SyncSetting

    account = _account(web_db)
    product = _product(web_db, "u1", broadcast=False)
    product.stock_on_hand = 18
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=False))
    web_db.commit()

    body = logged_in_client.get("/products/rows").text

    assert "не передаётся" in body
    assert "после включения" not in body


def test_a_transmitting_cabinet_shows_the_real_number(logged_in_client, web_db):
    from app.models import SyncSetting

    account = _account(web_db)
    product = _product(web_db, "u1", broadcast=True)
    product.stock_on_hand = 18
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    body = logged_in_client.get("/products/rows").text

    assert "после включения" not in body
    assert "→ 18" in body


# ------------------------------------------------ поле «порог кабинета»

def test_the_cabinet_threshold_is_no_longer_an_editable_box(logged_in_client, web_db):
    """Поле стояло вплотную к расчётному числу и принимало ввод, хотя при заданном
    пороге трансляции ни на что не влияло. В него вписали 46 — количество для
    трансляции, — и оно молча ждало момента, когда порог трансляции сбросят."""
    _with_cabinet(web_db, "u1")

    body = logged_in_client.get("/products/rows").text

    assert "pr-input--tiny" not in body


def test_a_leftover_threshold_is_shown_and_can_be_dropped(logged_in_client, web_db):
    """Скрыть сохранённое значение было бы хуже, чем показать: оно продолжает
    лежать в базе и сработает, если режим сменится."""
    from app.models import SyncSetting

    _with_cabinet(web_db, "u1", offset_base_date=date(2026, 8, 7),
                  offset_base_stock=30, fact_at_date=58, broadcast_offset=-28)
    setting = web_db.query(SyncSetting).filter(SyncSetting.uid_1c == "u1").one()
    setting.min_threshold = 46
    web_db.commit()

    body = logged_in_client.get("/products/rows").text

    assert "порог кабинета 46" in body
    assert "не применяется" in body, "при заданном пороге трансляции он молчит"
    assert "снять" in body


def test_a_threshold_that_really_works_is_not_called_idle(logged_in_client, web_db):
    from app.models import SyncSetting

    _with_cabinet(web_db, "u1")            # без порога трансляции — режим автоматический
    setting = web_db.query(SyncSetting).filter(SyncSetting.uid_1c == "u1").one()
    setting.min_threshold = 5
    web_db.commit()

    body = logged_in_client.get("/products/rows").text

    assert "порог кабинета 5" in body
    assert "не применяется" not in body


def test_a_cabinet_added_after_the_catch_up_makes_the_row_ask_for_a_recount(
        logged_in_client, web_db):
    """«Актуализирован» на такой строке — неправда, а именно на неё оператор
    опирается, когда включает трансляцию."""
    from app.timeutils import now_utc

    _with_cabinet(web_db, "u1", covered=False, offset_base_date=date(2026, 8, 7),
                  offset_base_stock=14, fact_at_date=14, recalc_done_at=now_utc())

    body = logged_in_client.get("/products/rows").text

    assert "нужен пересчёт: добавлен кабинет" in body
    assert "актуализирован" not in body


def test_an_unfinished_row_promises_nothing(logged_in_client, web_db):
    """Строка ZJYM269002 XL: «ждём 1С», а рядом «→ 21 после включения» — число от
    порога, оставшегося с другой даты. Обещать его нельзя: включать ещё рано."""
    _with_cabinet(web_db, "u1", offset_base_date=date(2026, 8, 14),
                  broadcast=False, broadcast_offset=0)

    body = logged_in_client.get("/products/rows").text

    assert "после включения" not in body
    assert "после расчёта" in body


def test_a_leftover_offset_does_not_pretend_to_be_manual(logged_in_client, web_db):
    """Порог остался от прошлой даты — стирать его нельзя, но и называть
    «заданным вручную» неправда: считали его мы, и на другое число."""
    _with_cabinet(web_db, "u1", offset_base_date=date(2026, 8, 14),
                  broadcast=False, broadcast_offset=0)

    body = logged_in_client.get("/products/rows").text

    assert "от прошлого расчёта" in body
    assert "задан вручную" not in body


def test_the_ready_badge_does_not_swallow_the_whole_row(logged_in_client, web_db):
    """Подсказка «перемещения в 1С созданы» жила ВНУТРИ значка, а значок не
    переносится: колонка расчёта растягивалась на всю ширину и выдавливала
    «Размер» и «Цвет» за край таблицы. Теперь подсказка — отдельная строка."""
    from app.timeutils import now_utc

    _with_cabinet(web_db, "u1", broadcast=False, offset_base_date=date(2026, 8, 7),
                  offset_base_stock=14, fact_at_date=14, recalc_done_at=now_utc())

    body = logged_in_client.get("/products/rows").text

    badge = body.split('class="pr-status')[1].split("</div>")[0]
    assert "можно включать трансляцию" not in badge, \
        "длинный текст внутри nowrap-значка ломает ширину колонок"

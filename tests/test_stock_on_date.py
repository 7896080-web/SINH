"""Выгрузка остатков 1С НА ЗАДАННОЕ ЧИСЛО (команда EXPORT_STOCK_ON_DATE).

Оператору нужен ответ на вопрос «сколько этого товара лежало на ЦС такого-то
числа»: разбор расхождения с площадкой, сверка с инвентаризацией, проверка
порога трансляции задним числом.

Главное, что проверяется здесь, — не сама выгрузка, а её ГРАНИЦА: срез за
прошлое число не должен попасть ни в остаток товара, ни в сверку, ни в очередь
рассылки. Применённый как текущий остаток, он отправил бы на WB/Ozon/Kit цифры
той давности, которую запросили для справки, — это прямой оверселл.
"""
from datetime import date, timedelta

import pytest

from app.models import (Barcode, DispatchQueueItem, Product, StockDateRow, StockDateSnapshot,
                        StockDateStatus, SyncSetting)
from app.timeutils import now_utc, today_local
from app.workers.ftp_channel import (LocalExchange, MAX_DATE_REQUESTS_PER_BATCH,
                                     STOCK_ON_DATE_TIMEOUT_MINUTES, apply_stock_on_date_files,
                                     build_task_batch, detect_timed_out_stock_date_requests,
                                     fetch_stock_export_snapshot, parse_stock_on_date_filename,
                                     prune_stock_date_snapshots)
from tests.factories import make_account

ROW = "u1|A-1|Джинсы|7|111,112|46|синий"
ROW2 = "u2|A-2|Футболка|0|222|M|белый"


def _exchange(tmp_path) -> LocalExchange:
    exchange = LocalExchange(tmp_path / "t", tmp_path / "r", tmp_path / "a")
    exchange._ensure_dirs()
    return exchange


def _request(db, day: date = date(2026, 8, 7), status=StockDateStatus.pending) -> StockDateSnapshot:
    snapshot = StockDateSnapshot(snapshot_date=day, status=status, requested_by="admin")
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def _result_file(exchange: LocalExchange, name: str, content: str):
    (exchange.dir_results / name).write_text(content, encoding="utf-8")


# --------------------------------------------------- запрос уходит в 1С

def test_pending_request_goes_into_the_task_file(db, tmp_path):
    exchange = _exchange(tmp_path)
    snapshot = _request(db)

    filename, content = build_task_batch(db, exchange=exchange)

    assert "EXPORT_STOCK_ON_DATE|20260807" in content.splitlines()
    db.refresh(snapshot)
    assert snapshot.status == StockDateStatus.sent
    assert snapshot.batch_filename == filename
    assert snapshot.sent_at is not None


def test_request_alone_is_enough_to_build_a_batch(db, tmp_path):
    """Заданий на перемещение может не быть вовсе — запрос всё равно должен уехать."""
    exchange = _exchange(tmp_path)
    _request(db)

    assert build_task_batch(db, exchange=exchange) is not None


def test_nothing_to_send_still_returns_none(db, tmp_path):
    assert build_task_batch(db, exchange=_exchange(tmp_path)) is None


def test_two_requests_for_one_date_give_one_line(db, tmp_path):
    """1С называет файл ответа по дате среза, поэтому две строки на одну дату
    означали бы, что вторая выгрузка затирает первую."""
    exchange = _exchange(tmp_path)
    first = _request(db)
    second = _request(db)

    _, content = build_task_batch(db, exchange=exchange)

    assert content.splitlines().count("EXPORT_STOCK_ON_DATE|20260807") == 1
    db.refresh(first), db.refresh(second)
    assert first.status == second.status == StockDateStatus.sent


def test_batch_takes_no_more_than_the_limit(db, tmp_path):
    """Каждая дата — отдельный запрос по регистру остатков боевой базы."""
    exchange = _exchange(tmp_path)
    for day in range(1, MAX_DATE_REQUESTS_PER_BATCH + 3):
        _request(db, date(2026, 8, day))

    _, content = build_task_batch(db, exchange=exchange)

    lines = [l for l in content.splitlines() if l.startswith("EXPORT_STOCK_ON_DATE")]
    assert len(lines) == MAX_DATE_REQUESTS_PER_BATCH
    assert db.query(StockDateSnapshot).filter(
        StockDateSnapshot.status == StockDateStatus.pending).count() == 2


def test_already_sent_request_is_not_repeated(db, tmp_path):
    exchange = _exchange(tmp_path)
    _request(db, status=StockDateStatus.sent)

    assert build_task_batch(db, exchange=exchange) is None


# --------------------------------------------------- разбор имени файла

@pytest.mark.parametrize("name,expected", [
    ("ondate_20260807_20260908121314.txt", date(2026, 8, 7)),
    ("ondate_20260101_20260101000000.txt", date(2026, 1, 1)),
    ("stock_20260908121314.txt", None),
    ("ondate_2026_20260908121314.txt", None),
    ("ondate_20261307_20260908121314.txt", None),      # 13-го месяца не бывает
    ("result_20260908121314.txt", None),
])
def test_filename_parsing(name, expected):
    assert parse_stock_on_date_filename(name) == expected


# --------------------------------------------------- приём выгрузки

def test_answer_fills_the_request(db, tmp_path):
    exchange = _exchange(tmp_path)
    snapshot = _request(db, status=StockDateStatus.sent)
    _result_file(exchange, "ondate_20260807_20260908121314.txt", f"{ROW}\n{ROW2}")

    stats = apply_stock_on_date_files(db, exchange)

    db.refresh(snapshot)
    assert stats == {"files": 1, "rows": 2, "unmatched": 0}
    assert snapshot.status == StockDateStatus.done
    assert snapshot.rows_count == 2
    assert snapshot.received_at is not None
    assert snapshot.result_filename == "ondate_20260807_20260908121314.txt"


def test_answer_rows_keep_everything_1c_sent(db, tmp_path):
    exchange = _exchange(tmp_path)
    snapshot = _request(db, status=StockDateStatus.sent)
    _result_file(exchange, "ondate_20260807_20260908121314.txt", ROW)

    apply_stock_on_date_files(db, exchange)

    row = db.query(StockDateRow).filter(StockDateRow.snapshot_id == snapshot.id).one()
    assert (row.uid_1c, row.article, row.name) == ("u1", "A-1", "Джинсы")
    assert (row.quantity, row.barcodes, row.size, row.color) == (7, "111,112", "46", "синий")


def test_file_is_archived_after_reading(db, tmp_path):
    exchange = _exchange(tmp_path)
    _request(db, status=StockDateStatus.sent)
    _result_file(exchange, "ondate_20260807_20260908121314.txt", ROW)

    apply_stock_on_date_files(db, exchange)

    assert exchange.list_stock_on_date_files() == []
    assert (exchange.dir_archive / "ondate_20260807_20260908121314.txt").exists()


def test_repeated_answer_does_not_double_the_rows(db, tmp_path):
    """Обработку 1С могли запустить дважды по одному заданию."""
    exchange = _exchange(tmp_path)
    snapshot = _request(db, status=StockDateStatus.sent)
    _result_file(exchange, "ondate_20260807_20260908121314.txt", ROW)
    apply_stock_on_date_files(db, exchange)
    snapshot.status = StockDateStatus.sent            # как будто ответ пришёл повторно
    db.commit()
    _result_file(exchange, "ondate_20260807_20260908130000.txt", ROW)

    apply_stock_on_date_files(db, exchange)

    assert db.query(StockDateRow).count() == 1


def test_empty_export_is_done_with_a_note(db, tmp_path):
    """Пусто — нормальный ответ для даты без остатков, но оператор должен видеть
    разницу между «пусто» и «ещё не пришло»."""
    exchange = _exchange(tmp_path)
    snapshot = _request(db, status=StockDateStatus.sent)
    _result_file(exchange, "ondate_20260807_20260908121314.txt", "")

    apply_stock_on_date_files(db, exchange)

    db.refresh(snapshot)
    assert snapshot.status == StockDateStatus.done
    assert snapshot.rows_count == 0
    assert "пустую выгрузку" in snapshot.note


def test_answer_without_a_request_is_archived_not_lost(db, tmp_path):
    exchange = _exchange(tmp_path)
    _result_file(exchange, "ondate_20260807_20260908121314.txt", ROW)

    stats = apply_stock_on_date_files(db, exchange)

    assert stats["unmatched"] == 1
    assert db.query(StockDateRow).count() == 0
    assert (exchange.dir_archive / "ondate_20260807_20260908121314.txt").exists()


def test_unparsable_name_is_archived(db, tmp_path):
    exchange = _exchange(tmp_path)
    _result_file(exchange, "ondate_недата_1.txt", ROW)

    stats = apply_stock_on_date_files(db, exchange)

    assert stats["unmatched"] == 1
    assert exchange.list_stock_on_date_files() == []


# --------------------------------------------------- ГРАНИЦА: это только справка

def test_dated_export_is_invisible_to_the_current_stock_export(db, tmp_path):
    """Файл среза не должен попасть в обычную выгрузку остатков: её приложение
    применяет как ТЕКУЩИЙ остаток и рассылает на площадки."""
    exchange = _exchange(tmp_path)
    _result_file(exchange, "ondate_20260807_20260908121314.txt", ROW)

    assert exchange.list_stock_files() == []
    assert fetch_stock_export_snapshot(exchange) == ([], None)


def test_dated_export_does_not_touch_the_product_stock(db, tmp_path):
    exchange = _exchange(tmp_path)
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A-1", name="Джинсы", stock_on_hand=2,
                   broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    _request(db, status=StockDateStatus.sent)
    _result_file(exchange, "ondate_20260807_20260908121314.txt", ROW)   # в срезе 7 штук

    apply_stock_on_date_files(db, exchange)

    assert db.query(Product).one().stock_on_hand == 2      # остаток остался «сейчас»
    assert db.query(DispatchQueueItem).count() == 0        # и на площадки ничего не ушло


def test_dated_export_creates_no_products(db, tmp_path):
    """Обычная выгрузка заводит новые SKU (1С — хозяин ассортимента). Срез за
    прошлое число этого делать не должен: там встречаются товары, которых в
    каталоге уже нет."""
    exchange = _exchange(tmp_path)
    _request(db, status=StockDateStatus.sent)
    _result_file(exchange, "ondate_20260807_20260908121314.txt", f"{ROW}\n{ROW2}")

    apply_stock_on_date_files(db, exchange)

    assert db.query(Product).count() == 0


# --------------------------------------------------- таймаут и уборка

def test_silent_request_becomes_timeout(db):
    snapshot = _request(db, status=StockDateStatus.sent)
    snapshot.sent_at = now_utc() - timedelta(minutes=STOCK_ON_DATE_TIMEOUT_MINUTES + 1)
    db.commit()

    stale = detect_timed_out_stock_date_requests(db)

    assert len(stale) == 1
    db.refresh(snapshot)
    assert snapshot.status == StockDateStatus.timeout
    assert "обработка" in snapshot.note


def test_fresh_request_is_not_declared_lost(db):
    """Обработка 1С запускается по своему расписанию — первые минуты молчания норма."""
    snapshot = _request(db, status=StockDateStatus.sent)
    snapshot.sent_at = now_utc() - timedelta(minutes=5)
    db.commit()

    assert detect_timed_out_stock_date_requests(db) == []
    db.refresh(snapshot)
    assert snapshot.status == StockDateStatus.sent


def test_old_snapshots_are_pruned_with_their_rows(db, tmp_path):
    """Каждая выгрузка — склад целиком; без уборки база росла бы от справок."""
    for day in range(1, 6):
        snapshot = _request(db, date(2026, 8, day), status=StockDateStatus.done)
        db.add(StockDateRow(snapshot_id=snapshot.id, uid_1c="u1", quantity=1))
    db.commit()

    removed = prune_stock_date_snapshots(db, keep=2)

    assert removed == 3
    assert db.query(StockDateSnapshot).count() == 2
    assert db.query(StockDateRow).count() == 2
    assert [s.snapshot_date.day for s in db.query(StockDateSnapshot).all()] == [4, 5]


# --------------------------------------------------- страница

def _done_snapshot(web_db, day: date = date(2026, 8, 7)) -> StockDateSnapshot:
    snapshot = StockDateSnapshot(snapshot_date=day, status=StockDateStatus.done,
                                 requested_by="admin", rows_count=2, received_at=now_utc())
    web_db.add(snapshot)
    web_db.commit()
    web_db.refresh(snapshot)
    web_db.add(StockDateRow(snapshot_id=snapshot.id, uid_1c="u1", article="A-1",
                            name="Джинсы", size="46", color="синий", barcodes="111", quantity=7))
    web_db.add(StockDateRow(snapshot_id=snapshot.id, uid_1c="u2", article="A-2",
                            name="Футболка", size="M", color="белый", barcodes="222", quantity=0))
    web_db.commit()
    return snapshot


def test_page_opens(logged_in_client, web_db):
    r = logged_in_client.get("/stock-on-date")

    assert r.status_code == 200
    assert "Остатки 1С на дату" in r.text


def test_page_is_in_the_menu(logged_in_client, web_db):
    assert "/stock-on-date" in logged_in_client.get("/products").text


def test_request_creates_a_pending_snapshot(logged_in_client, web_db):
    from app.models import AuditLog

    r = logged_in_client.post("/stock-on-date/request", data={"value": "2026-08-07"},
                              follow_redirects=False)

    assert r.status_code == 303
    snapshot = web_db.query(StockDateSnapshot).one()
    assert snapshot.snapshot_date == date(2026, 8, 7)
    assert snapshot.status == StockDateStatus.pending
    assert snapshot.requested_by == "admin"
    assert web_db.query(AuditLog).filter(AuditLog.action == "stock_on_date_requested").count() == 1


def test_bad_date_is_refused_with_a_message(logged_in_client, web_db):
    r = logged_in_client.post("/stock-on-date/request", data={"value": "07.08.2026"},
                              follow_redirects=True)

    assert "Дата должна быть в формате" in r.text
    assert web_db.query(StockDateSnapshot).count() == 0


def test_future_date_is_refused(logged_in_client, web_db):
    tomorrow = (today_local() + timedelta(days=1)).isoformat()

    r = logged_in_client.post("/stock-on-date/request", data={"value": tomorrow},
                              follow_redirects=True)

    assert "будущую дату" in r.text
    assert web_db.query(StockDateSnapshot).count() == 0


def test_second_request_for_the_same_date_is_not_created(logged_in_client, web_db):
    logged_in_client.post("/stock-on-date/request", data={"value": "2026-08-07"})

    r = logged_in_client.post("/stock-on-date/request", data={"value": "2026-08-07"},
                              follow_redirects=True)

    assert "уже отправлен" in r.text
    assert web_db.query(StockDateSnapshot).count() == 1


def test_finished_export_is_shown(logged_in_client, web_db):
    _done_snapshot(web_db)

    r = logged_in_client.get("/stock-on-date")

    assert "Джинсы" in r.text and "Футболка" in r.text


def test_search_filters_the_rows(logged_in_client, web_db):
    snapshot = _done_snapshot(web_db)

    r = logged_in_client.get("/stock-on-date/rows",
                             params={"snapshot_id": snapshot.id, "q": "A-2"})

    assert "Футболка" in r.text
    assert "Джинсы" not in r.text


def test_barcode_search_works(logged_in_client, web_db):
    snapshot = _done_snapshot(web_db)

    r = logged_in_client.get("/stock-on-date/rows",
                             params={"snapshot_id": snapshot.id, "q": "111"})

    assert "Джинсы" in r.text and "Футболка" not in r.text


def test_nonzero_filter_hides_empty_positions(logged_in_client, web_db):
    snapshot = _done_snapshot(web_db)

    r = logged_in_client.get("/stock-on-date/rows",
                             params={"snapshot_id": snapshot.id, "nonzero": "true"})

    assert "Джинсы" in r.text and "Футболка" not in r.text


def test_export_gives_an_xlsx(logged_in_client, web_db):
    snapshot = _done_snapshot(web_db)

    r = logged_in_client.get(f"/stock-on-date/{snapshot.id}/export")

    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]


def test_unknown_snapshot_id_does_not_crash_the_page(logged_in_client, web_db):
    assert logged_in_client.get("/stock-on-date", params={"snapshot_id": "мусор"}).status_code == 200


# --------------------------------------------------- порог считается при приёме файла

def test_arriving_file_computes_the_threshold_for_waiting_products(db, tmp_path):
    """Сквозная проверка связки: оператор задал дату заранее, 1С ответила файлом —
    порог обязан посчитаться прямо здесь.

    Без этого массовая простановка даты не работала бы вовсе: ответ 1С идёт до
    десяти минут, и расчёт застревал бы до тех пор, пока каждую из 152 тысяч
    строк не тронут руками.
    """
    from app.offset_base import set_base_date

    exchange = _exchange(tmp_path)
    snapshot = _request(db, status=StockDateStatus.sent)
    product = Product(uid_1c="u1", article="A-1", name="Джинсы", stock_on_hand=9,
                      reserve=2, broadcast_enabled=True)
    db.add(product)
    db.commit()
    # Порядок: сначала дата, потом факт. Смена даты стирает факт — он всегда
    # «факт на дату», и число, пересчитанное на другое число, к делу не относится.
    set_base_date(db, product, date(2026, 8, 7))
    product.fact_at_date = 5
    db.commit()
    assert product.broadcast_offset is None        # ответа ещё нет — считать не из чего

    _result_file(exchange, "ondate_20260807_20260908121314.txt", ROW)   # u1 -> 7
    apply_stock_on_date_files(db, exchange)

    db.refresh(product)
    assert product.offset_base_stock == 7
    assert product.broadcast_offset == 4           # 7 − (5 − 2)


def test_arriving_file_does_not_touch_the_current_stock(db, tmp_path):
    """Граница, ради которой выгрузка на дату живёт отдельно, от нового расчёта
    не сдвигается: остаток товара — по-прежнему «сейчас», а не «на дату»."""
    from app.offset_base import set_base_date

    exchange = _exchange(tmp_path)
    _request(db, status=StockDateStatus.sent)
    product = Product(uid_1c="u1", article="A-1", name="Джинсы", stock_on_hand=9,
                      reserve=0, broadcast_enabled=True)
    db.add(product)
    db.commit()
    set_base_date(db, product, date(2026, 8, 7))

    _result_file(exchange, "ondate_20260807_20260908121314.txt", ROW)   # u1 -> 7
    apply_stock_on_date_files(db, exchange)

    db.refresh(product)
    assert product.stock_on_hand == 9              # НЕ 7

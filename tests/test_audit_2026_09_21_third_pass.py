"""Находки третьей волны аудита 21.09 — обслуживание, клиенты, каналы.

Тут собраны дефекты, у которых общее — они делают систему ТИХОЙ. Копия базы не
снимается, а мониторинг зелёный. Ответ 1С теряется, а задание просто «ещё в
пути». Отказ площадки по конкретной карточке выглядит как обрыв связи, и человека
отправляют чинить сеть вместо мэппинга.
"""
import sqlite3
import threading
import time
from datetime import date, timedelta

import pytest
import requests

from app.models import (Barcode, DispatchQueueItem, DispatchStatus, FtpTask, FtpTaskStatus,
                        PlatformAccount, Product, SyncSetting, WorkerHeartbeat)
from app.timeutils import now_utc


# --------------------------------------------------------------------------
# Бэкап
# --------------------------------------------------------------------------

def test_backup_completes_while_someone_else_is_writing(tmp_path, monkeypatch):
    """Копия снимается на ЖИВОЙ базе под чужой записью.

    `Connection.backup(pages=...)` при чужой записи между шагами начинает
    копирование ЗАНОВО. Порциями по тысяче страниц это означало, что копия может
    не сняться никогда: замер на базе в 150 МБ при десяти коммитах в секунду дал
    33,5 с и 1448 перезапусков, при двадцати и ста — не завершилось за минуту
    вовсе. Задание при этом не падает, а крутится на 100% CPU и не обновляет
    heartbeat. Порции не покупали ничего: в WAL читатель писателя не блокирует.
    """
    import app.backup as backup

    source = tmp_path / "live.db"
    conn = sqlite3.connect(source)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE products (uid_1c TEXT PRIMARY KEY, name TEXT, n INTEGER)")
    conn.executemany("INSERT INTO products VALUES (?,?,?)",
                     [(f"u{i}", "Товар " * 20, i) for i in range(20000)])
    conn.commit()
    conn.close()

    stop = threading.Event()

    def writer():
        w = sqlite3.connect(source, timeout=30)
        w.execute("PRAGMA journal_mode=WAL")
        while not stop.is_set():
            w.execute("UPDATE products SET n = n + 1 WHERE uid_1c = 'u1'")
            w.commit()
            time.sleep(0.002)          # плотная запись, как в час пик
        w.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "copies"))
        started = time.time()
        result = backup.make_backup(database_url=f"sqlite:///{source}")
        took = time.time() - started
    finally:
        stop.set()
        thread.join()

    assert result.ok, f"копия не снялась: {result.error}"
    assert took < 30, f"копия снималась {took:.1f} с — похоже на перезапуски"


def test_a_broken_copy_is_not_mistaken_for_a_fresh_one(tmp_path, monkeypatch):
    """Оборванная копия не должна считаться копией.

    `last_backup()` различает копии ПО ИМЕНИ. Файл правильного вида, оставшийся
    от сорвавшейся попытки, проходил за полноценную копию: следующий запуск
    видел «свежая копия уже есть», писал зелёный heartbeat и не пробовал снова
    двадцать часов, а находка отчёта молчала двое суток.
    """
    import app.backup as backup

    directory = tmp_path / "copies"
    directory.mkdir()
    monkeypatch.setenv("BACKUP_DIR", str(directory))

    source = tmp_path / "live.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE products (uid_1c TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    # `sqlite3.Connection` подменить нельзя (неизменяемый тип), поэтому рвём само
    # копирование — ровно там, где оно и рвётся на бою (кончилось место на диске).
    def _explode(src, dst):
        dst.write_bytes(b"not-a-database")            # файл на диске уже создан
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(backup, "_copy_database", _explode)
    result = backup.make_backup(database_url=f"sqlite:///{source}")

    assert not result.ok
    moment, count = backup.last_backup(directory)
    assert moment is None and count == 0, "негодный файл засчитан за копию"
    assert list(directory.glob("*.bad")), "файл не сохранён для разбора"


def test_pruning_keeps_one_copy_per_day_not_the_last_fourteen(tmp_path):
    """Уборка хранит по копии на КАЛЕНДАРНЫЙ ДЕНЬ.

    Было «первые четырнадцать файлов», и стоило копиям пойти чаще раза в сутки —
    а `scripts/backup_db.py` прямо предлагается для планировщика Windows и
    никакого промежутка не проверяет, — как четырнадцать «ежедневных»
    превращались в четырнадцать последних ЧАСОВ. Замер: после уборки оставались
    15 последних часов и провал в неделю сразу за ними.
    """
    import app.backup as backup

    directory = tmp_path / "copies"
    directory.mkdir()
    # Сутки почасовых копий + две недели суточных.
    for hour in range(24):
        (directory / f"sync_admin-20260921-{hour:02d}0000.db").write_bytes(b"x")
    for day in range(7, 21):
        (directory / f"sync_admin-202609{day:02d}-120000.db").write_bytes(b"x")

    backup.prune(directory, keep_daily=14, keep_weekly=0)

    left = sorted(p.name for p in directory.glob("sync_admin-*.db"))
    # Дни считаем ТЕМ ЖЕ правилом, что и уборка, — по местному календарю.
    # Раньше здесь стояло `name.split("-")[1]`, то есть UTC-шное число из имени
    # файла, и тест был верен только там, где местный пояс совпадает с UTC.
    # Машина разработки и CI стоят в UTC, боевой сервер — в Москве: 24.09 накат
    # на нём встал именно здесь. Две почасовые копии после 21:00 UTC приходятся
    # уже на СЛЕДУЮЩИЙ местный день, и по именам файлов они выглядели одним днём.
    days = {backup.local_date_of(backup._parse_moment(name)) for name in left}
    assert len(days) >= 14, f"дней осталось {len(days)}, а должно быть 14: {left}"
    assert date(2026, 9, 14) in days, "неделю назад копии не осталось"


# --------------------------------------------------------------------------
# Чистка истории
# --------------------------------------------------------------------------

def test_retention_keeps_an_unresolved_dispatch_error(db):
    """Отказ рассылки — незаконченное дело, а не история.

    Запись в `error` выглядит терминальной (попытки исчерпаны), но описывает
    расхождение, которое никуда не делось: остаток списан, число не уехало,
    площадка продаёт то, чего нет. Через тридцать суток критичная находка просто
    исчезала из отчёта.
    """
    from app.retention import apply_retention

    account = PlatformAccount(platform="wb", name="WB", warehouse_id="w1", is_active=True)
    db.add(account); db.commit(); db.refresh(account)
    old = now_utc() - timedelta(days=40)
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=5))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5,
                             reason="order", status=DispatchStatus.error,
                             created_at=old, last_error="не отправлено за 5 попыток"))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=account.id, quantity=5,
                             reason="order", status=DispatchStatus.sent,
                             created_at=old, sent_at=old, sent_quantity=5))
    db.commit()

    apply_retention(db)

    left = db.query(DispatchQueueItem).all()
    assert [r.status for r in left] == [DispatchStatus.error], (
        "отказ удалён вместе с историей — находка отчёта исчезнет")


# --------------------------------------------------------------------------
# Отметка «задание отработало»
# --------------------------------------------------------------------------

def test_heartbeat_survives_a_broken_session(db):
    """Отметка об ошибке пишется даже после упавшего коммита.

    Самый важный класс сбоя — упавший `commit` внутри задания. После него сессия
    сломана, и `_heartbeat` падал САМ: запись об ошибке не появлялась, а в базе
    оставались время и `last_success=True` от ПРОШЛОГО удачного прогона. `/health`
    зелёный, «Диагностика» пустая, текст ошибки потерян.
    """
    from app.workers.scheduler import _heartbeat

    _heartbeat(db, "проба", True)
    # Ломаем сессию так же, как её ломает неудачный commit задания.
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=1))
    db.commit()
    db.add(Product(uid_1c="u1", article="A", name="дубль", stock_on_hand=1))
    with pytest.raises(Exception):
        db.commit()
    assert db.is_active is False

    _heartbeat(db, "проба", False, "database is locked")

    row = db.query(WorkerHeartbeat).filter(WorkerHeartbeat.worker_name == "проба").one()
    assert row.last_success is False
    assert "locked" in (row.last_error or "")


def test_heartbeat_does_not_discard_a_healthy_transaction(db):
    """А на здоровой сессии отметка НИЧЕГО не откатывает.

    Часть заданий коммитит свою работу тем же коммитом, что и отметку.
    Безусловный откат был бы лекарством хуже болезни.
    """
    from app.workers.scheduler import _heartbeat

    db.add(Product(uid_1c="u9", article="A", name="Т", stock_on_hand=3))
    _heartbeat(db, "проба2", True)

    assert db.query(Product).filter(Product.uid_1c == "u9").one().stock_on_hand == 3


# --------------------------------------------------------------------------
# Канал 1С
# --------------------------------------------------------------------------

def test_a_result_file_stays_until_it_is_parsed(tmp_path, db, monkeypatch):
    """Ответ 1С уезжает в архив только ПОСЛЕ успешного разбора.

    Файл архивировался первым действием, и любой сбой записи в базу (`database is
    locked`) уносил ответы безвозвратно: из архива их никто не перечитывает,
    задания оставались «в пути» навсегда — по созданиям остаток занижен, по
    отменам завышен.
    """
    from app.workers.ftp_channel import LocalExchange

    exchange = LocalExchange(tmp_path / "t", tmp_path / "r", tmp_path / "a")
    exchange._ensure_dirs()
    (exchange.dir_results / "result_1.txt").write_text(
        "o1|OK|ЦБ0001|CREATE_MOVEMENT", encoding="utf-8")

    content = exchange.read_result("result_1.txt")
    assert "ЦБ0001" in content
    assert (exchange.dir_results / "result_1.txt").exists(), (
        "чтение не должно трогать файл — разбор может ещё не удаться")

    exchange.archive_result("result_1.txt")
    assert not (exchange.dir_results / "result_1.txt").exists()
    assert (exchange.dir_archive / "result_1.txt").exists()


def test_the_command_name_makes_matching_exact(db):
    """Ответ по ОТМЕНЕ не должен закрывать задание СОЗДАНИЯ.

    Без имени команды разбор берёт самое старое незакрытое задание по номеру
    заказа. Отмена уходила в `timeout` и навсегда считалась «в пути» со знаком
    минус — остаток завышался ровно на количество заказа и уезжал на площадки.
    """
    from app.workers.ftp_channel import apply_result_batch

    account = PlatformAccount(platform="wb", name="WB", warehouse_id="w1", is_active=True)
    db.add(account); db.commit(); db.refresh(account)
    create = FtpTask(command="CREATE_MOVEMENT", barcode="111", warehouse_from="ЦС",
                     warehouse_to="WB", quantity=1, order_id="o1",
                     account_id=account.id, status=FtpTaskStatus.timeout)
    cancel = FtpTask(command="CANCEL_MOVEMENT", barcode="111", quantity=1, order_id="o1",
                     account_id=account.id, status=FtpTaskStatus.sent)
    db.add_all([create, cancel]); db.commit()

    apply_result_batch(db, "o1|OK|ЦБ-реверс|CANCEL_MOVEMENT")

    db.refresh(create); db.refresh(cancel)
    assert cancel.status == FtpTaskStatus.done, "ответ по отмене не закрыл отмену"
    assert create.status == FtpTaskStatus.timeout, "ответ по отмене съел создание"


# --------------------------------------------------------------------------
# Клиенты площадок
# --------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)
        self.content = b"x"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


def test_ozon_reads_every_page_of_new_orders(monkeypatch):
    """Приём заказов Ozon листает ленту, а не берёт первую сотню.

    Больше сотни отправлений в `awaiting_approve` — и заказы со сто первого не
    видел никто. Подтверждённое отправление уходит из этого статуса, то есть мы
    не увидели бы их УЖЕ НИКОГДА: остаток не списан, документа в 1С нет, наружу
    идёт завышенное число.
    """
    from app.workers.platform_clients.ozon import OzonClient

    pages = []

    def fake_post(self, path, body):
        offset = body.get("offset", 0)
        pages.append(offset)
        total = 250
        chunk = [
            {"posting_number": f"p{offset + i}",
             "products": [{"barcode": f"bc{offset + i}", "sku": offset + i, "quantity": 1}]}
            for i in range(min(100, max(0, total - offset)))
        ]
        return {"result": {"postings": chunk, "has_next": offset + len(chunk) < total}}

    monkeypatch.setattr(OzonClient, "_post", fake_post)
    client = OzonClient(client_id="x", api_key="y")
    orders = client.get_orders_awaiting_confirmation()

    assert len(orders) == 250, f"собрано {len(orders)} из 250"
    assert pages == [0, 100, 200]


def test_ozon_names_the_guilty_position_as_terminal(monkeypatch):
    """Отказ Ozon по карточке закрывается сразу, а не пять раз повторяется.

    Словарь ошибки уходил наверх сырым — без `sku` и `terminal`, — и рассылка не
    могла отличить «этой карточки нет» от обрыва связи: двадцать три минуты
    повторов, а потом отчёт отправлял человека чинить связь вместо мэппинга.
    """
    from app.workers.platform_clients.ozon import OzonClient
    from app.workers.platform_clients.base import StockPushItem

    def fake_post(self, path, body):
        return {"result": [{
            "offer_id": "ART-1", "updated": False,
            "errors": [{"code": "NOT_FOUND_ERROR", "message": "товар не найден"}],
        }]}

    monkeypatch.setattr(OzonClient, "_post", fake_post)
    client = OzonClient(client_id="x", api_key="y")
    result = client.push_stock("w", [StockPushItem(barcode="111", quantity=5, article="ART-1")])

    assert result["ok"] == []
    assert result["errors"][0]["sku"] == "111"
    assert result["errors"][0]["terminal"] is True
    assert "NOT_FOUND_ERROR" in result["errors"][0]["detail"]


def test_wb_does_not_report_a_rejected_sku_as_sent(monkeypatch):
    """Отклонённая позиция не может одновременно считаться отправленной.

    Из `remaining` позиция выбывает ТОЛЬКО когда её отклонила площадка. Раньше
    аварийные выходы возвращали «то, что выбыло» как успешно отправленное: баркод
    попадал и в `ok`, и в `errors`, а рассылка проверяет `ok` раньше — запись
    закрывалась как успешная отправка со временем.
    """
    from app.workers.platform_clients.wb import WbClient
    from app.workers.platform_clients.base import StockPushItem

    calls = {"n": 0}

    def fake_put(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse([{"code": "NotFound",
                                   "data": [{"sku": "B-BAD", "chrtId": 0, "amount": 0}]}],
                                 status=409)
        raise requests.ConnectionError("обрыв связи")

    client = WbClient(token="x", warehouse_id="w")
    monkeypatch.setattr(client.session, "put", fake_put)

    result = client.push_stock("w", [
        StockPushItem(barcode="B-BAD", quantity=1),
        StockPushItem(barcode="B-OK", quantity=2),
    ])

    assert "B-BAD" not in result["ok"], "отклонённая позиция объявлена отправленной"


def test_retry_after_is_capped(monkeypatch):
    """Площадка не может усыпить воркер на часы.

    `Retry-After: 3600` уходил в `sleep` как есть, дважды за вызов — два часа сна
    в потоке планировщика. Рассылка всё это время не отправляет остатки, а
    снаружи всё выглядит работающим.
    """
    import app.workers.http_retry as http_retry

    slept = []
    monkeypatch.setattr(http_retry.time, "sleep", lambda s: slept.append(s))

    class _Resp:
        status_code = 429
        headers = {"Retry-After": "3600"}

    def always_429():
        raise requests.HTTPError("429", response=_Resp())

    with pytest.raises(requests.HTTPError):
        http_retry.with_retry(always_429)

    assert slept, "повторов не было — тест ни о чём"
    assert max(slept) <= http_retry.MAX_SLEEP_SECONDS, f"паузы: {slept}"

"""Находки аудита 23.09, части 3–6.

Каждая — про то, что сигнал или правило есть, а работают они не так, как
написано.
"""
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import backup
from app.models import (Base, DiscrepancySource, OrderProcessStatus,
                        ProcessedOrder, Product, WorkerHeartbeat)
from app.offset_base import set_discrepancy
from app.routers import health
from app.timeutils import now_utc
from app.workers import scheduler

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------- 3. сторож

def test_the_watchdog_pings_without_waiting_for_the_alerts_delay():
    """Двенадцать минут заведены под КАНАЛЫ, а сторож их наследовал.

    Он отвечает на один вопрос — «жив ли воркер», — и ответ верен с первой
    секунды. Пока задержка была общей, штатный разрыв пингов при накате выходил
    около тринадцати минут, и на той стороне приходилось держать
    `period` + `grace` не меньше пятнадцати — то есть четверть часа слепоты к
    настоящей смерти воркера."""
    assert scheduler.WATCHDOG_FIRST_RUN_DELAY < timedelta(minutes=2), (
        "сторож снова ждёт перед первым пингом")
    assert scheduler.WATCHDOG_FIRST_RUN_DELAY < scheduler.ALERT_FIRST_RUN_DELAY


def test_the_watchdog_is_a_job_of_its_own(monkeypatch, db):
    """Отдельное задание, а не строчка внутри `job_alerts`.

    Иначе оно снова унаследует чужое расписание при первой же правке."""
    pinged = []
    monkeypatch.setattr("app.alerts.ping_alive",
                        lambda session: pinged.append(1) or "")
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    scheduler.job_watchdog()

    assert pinged == [1], "сторож не дёрнулся"
    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "watchdog").first()
    assert row is not None and row.last_success is True
    assert not (row.last_error or ""), "оговорка на исправном пинге"


def test_a_ping_that_did_not_arrive_is_said_out_loud(monkeypatch, db):
    """Сторож, о котором думают, что он сторожит, хуже отсутствующего."""
    monkeypatch.setattr("app.alerts.ping_alive",
                        lambda session: "ConnectionError: нет сети")
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    scheduler.job_watchdog()

    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "watchdog").first()
    assert row.last_success is True, "не доехавший пинг — не поломка задания"
    assert "внешний сторож не ответил" in row.last_error


def test_the_alerts_job_no_longer_carries_the_watchdog():
    """Два смысла в одном задании — и сигнал одного неотличим от другого."""
    source = (ROOT / "app" / "workers" / "scheduler.py").read_text(encoding="utf-8")
    start = source.index("def job_alerts(")
    # До БЛИЖАЙШЕГО следующего определения, а не до `build_scheduler`: между
    # ними теперь стоит сам `job_watchdog`, и срез «до сборки расписания»
    # захватывал его — тест падал на правильном коде.
    end = source.index("\ndef ", start + 1)
    body = source[start:end]
    assert "ping_alive" not in body, "сторож снова живёт внутри уведомлений"


def test_the_watchdog_is_watched_itself():
    """Задание, которое зовёт человека, обязано быть под присмотром само.

    Замолчав, оно молчит ровно так же, как исправная система."""
    assert "watchdog" in health.REQUIRED_WORKERS
    assert "watchdog" in health.EXPECTED_INTERVAL_SECONDS
    interval = scheduler.WATCHDOG_INTERVAL_MINUTES * 60
    assert health.EXPECTED_INTERVAL_SECONDS["watchdog"] >= 2 * interval


def test_the_diagnostics_page_shows_the_watchdog(logged_in_client):
    """Оговорка без читателя — это оговорка, которой нет."""
    page = logged_in_client.get("/diagnostics").text
    assert "watchdog" in page


# ------------------------------------------------- 4. сутки копий местные

def _name(stamp):
    return f"sync_admin-{stamp}.db"


def test_backup_days_are_counted_in_local_time(monkeypatch):
    """Имя копии штампуется UTC, а «календарный день» — местный.

    У Москвы UTC+3: копия, снятая в 01:00 по местному, по UTC приходится на
    предыдущие сутки. Считая по UTC, две копии ОДНОГО местного дня занимали
    РАЗНЫЕ слоты — и глубина хранения молча становилась на день короче.

    Набор подобран так, чтобы два правила дали РАЗНЫЙ ответ, иначе проверка
    ничего не проверяет:

        A  01.09 22:00 UTC = 02.09 01:00 МСК
        B  02.09 05:00 UTC = 02.09 08:00 МСК
        C  02.09 22:00 UTC = 03.09 01:00 МСК

    По местному это дни 02, 02, 03 — при двух суточных слотах лишняя A.
    По UTC это 01, 02, 02 — лишней оказывается B, то есть удаляется НЕ ТА копия.

    **Пояс подменяется ФУНКЦИЕЙ, а не переменной окружения.** Первая версия
    ставила `TZ=Europe/Moscow` и звала `time.tzset()` — и накат на бою упал с
    `AttributeError: module 'time' has no attribute 'tzset'`: `tzset` есть только
    на Unix, а боевой сервер — Windows. Машина разработки при этом Linux и в UTC,
    то есть увидеть это здесь нельзя было ни при каких условиях. Подмена
    `local_date_of` делает тест независимым и от платформы, и от пояса ОС, а
    проверяет он ровно то, что нужно: считаются сутки ЭТОЙ функцией, а не
    `moment.date()`.
    """
    def moscow(moment):
        return (moment + timedelta(hours=3)).date()

    monkeypatch.setattr(backup, "local_date_of", moscow)

    a, b, c = (_name("20260901-220000"), _name("20260902-050000"),
               _name("20260902-220000"))
    drop = backup.names_to_drop([a, b, c], keep_daily=2, keep_weekly=0,
                                now=datetime(2026, 9, 20, 12, 0, 0))
    assert drop == [a], (
        f"по местным суткам лишняя — {a}, а удалено {drop}: сутки считаются по UTC")


def test_the_timezone_is_never_taken_from_the_operating_system(monkeypatch):
    """И ЭТОГО в тестах бэкапа быть не должно вовсе.

    `time.tzset()` отсутствует на Windows, а боевой сервер — Windows: такой тест
    валит накат всегда, и починить его по месту нельзя — на машине разработки он
    зелёный. Сканер исходника, потому что поведенчески это здесь не проверяется.
    """
    import ast

    # Разбираем ДЕРЕВО, а не текст: слово `tzset` стоит в объяснении выше, и
    # сканер по подстроке падал на собственном комментарии. Проверка про ВЫЗОВ.
    for path in sorted((ROOT / "tests").glob("test_*backup*.py")) + \
            [ROOT / "tests" / "test_audit_findings_3_to_6.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        assert "tzset" not in called, (
            f"{path.name} меняет пояс через ОС — на боевом Windows это "
            f"AttributeError, и накат встанет на тестах")


# ------------------------------- 5. массовая правка умеет снять измерение

def test_bulk_can_clear_a_measurement_the_way_a_row_can(logged_in_client, web_db):
    """Строка снимает измерение пустым полем, файл — «-», а отбор не мог никак.

    Оператор, снявший измерение у одной строки, не мог снять его у отбора: поле
    отвечало «введите число». Правило общее — массовый путь делает то же, что
    построчный."""
    product = Product(uid_1c="u1", article="a", name="n", stock_on_hand=10,
                      reserve=0)
    web_db.add(product)
    web_db.flush()
    set_discrepancy(web_db, product, 7, source=DiscrepancySource.manual)
    web_db.commit()

    answer = logged_in_client.post("/products/bulk", data={
        "action": "set_discrepancy", "int_value": "-", "uids": ["u1"],
    }, follow_redirects=False)
    assert answer.status_code == 303

    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first() \
        .stock_discrepancy is None, "измерение не снято"


def test_bulk_still_refuses_an_empty_field(logged_in_client, web_db):
    """Пустое поле — «числа не ввели», а не «снять».

    Снять значение можно, только сказав это вслух: иначе промах мимо поля
    снимал бы измерение по всему отбору молча."""
    product = Product(uid_1c="u1", article="a", name="n", stock_on_hand=10,
                      reserve=0)
    web_db.add(product)
    web_db.flush()
    set_discrepancy(web_db, product, 7, source=DiscrepancySource.manual)
    web_db.commit()

    logged_in_client.post("/products/bulk", data={
        "action": "set_discrepancy", "int_value": "", "uids": ["u1"],
    }, follow_redirects=False)

    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first() \
        .stock_discrepancy == 7, "пустое поле сняло измерение"


def test_a_cleared_measurement_is_not_the_same_as_zero(logged_in_client, web_db):
    """NULL — «не измеряли», ноль — «измеряли, склад сошёлся»."""
    for uid in ("u-снять", "u-ноль"):
        p = Product(uid_1c=uid, article=uid, name=uid, stock_on_hand=10, reserve=0)
        web_db.add(p)
        web_db.flush()
        set_discrepancy(web_db, p, 7, source=DiscrepancySource.manual)
    web_db.commit()

    logged_in_client.post("/products/bulk", data={
        "action": "set_discrepancy", "int_value": "-", "uids": ["u-снять"]},
        follow_redirects=False)
    logged_in_client.post("/products/bulk", data={
        "action": "set_discrepancy", "int_value": "0", "uids": ["u-ноль"]},
        follow_redirects=False)

    web_db.expire_all()
    by_uid = {p.uid_1c: p.stock_discrepancy for p in web_db.query(Product).all()}
    assert by_uid["u-снять"] is None
    assert by_uid["u-ноль"] == 0


# ------------------------------------------- 6. докстрока про порог

def test_the_recompute_docstring_no_longer_lies():
    """Комментарий, утверждающий обратное тому, что делает функция, дороже
    отсутствующего: в этом коде правила передаются объяснением рядом с кодом."""
    source = (ROOT / "app" / "transmit.py").read_text(encoding="utf-8")
    body = source[source.index("def recompute_offset("):]
    doc = body[:body.index('"""', body.index('"""') + 3)]
    assert "Даты нет — порог не трогаем" not in doc, (
        "докстрока описывает прежнюю модель, где носителем порога была ДАТА")


def test_a_stored_discrepancy_survives_a_date_that_was_taken_away():
    """И это не только про текст: свойство, которое докстрока отрицала."""
    from app.transmit import recompute_offset

    product = Product(uid_1c="u1", article="a", name="n", stock_on_hand=10,
                      reserve=2, stock_discrepancy=5,
                      offset_base_date=None, offset_base_stock=None)
    recompute_offset(product)
    assert product.broadcast_offset == 7, "порог без даты не посчитался"


# ------------------------------------ блок «Заказы и отмены» в скрипте

@pytest.fixture()
def catalog_with_orders(tmp_path):
    path = tmp_path / "probe_orders.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    session.add(Product(uid_1c="u1", article="36446 b", name="Свитшот",
                        size="5XL", color="LACIVERT", stock_on_hand=0, reserve=2))
    session.add(ProcessedOrder(order_id="5855466231", account_id=5, uid_1c="u1",
                               quantity=1, status=OrderProcessStatus.cancelled,
                               processed_at=now_utc()))
    session.add(ProcessedOrder(order_id="5861363223", account_id=5, uid_1c="u1",
                               quantity=1, status=OrderProcessStatus.processed,
                               processed_at=now_utc() - timedelta(hours=7)))
    session.commit()
    session.close()
    engine.dispose()
    return url


def test_the_probe_names_orders_and_cancellations(catalog_with_orders):
    """Номера заказов не показывает НИ ОДНА страница и ни одна выгрузка.

    Поэтому связать обратный документ 1С («sync REVERSE sync order_id=…») с тем,
    что видела система, приходилось сверкой по часам с пересчётом МСК в UTC.
    Очередь рассылки номеров заказов не несёт вовсе: она про то, какое ЧИСЛО
    ушло на площадку, а не про то, что его вызвало."""
    env = dict(os.environ)
    env["DATABASE_URL"] = catalog_with_orders
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "probe_offset.py"), "u1"],
                          capture_output=True, text=True, env=env, cwd=str(ROOT))
    out = done.stdout + done.stderr

    assert "ЗАКАЗЫ И ОТМЕНЫ" in out, out
    assert "5855466231" in out and "5861363223" in out
    assert "каб.5" in out
    assert "ОТМЕНА" in out, "отмену надо видеть — её и ищут"

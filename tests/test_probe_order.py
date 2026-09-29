"""Скрипт разбора заказа обязан называть ИСТОЧНИК отмены, а не только числа.

Повод. В 1С нашёлся документ «Перемещение товаров» с комментарием
`sync REVERSE sync order_id=... wb`, и по нему видно ровно одно: 1С отработала
наше задание `CANCEL_MOVEMENT`. Кто эту отмену заказал, оттуда не видно вовсе —
документ одинаков и когда заказ отменил продавец в кабинете площадки, и когда
человек нажал «Симулировать отмену» у нас.

Ответ лежит в трёх местах нашей базы, и по любому одному вывод получается
неверный: боевое задание без следа в журнале и тренировочное задание выглядят в
списке заданий почти одинаково, а разница между ними — «разбирайтесь в кабинете
WB» против «это мы сами».

Поэтому проверяется именно ВЫВОД, и каждый из трёх исходов своим тестом.
Прогоняем САМ СКРИПТ подпроцессом: предмет правки — то, что увидит человек в
консоли сервера, а не поведение функций.
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (AuditLog, Base, FtpTask, FtpTaskStatus,
                        OrderProcessStatus, Platform, PlatformAccount,
                        ProcessedOrder, Product)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_order.py"
ORDER = "5583015069"


@pytest.fixture()
def db_url(tmp_path):
    """Файловая база: скрипт идёт ОТДЕЛЬНЫМ процессом и in-memory не увидит."""
    path = tmp_path / "probe_order.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    session.add(Product(uid_1c="uid-1", article="57020 5299 АВЕР", name="Рубашка Д/р",
                        size="L", color="Тёмно-коричневый", stock_on_hand=3))
    session.add(PlatformAccount(id=1, platform=Platform.wb, name="ИП КАРАМАН",
                                warehouse_id="wh-1"))
    # Времена ставим явно и ровно те, что были на бою: заказ принят и отменён
    # через двадцать секунд. Оставь мы `cancelled_at` пустым, строка про разрыв
    # не печаталась бы вовсе, и проверка на неё молчала бы по неверной причине.
    taken = datetime(2026, 9, 25, 16, 37, 22)
    session.add(ProcessedOrder(account_id=1, order_id=ORDER, uid_1c="uid-1",
                               quantity=1, status=OrderProcessStatus.cancelled,
                               processed_at=taken,
                               cancelled_at=taken + timedelta(seconds=20)))
    session.commit()
    session.close()
    engine.dispose()
    return url, path


def _add(url, obj):
    engine = create_engine(url, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine, autoflush=False)()
    session.add(obj)
    session.commit()
    session.close()
    engine.dispose()


def _run(url):
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    done = subprocess.run([sys.executable, str(SCRIPT), ORDER],
                          capture_output=True, text=True, env=env, cwd=str(ROOT))
    return done.stdout + done.stderr


def _cancel_task(is_test=False):
    return FtpTask(command="CANCEL_MOVEMENT", barcode="2000000000017",
                   warehouse_from="Wildberries_Склад_FBO", warehouse_to="ЦС Склад",
                   quantity=1, order_id=ORDER, account_id=1, platform=Platform.wb,
                   status=FtpTaskStatus.done, result_status="OK",
                   result_detail="ЦБ000001922", is_test=is_test)


def test_a_live_cancel_without_a_human_trace_points_at_the_cabinet(db_url):
    """Главный исход, и ради него скрипт и написан.

    Боевое задание есть, в журнале пусто — значит отмену принёс опрос площадки.
    Для WB это `supplierStatus=cancel`, то есть сборочное задание отменено на
    стороне ПРОДАВЦА: клиентские отмены мы не реверсим вовсе. Не скажи скрипт
    этого вслух, человек пошёл бы искать ошибку у себя.
    """
    url, _ = db_url
    _add(url, _cancel_task())

    out = _run(url)

    # Сравнение по нижнему регистру намеренно: в выводе слово набрано
    # прописными («ОПРОС площадки»), и проверка «not in» в соседних тестах
    # прошла бы на этом молча — то есть стерегла бы пустоту.
    assert "опрос площадки" in out.lower(), out
    assert "supplierStatus=cancel" in out
    assert "в кабинете WB" in out
    # Площадка у заказа известна через кабинет; «-» тут читалось бы как
    # «неизвестно» и отправило бы человека искать несуществующий пробел.
    assert "площадка: wb" in out


def test_the_gap_between_intake_and_cancel_is_printed_as_a_number(db_url):
    """Двадцать секунд и два часа — разные дела, а глазами они неразличимы.

    Оба времени стоят в соседних строках и отличаются только секундами; человек
    вычитает их в уме и ошибается ровно там, где это решает, кто отменял:
    автоматика или человек в кабинете.
    """
    url, _ = db_url
    _add(url, _cancel_task())

    out = _run(url)

    assert "отменён через 20 с после приёма" in out, out


def test_a_simulated_cancel_names_the_person(db_url):
    """Тот же документ в 1С, но причина обратная — и вывод обязан отличаться.

    Ошибись скрипт здесь, человек пошёл бы разбираться в кабинет площадки по
    отмене, которую сам же и завёл.
    """
    url, _ = db_url
    _add(url, _cancel_task(is_test=True))
    _add(url, AuditLog(actor="admin", action="test_simulate_cancel",
                       details=f"TEST-{ORDER}: ok"))

    out = _run(url)

    assert "завёл человек" in out, out
    assert "admin" in out
    assert "опрос площадки" not in out.lower()


def test_no_task_at_all_says_the_document_is_not_ours(db_url):
    """Реверс в 1С есть, а задания нет — значит обработку запускали в самой 1С.

    Промолчи скрипт об этом, вывод «отмен не найдено» читался бы как «всё
    чисто», тогда как документ в 1С стоит и остаток по нему уже изменён.
    """
    url, _ = db_url

    out = _run(url)

    assert "заказывали его не мы" in out, out


def test_a_training_task_is_not_mistaken_for_a_live_one(db_url):
    """Тренировочное задание в 1С не уезжает вовсе.

    Без этой ветки оно попало бы в «опрос площадки», и скрипт объявил бы
    источником кабинет WB по заданию, которого площадка никогда не видела.
    """
    url, _ = db_url
    _add(url, _cancel_task(is_test=True))

    out = _run(url)

    # Проверяем именно ВЫВОД, а не пометку в списке заданий: пометка есть и
    # там, и на ней проверка прошла бы, даже сломайся сам разбор.
    assert "пришёл не отсюда" in out, out
    assert "опрос площадки" not in out.lower()


def test_without_an_order_number_it_refuses_with_an_example(db_url):
    """Скрипт запускают с консоли сервера, где подсказки взять неоткуда."""
    url, _ = db_url
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    done = subprocess.run([sys.executable, str(SCRIPT)],
                          capture_output=True, text=True, env=env, cwd=str(ROOT))

    assert done.returncode != 0
    assert "Укажите номер заказа" in done.stdout

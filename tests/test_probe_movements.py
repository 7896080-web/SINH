"""Движения по ряду: вопрос «почему на этом размере столько» решается списком.

Остаток строки — это сумма её движений, а показать их было негде: «Диагностика»
знает только зависшие задания, отчёт — только сводку, `probe_offset` печатает
заказы и очередь, то есть ПОВОД и СЛЕДСТВИЕ, но не сам документ 1С.

Два свойства тут дороже прочих, и оба про «ответ выглядит ответом».

ПЕРВОЕ: ряд печатается ЦЕЛИКОМ. Артикул один на все размеры, человек держит в
руках артикул, и ответ про один размер молча выдал бы себя за ответ про товар.
`probe_offset` в том же случае ОТКАЗЫВАЕТ — там вопрос про одну строку, и чужие
числа не отличить от нужных; здесь наоборот, и потому это разные скрипты.

ВТОРОЕ: итог по ряду считается ПО ЗНАКУ КОМАНДЫ, и команда без известного знака
не проходит молча. Посчитайся она нулём, строка «проведено движений» соврала бы,
не сказав об этом, — а по ней и решают, сходится ли остаток.

Прогоняем САМ СКРИПТ подпроцессом: предмет правки — то, что увидит человек в
консоли боевого сервера.
"""
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (Barcode, Base, FtpTask, FtpTaskStatus, Platform,
                        PlatformAccount, ProcessedOrder, Product)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_movements.py"

ARTICLE = "39681 SELIANIK МСЛ Свитшот"
# Ряд с НЕЧИСЛОВЫМ соседом: сортировка размеров обязана работать на обоих, а
# ошибку в ней на чисто числовом ряду не поймать — там любой порядок совпадает.
SIZES = ["44", "46", "48", "50", "52", "54", "56"]


@pytest.fixture()
def catalog(tmp_path):
    """Файловая база: скрипт идёт ОТДЕЛЬНЫМ процессом и in-memory не увидит."""
    path = tmp_path / "test_movements.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()

    session.add(PlatformAccount(id=1, platform=Platform.wb, name="ИП ТЕСТ",
                                is_active=True))
    for i, size in enumerate(SIZES):
        session.add(Product(uid_1c=f"u-{size}", article=ARTICLE,
                            name="Свитшот MAVI MELANJ", size=size,
                            color="MAVI MELANJ", stock_on_hand=10 + i, reserve=0))
        session.add(Barcode(barcode=f"200000000{i:04d}", uid_1c=f"u-{size}"))
    session.flush()

    # Размер 50: продажа проведена, отмена проведена — остаток вернулся.
    session.add(FtpTask(command="CREATE_MOVEMENT", barcode="2000000000003",
                        warehouse_from="ЦС Склад",
                        warehouse_to="Wildberries_Склад_FBO",
                        quantity=1, order_id="5583015069", account_id=1,
                        platform=Platform.wb, movement_date=date(2026, 9, 20),
                        status=FtpTaskStatus.done, result_status="OK",
                        result_detail="ЦБ000001910",
                        created_at=datetime(2026, 9, 20, 11, 2)))
    session.add(FtpTask(command="CANCEL_MOVEMENT", barcode="2000000000003",
                        quantity=1, order_id="5583015069", account_id=1,
                        platform=Platform.wb, status=FtpTaskStatus.done,
                        result_status="OK", result_detail="ЦБ000001922",
                        created_at=datetime(2026, 9, 20, 11, 20)))
    session.add(ProcessedOrder(account_id=1, order_id="5583015069",
                               uid_1c="u-50", quantity=1,
                               processed_at=datetime(2026, 9, 20, 11, 2)))

    # Размер 54: продажа висит в timeout — «в пути», остаток занижен.
    session.add(FtpTask(command="CREATE_MOVEMENT", barcode="2000000000005",
                        quantity=2, order_id="777", account_id=1,
                        platform=Platform.wb, status=FtpTaskStatus.timeout,
                        created_at=datetime(2026, 9, 25, 8, 0)))
    session.commit()
    session.close()
    engine.dispose()
    return url


def _run(url, *args):
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    done = subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, env=env, cwd=str(ROOT))
    return done.stdout + done.stderr


def test_the_whole_size_run_is_printed_not_one_row(catalog):
    """Ответ про один размер выдал бы себя за ответ про товар."""
    out = _run(catalog, "39681")

    assert f"Строк 1С: {len(SIZES)}" in out, out
    for size in SIZES:
        assert f"РАЗМЕР {size}" in out, f"нет размера {size}\n{out}"


def test_sizes_come_out_in_order(catalog):
    """Ряд сверяют с витриной глазами, и порядок — часть ответа."""
    out = _run(catalog, "39681")
    places = [out.index(f"РАЗМЕР {size}") for size in SIZES]

    assert places == sorted(places), f"размеры вышли не по порядку\n{out}"


def test_a_movement_names_the_1c_document(catalog):
    """Номер документа — то, по чему строку находят в самой 1С."""
    out = _run(catalog, "39681")

    assert "ЦБ000001910" in out, out
    assert "ЦБ000001922" in out
    assert "Wildberries_Склад_FBO" in out
    assert "5583015069" in out


def test_an_open_task_is_counted_as_in_flight(catalog):
    """Незакрытое задание занижает остаток, и это главное, что ищут скриптом.

    Сводка называет И число заданий, И ШТУКИ. По строке «в пути» считается в
    штуках (`_in_flight_adjustment`), и два разных «в пути» рядом читались бы
    как расхождение там, где его нет: одно задание на две единицы.
    """
    out = _run(catalog, "39681")

    assert "незакрытых заданий 1С («в пути»): 1 на 2 шт" in out, out


def test_the_run_total_adds_movements_by_sign(catalog):
    """Продажа минус, отмена плюс: по этой строке и решают, сходится ли остаток.

    По размеру 50 проведены обе, значит вклад нулевой; задание в `timeout` в
    итог не входит вовсе — документа может и не быть.
    """
    out = _run(catalog, "39681")

    assert "проведено движений на остаток ЦС: +0 шт" in out, out


def test_a_command_without_a_known_sign_says_so(catalog):
    """Посчитайся она нулём, итог соврал бы, не сказав об этом."""
    engine = create_engine(catalog, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine, autoflush=False)()
    session.add(FtpTask(command="NEW_UNKNOWN_COMMAND", barcode="2000000000000",
                        quantity=5, order_id="n1", status=FtpTaskStatus.done,
                        result_status="OK"))
    session.commit()
    session.close()
    engine.dispose()

    out = _run(catalog, "39681")

    assert "NEW_UNKNOWN_COMMAND" in out
    assert "команды без известного знака" in out, out


def test_a_barcode_finds_the_whole_run(catalog):
    """Спросили про единицу — отвечать надо про ряд: заказ приходит по баркоду."""
    out = _run(catalog, "2000000000003")

    assert f"Строк 1С: {len(SIZES)}" in out, out
    assert "ЦБ000001910" in out


def test_the_size_argument_narrows_to_one_row(catalog):
    """Ряд бывает длинным, а вопрос — про один размер."""
    out = _run(catalog, "39681", "50")

    assert "Строк 1С: 1" in out, out
    assert "РАЗМЕР 50" in out
    assert "РАЗМЕР 44" not in out


def test_a_missing_size_is_refused_with_the_list(catalog):
    """Отказ называет, что есть: иначе человек решит, что товара нет вовсе."""
    out = _run(catalog, "39681", "62")

    assert "размера «62» среди них НЕТ" in out, out
    assert "44" in out and "56" in out


def test_an_unknown_article_is_refused_plainly(catalog):
    out = _run(catalog, "нет-такого-артикула")

    assert "не найдено" in out, out


def test_the_output_warns_about_the_timezone(catalog):
    """Время в базе UTC, документы 1С — по Москве.

    Человек, сверяющий список с журналом 1С, иначе ищет движение на три часа не
    там, а не найдя — решает, что его нет.
    """
    out = _run(catalog, "39681")

    assert "UTC" in out and "UTC+3" in out, out


def test_the_output_warns_that_movements_hang_on_barcodes(catalog):
    """Перевязка баркода меняет список в обе стороны — список не полон молча."""
    out = _run(catalog, "39681")

    assert "ПО БАРКОДАМ" in out, out

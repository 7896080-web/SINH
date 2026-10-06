"""Качание остатка: разбор обязан называть СЛАГАЕМОЕ, а не «сверка сломалась».

06.10 на «Крупных расхождениях со складом 1С за сутки» стояли ПАРЫ строк по
одним и тем же позициям: в 12:33 остаток 8 -> 16 (+8), в 13:33 16 -> 8 (-8), и
так час за часом по десятку позиций. Страница показывает только ИТОГ, а сверка
считает его из трёх слагаемых:

    ждали   = наш остаток + «в пути»
    разница = остаток 1С - ждали
    новый   = остаток 1С - «в пути»

Качание даёт ЛЮБОЕ из двух: либо 1С отдаёт разные числа (чужой баркод у строки,
и максимум по баркодам привозит остаток соседнего размера), либо качаются наши
задания («в пути»). По итоговой разнице эти случаи неразличимы ПОЛНОСТЬЮ, а
чинятся по-разному: первый — мэппингом, второй — разбором зависшего задания.
Назови скрипт не то, и человек правит не то.

Прогоняем САМ СКРИПТ подпроцессом: предмет правки — то, что увидит человек в
консоли боевого сервера.
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

from app.models import (Barcode, Base, FtpTask, FtpTaskStatus, Product,
                        ReconciliationClassification, ReconciliationLog)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_reconcile.py"

BASE = datetime(2026, 10, 5, 6, 33)


def _log(uid, hour, ours, flight, actual):
    expected = ours + flight
    return ReconciliationLog(
        uid_1c=uid, checked_at=BASE + timedelta(hours=hour),
        python_stock=ours, in_flight=flight, expected_1c=expected,
        actual_1c=actual, delta=actual - expected,
        classification=ReconciliationClassification.normal)


@pytest.fixture()
def base(tmp_path):
    """Файловая база: скрипт идёт ОТДЕЛЬНЫМ процессом и in-memory не увидит."""
    path = tmp_path / "test_reconcile.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine, autoflush=False)()

    # Размер 52 — боевой случай 06.10: своя цифра 11, чужая 15, и они сменяют
    # друг друга каждый час. Чужой баркод лежит у 52, хотя остаток 15 — у 50.
    s.add(Product(uid_1c="u-52", article="2617 C24-2317CQ", size="52",
                  color="BRICK RED", name="Куртка тинсулейт BRICK RED",
                  stock_on_hand=11))
    s.add(Product(uid_1c="u-50", article="2617 C24-2317CQ", size="50",
                  color="BRICK RED", name="Куртка тинсулейт BRICK RED",
                  stock_on_hand=15))
    s.add(Barcode(barcode="200111", uid_1c="u-52"))
    s.add(Barcode(barcode="200222", uid_1c="u-50"))
    s.add(Barcode(barcode="200333", uid_1c="u-52"))

    for i, actual in enumerate([11, 15, 11, 15, 11, 15]):
        ours = 11 if actual == 15 else 15
        s.add(_log("u-52", i, ours, 0, actual))

    # Размер 50 — ровно обратный случай: 1С отдаёт одно и то же (15), качаются
    # НАШИ задания. Остаток ходит 15 -> 12 -> 15 именно потому, что «в пути»
    # то считается, то нет: ждали 18, получили 15, записали 15 - 3 = 12; через
    # час задание в счёт не попало, ждали 12, получили 15, вернули 15.
    for i, flight in enumerate([0, 3, 0, 3, 0, 3]):
        ours = 15 if flight else 12
        s.add(_log("u-50", i, ours, flight, 15))
    s.add(FtpTask(command="CREATE_MOVEMENT", barcode="200222", quantity=3,
                  status=FtpTaskStatus.timeout, order_id="TEST-1",
                  created_at=BASE, is_test=False))

    s.commit()
    s.close()
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


def test_a_swinging_platform_number_is_named(base):
    """1С отдаёт разное — значит мэппинг, и сказать надо именно это."""
    out = _run(base, "2617", "52", "--hours", "240")

    assert "КАЧАЕТСЯ ОСТАТОК 1С: 11 <-> 15" in out, out
    assert "МАКСИМУМ" in out, "следствие обязано быть названо\n" + out
    assert "КАЧАЕТСЯ «В ПУТИ»" not in out, (
        "по этой строке задания стабильны — второй диагноз здесь ложный\n" + out)


def test_a_swinging_in_flight_is_named_separately(base):
    """А тут наоборот: 1С стабильна, качаются НАШИ задания."""
    out = _run(base, "2617", "50", "--hours", "240")

    assert "КАЧАЕТСЯ «В ПУТИ»: 0 <-> 3" in out, out
    assert "КАЧАЕТСЯ ОСТАТОК 1С" not in out, (
        "1С отдавала одно и то же — диагноз не тот\n" + out)
    assert "timeout" in out, "зависшее задание обязано быть видно\n" + out


def test_the_slagaemye_are_printed_not_just_the_result(base):
    """Страница показывает итог, скрипт обязан показать, ИЗ ЧЕГО он сложен.

    Без столбцов «наш», «в пути», «1С дала» вывод повторял бы страницу и не
    отвечал бы на вопрос, ради которого его запускают.
    """
    out = _run(base, "2617", "52", "--hours", "240")

    for column in ("наш", "в пути", "ждали", "1С дала", "разница"):
        assert column in out, f"нет колонки «{column}»\n{out}"
    assert "05.10 06:33:00" in out, "строки сверки по часам обязаны быть\n" + out


def test_the_row_with_a_foreign_barcode_stands_out(base):
    """Перекос баркодов виден только РЯДОМ целиком.

    По одной строке три баркода выглядят нормально; а что у соседнего размера
    их один, видно лишь в таблице ряда — и это ровно тот перекос, из-за
    которого максимум привозит чужой остаток.
    """
    out = _run(base, "2617", "--hours", "240")

    assert "РЯД ЦЕЛИКОМ" in out, out
    assert "баркодов 2" in out and "баркодов 1" in out, out
    assert "worker.err.log" in out, (
        "у счётчика конфликтов читатель только в логе — надо сказать, где\n" + out)


def test_a_quiet_product_is_not_called_swinging(base):
    """Молчание обязано быть молчанием: без качания диагнозов нет.

    Проверка, называющая качание там, где его нет, приучает не читать вывод, —
    то же правило, по которому отчёт молчит на исправной системе.
    """
    engine = create_engine(base, connect_args={"check_same_thread": False})
    s = sessionmaker(bind=engine, autoflush=False)()
    s.add(Product(uid_1c="u-ok", article="СПОКОЙНЫЙ", size="M", color="СИНИЙ",
                  name="Ровный", stock_on_hand=7))
    for i in range(6):
        s.add(_log("u-ok", i, 7, 0, 7))
    s.commit()
    s.close()
    engine.dispose()

    out = _run(base, "СПОКОЙНЫЙ", "--hours", "240")

    assert "КАЧАЕТСЯ" not in out, out
    assert "строк сверки: 6" in out, out


def test_an_unknown_refinement_is_refused_with_both_rows(base):
    """Правило общее с probe_movements: отказ называет И размеры, И цвета.

    Функция там же и взята (`_classify_filters`) — повтори её здесь, один и
    тот же вызов на двух скриптах однажды ответил бы по-разному.
    """
    out = _run(base, "2617", "ФИОЛЕТОВЫЙ")

    assert "ни размер, ни цвет" in out, out
    assert "размеры:" in out and "цвета:" in out, out


def test_nothing_found_says_so(base):
    out = _run(base, "НЕТ-ТАКОГО")

    assert "не найдено" in out, out


def test_hours_needs_a_number(base):
    """Отказ, а не молчаливое умолчание: человек просил другую глубину."""
    out = _run(base, "2617", "--hours", "сутки")

    assert "--hours требует числа" in out, out


def test_a_product_without_reconciliation_says_so(base):
    """«Сверка не проходила» и «расхождений нет» — РАЗНЫЕ утверждения.

    Пустая таблица читалась бы как «всё сошлось», тогда как сверка по строке
    могла не идти вовсе: товара нет в снимке 1С, и это сама по себе находка.
    """
    engine = create_engine(base, connect_args={"check_same_thread": False})
    s = sessionmaker(bind=engine, autoflush=False)()
    s.add(Product(uid_1c="u-new", article="НОВЫЙ", size="S", color="БЕЛЫЙ",
                  name="Без сверки", stock_on_hand=3))
    s.commit()
    s.close()
    engine.dispose()

    out = _run(base, "НОВЫЙ")

    assert "не проходила ни разу" in out, out


def test_it_says_it_only_reads(base):
    out = _run(base, "2617")

    assert "только читает" in out, out


def test_ordinary_stock_movement_is_not_a_swing(base):
    """Обычная торговля меняет остаток КАЖДЫЙ час — и это не качание.

    Разница между ними ровно одна: качание ВОЗВРАЩАЕТ прежнее число, движение
    склада идёт дальше. Проверь скрипт «значение изменилось», и он объявил бы
    качанием любой продающийся товар — то есть кричал бы на всём каталоге, а
    проверка, срабатывающая на норме, кончается тем, что её перестают читать.
    """
    engine = create_engine(base, connect_args={"check_same_thread": False})
    s = sessionmaker(bind=engine, autoflush=False)()
    s.add(Product(uid_1c="u-sell", article="ПРОДАЖИ", size="L", color="ЧЁРНЫЙ",
                  name="Ходовой", stock_on_hand=2))
    for i, actual in enumerate([20, 17, 15, 11, 6, 2]):
        s.add(_log("u-sell", i, actual, 0, actual))
    s.commit()
    s.close()
    engine.dispose()

    out = _run(base, "ПРОДАЖИ", "--hours", "240")

    assert "КАЧАЕТСЯ" not in out, (
        "монотонная распродажа объявлена качанием\n" + out)


def test_a_one_time_step_is_not_a_swing(base):
    """Остаток изменился ОДИН раз и стоит — это поставка, а не качание.

    Значений тоже два, как при качании, и отличает их только одно: качание
    ВОЗВРАЩАЕТ прежнее число снова и снова. Объяви скрипт качанием всякую пару
    значений, и первая же поставка выглядела бы поломкой сверки.
    """
    engine = create_engine(base, connect_args={"check_same_thread": False})
    s = sessionmaker(bind=engine, autoflush=False)()
    s.add(Product(uid_1c="u-step", article="ПОСТАВКА", size="XL", color="СЕРЫЙ",
                  name="Пришла партия", stock_on_hand=15))
    for i, actual in enumerate([11, 11, 11, 15, 15, 15]):
        s.add(_log("u-step", i, actual, 0, actual))
    s.commit()
    s.close()
    engine.dispose()

    out = _run(base, "ПОСТАВКА", "--hours", "240")

    assert "КАЧАЕТСЯ" not in out, "разовая ступенька объявлена качанием\n" + out


def test_a_one_sided_gap_is_not_a_swing(base):
    """Расхождение ВСЕГДА в одну сторону — перекос, а не качание.

    Час за часом 1С даёт на 4 больше, сверка это применяет, в следующий раз
    снова на 4 больше. Строки с нулевой разницей между ними — это часы, когда
    сверка сошлась, и в счёт знака они идти НЕ ДОЛЖНЫ: посчитай скрипт ноль за
    минус, и ровный односторонний перекос читался бы как «плюс, минус, плюс» —
    то есть качание там, где его нет, а чинить надо совсем другое.
    """
    engine = create_engine(base, connect_args={"check_same_thread": False})
    s = sessionmaker(bind=engine, autoflush=False)()
    s.add(Product(uid_1c="u-bias", article="ПЕРЕКОС", size="S", color="ХАКИ",
                  name="Всегда больше", stock_on_hand=20))
    for i in range(8):
        # Чётный час — разница +4, нечётный — сверка сошлась.
        if i % 2 == 0:
            s.add(_log("u-bias", i, 16 + i, 0, 20 + i))
        else:
            s.add(_log("u-bias", i, 20 + i, 0, 20 + i))
    s.commit()
    s.close()
    engine.dispose()

    out = _run(base, "ПЕРЕКОС", "--hours", "240")

    assert "КАЧАЕТСЯ" not in out, (
        "односторонний перекос объявлен качанием\n" + out)

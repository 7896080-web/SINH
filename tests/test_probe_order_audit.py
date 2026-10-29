"""Предсказание первого прогона сверки заказов — до того, как в 1С поедут документы.

Сверка ПРОВОДИТ найденное: списывает остаток и создаёт перемещение. Это
осознанный выбор, но сколько заказов живой опрос потерял за всё время, заранее
не знает никто — это и есть то, что сверка выясняет. Потолок объём ограничивает,
а показать его до первого прогона было нечем.

Прогоняем САМ СКРИПТ подпроцессом: предмет правки — то, что увидит человек в
консоли боевого сервера, а не поведение функций. И отдельно — то же самое в
cp1251: на бою консоль однобайтовая, и один символ, которого в ней нет, роняет
вывод посреди строки, то есть ровно там, куда человек смотрит, когда уже беда.
"""
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (Barcode, Base, OrderProcessStatus, Platform,
                        PlatformAccount, ProcessedOrder, Product, SyncSetting)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_order_audit.py"

ON = "2000000000001"        # пара отмечена — заказ по нему уедет в 1С
OFF = "2000000000002"       # пара НЕ отмечена — норма перехода
ORPHAN = "2000000000003"    # баркода нет в «Баркодах» вовсе

# Заглушка клиента подставляется скрипту через переменную окружения: своих
# ключей площадки в тесте нет и быть не должно.
STUB = '''
import os, sys
from datetime import date, timedelta
from app.workers.platform_clients.base import PlatformOrder

class Stub:
    orders_history_days = 90
    last_truncated = False
    last_unresolved = 0
    def get_orders_since(self, since):
        day = date.today() - timedelta(days=2)
        return [
            PlatformOrder(order_id="O-on", barcode="{ON}", quantity=2,
                          raw_status="new", order_date=day),
            PlatformOrder(order_id="O-off", barcode="{OFF}", quantity=1,
                          raw_status="new", order_date=day),
            PlatformOrder(order_id="O-orphan", barcode="{ORPHAN}", quantity=1,
                          raw_status="new", order_date=day),
            PlatformOrder(order_id="O-dead", barcode="{ON}", quantity=1,
                          raw_status="cancel", order_date=day),
            PlatformOrder(order_id="O-known", barcode="{ON}", quantity=1,
                          raw_status="new", order_date=day),
        ]
    def is_cancelled_status(self, raw_status):
        return raw_status == "cancel"

import app.workers.client_factory as factory
factory.build_client = lambda db, account_id: Stub()
'''


@pytest.fixture()
def feed(tmp_path):
    """Кабинет, три пары и один заказ, который мы уже знаем.

    Файловая база: скрипт идёт ОТДЕЛЬНЫМ процессом и in-memory не увидит.
    """
    path = tmp_path / "test_audit.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()

    session.add(PlatformAccount(id=1, platform=Platform.wb, name="ИП КАРАМАН",
                                warehouse_id="wh", is_active=True))
    for uid, article, barcode, enabled in (("u-on", "4033 Неро", ON, True),
                                           ("u-off", "4052 b", OFF, False)):
        session.add(Product(uid_1c=uid, article=article, name=article + " SIYAH",
                            size="3XL", color="SIYAH", stock_on_hand=20))
        session.add(Barcode(barcode=barcode, uid_1c=uid))
        session.add(SyncSetting(uid_1c=uid, account_id=1, enabled=enabled,
                               min_threshold=0))
    session.add(ProcessedOrder(account_id=1, order_id="O-known", uid_1c="u-on",
                               quantity=1, status=OrderProcessStatus.processed))
    session.commit()
    session.close()
    engine.dispose()
    return url, path


def _run(url, *args, encoding="utf-8"):
    """Запустить скрипт и прочитать вывод ТЕМ ЖЕ, чем попросили писать.

    Кодировка задаётся с ОБЕИХ сторон и всегда: у перенаправленного вывода
    питон берёт её из локали дочернего процесса — здесь UTF-8, на боевом
    Windows cp1251, — и читай мы его чем-то своим, тест падал бы на мозаике из
    наших же русских строк, причём только на сервере, в накате.
    """
    stub = Path(url.replace("sqlite:///", "")).parent / "sitecustomize_stub.py"
    stub.write_text(STUB.replace("{ON}", ON).replace("{OFF}", OFF)
                    .replace("{ORPHAN}", ORPHAN), encoding="utf-8")
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    env["PYTHONIOENCODING"] = encoding
    done = subprocess.run(
        [sys.executable, "-c",
         f"exec(open({str(stub)!r}, encoding='utf-8').read());"
         f"exec(compile(open({str(SCRIPT)!r}, encoding='utf-8').read(),"
         f" {str(SCRIPT)!r}, 'exec'), {{'__name__': '__main__',"
         f" '__file__': {str(SCRIPT)!r}}})",
         *args],
        capture_output=True, encoding=encoding, errors="replace",
        env=env, cwd=str(ROOT))
    return done.stdout + done.stderr


def test_it_says_what_would_go_to_1c_and_what_it_would_skip(feed):
    """Главное число — «уедет в 1С», и рядом обязаны стоять остальные три.

    Одно число без разбивки читается как «сверка тронет весь этот поток». А
    поток во время перехода почти весь по НЕотмеченным парам, и их сверка не
    трогает вовсе — именно это ограничение и делает механизм безопасным.
    """
    url, _path = feed
    out = _run(url)

    assert "УЕДЕТ В 1С перемещениями:      1" in out, out
    assert "пары не отмечены):  1" in out, out
    assert "баркод не сопоставлен:         1" in out, out
    assert "отменены площадкой:            1" in out, out
    assert "уже есть у нас:                1" in out, out
    assert "заказов в лентах:              5" in out, out


def test_it_changes_nothing(feed):
    """Скрипт для того и нужен, чтобы посмотреть ДО.

    Проверяем следствием: ни остаток не тронут, ни заказ не заведён, ни задание
    в 1С не создано. И отдельно — конфликт сопоставления по незнакомому
    баркоду: боевой путь его ЗАВОДИТ (`resolve_barcode` пишет), и ровно поэтому
    скрипт спрашивает баркоды напрямую, а не через него.
    """
    from app.models import FtpTask, MappingConflict

    url, path = feed
    _run(url)

    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        assert session.query(Product).filter(
            Product.uid_1c == "u-on").first().stock_on_hand == 20
        assert session.query(ProcessedOrder).count() == 1
        assert session.query(FtpTask).count() == 0
        assert session.query(MappingConflict).count() == 0
    finally:
        session.close()
        engine.dispose()


def test_it_says_the_split_is_a_forecast(feed):
    """Предсказание, названное обещанием, хуже отсутствующего.

    Боевой путь идёт через `resolve_barcode`, который умеет связать баркод
    через каталог площадки, — то есть «не сопоставлен» у скрипта и у сверки на
    незнакомых баркодах могут разойтись. Промолчи он об этом, по числу
    принимали бы решение как по точному.
    """
    url, _path = feed
    out = _run(url)

    assert "ПРЕДСКАЗАНИЕ" in out, out
    assert "resolve_barcode" in out, out


def test_zero_is_not_the_same_as_fine(feed):
    """Ноль в «уедет» не значит «сверка не нужна».

    Проверка, которой нечего смотреть, молчит так же, как исправный набор —
    этот урок в проекте оплачен не раз. Потери появляются тогда, когда кабинет
    гаснет, и до следующего прогона о них не узнаёт никто.
    """
    url, _path = feed
    out = _run(url)

    assert "НЕ значит" in out, out


def test_the_ceiling_is_named_when_the_first_run_would_not_fit(feed, tmp_path):
    """Больше потолка — скрипт говорит, за сколько суток это сойдётся.

    Иначе человек увидит «уедет 300» при потолке 50 и решит, что всё поедет
    разом.
    """
    url, path = feed
    from app.workers.order_audit import MAX_PER_ACCOUNT

    big = "\n".join(
        f'            PlatformOrder(order_id="O-{i}", barcode="{ON}", '
        f'quantity=1, raw_status="new", order_date=day),'
        for i in range(MAX_PER_ACCOUNT + 3))
    stub = Path(path).parent / "sitecustomize_stub.py"
    out = _run(url)          # создаст заглушку
    stub.write_text(
        stub.read_text(encoding="utf-8").replace(
            f'            PlatformOrder(order_id="O-on", barcode="{ON}", quantity=2,\n'
            f'                          raw_status="new", order_date=day),', big),
        encoding="utf-8")
    env = dict(os.environ)
    env.update({"DATABASE_URL": url, "SESSION_SECRET": "x" * 32,
                "SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "PYTHONIOENCODING": "utf-8"})
    done = subprocess.run(
        [sys.executable, "-c",
         f"exec(open({str(stub)!r}, encoding='utf-8').read());"
         f"exec(compile(open({str(SCRIPT)!r}, encoding='utf-8').read(),"
         f" {str(SCRIPT)!r}, 'exec'), {{'__name__': '__main__',"
         f" '__file__': {str(SCRIPT)!r}}})"],
        capture_output=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(ROOT))
    out = done.stdout + done.stderr

    assert "больше потолка" in out, out
    assert f"уедет {MAX_PER_ACCOUNT}" in out, out


def test_it_survives_the_production_console(feed):
    """cp1251 — та самая консоль боевого сервера.

    Запускают скрипт ИМЕННО когда уже беда, и трассировка `UnicodeEncodeError`
    вместо отчёта читается как поломка самого скрипта: проверку отложат вовсе.
    26.09 на этом встал накат — пять скриптов падали на `->` и `~`.

    В данных теста нарочно есть ЧУЖОЙ символ, которого в cp1251 нет (турецкая
    «İ» — названия товаров у нас турецкие): без него прогон зелен и с
    выключенной защитой, потому что свои строки мы и так держим в её пределах.
    """
    url, path = feed
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(bind=engine, autoflush=False)()
    product = session.query(Product).filter(Product.uid_1c == "u-on").first()
    product.name = "İNDİGO MELANJ"
    session.commit()
    session.close()
    engine.dispose()

    out = _run(url, "--orders", encoding="cp1251")

    assert "Traceback" not in out, out
    assert "УЕДЕТ В 1С перемещениями:      1" in out, out
    # Чужой символ обязан ДОЕХАТЬ до вывода, иначе проверка сторожит пустоту:
    # свои строки мы и так держим в пределах cp1251. В списке он стоит в
    # названии товара, то есть на той самой строке, где `errors="replace"` и
    # работает — она печатается, но «İ» в ней заменена.
    assert "O-on" in out, "списка заказов нет — чужой символ до вывода не дошёл"
    assert "NDGO MELANJ" in out.replace("?", ""), out

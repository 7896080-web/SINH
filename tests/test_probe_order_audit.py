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
OFF = "2000000000002"       # пара НЕ отмечена — уедет, но с пометкой
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


def _run(url, *args, encoding="utf-8", orders=None):
    """Запустить скрипт и прочитать вывод ТЕМ ЖЕ, чем попросили писать.

    Кодировка задаётся с ОБЕИХ сторон и всегда: у перенаправленного вывода
    питон берёт её из локали дочернего процесса — здесь UTF-8, на боевом
    Windows cp1251, — и читай мы его чем-то своим, тест падал бы на мозаике из
    наших же русских строк, причём только на сервере, в накате.
    """
    stub = Path(url.replace("sqlite:///", "")).parent / "sitecustomize_stub.py"
    body = STUB.replace("{ON}", ON).replace("{OFF}", OFF).replace("{ORPHAN}", ORPHAN)
    if orders is not None:
        # Подмена ленты идёт ЧЕРЕЗ ЭТОТ ЖЕ путь, а не вторым `subprocess.run`:
        # второй запуск однажды разошёлся бы кодировкой с читателем, и тест
        # падал бы мозаикой на боевом Windows. Ровно это и поймал сканер
        # `tests/test_subprocess_encoding.py`.
        body = body.replace("        return [", "        return [\n" + orders + "\n")
    stub.write_text(body, encoding="utf-8")
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
    """Главное число — «уедет в 1С», и рядом обязаны стоять остальные.

    Одно число без разбивки читается как «сверка тронет весь этот поток и
    тронет одинаково». А тронет она по-разному: по отмеченной паре новый
    остаток уйдёт на площадку сам, по неотмеченной заведётся аномалия и решать
    придётся человеку — и сколько таких, он хочет знать ДО прогона.
    """
    url, _path = feed
    out = _run(url)

    assert "УЕДЕТ В 1С перемещениями:      2" in out, out
    assert "из них по парам БЕЗ галочки: 1" in out, out
    assert "пропустит (баркод не сопоставлен): 1" in out, out
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


def test_the_ceiling_is_named_when_the_first_run_would_not_fit(feed):
    """Больше потолка — скрипт говорит, что уедет за прогон, а что в следующие.

    Иначе человек увидит «уедет 300» при потолке 50 и решит, что всё поедет
    разом. А решает он по этому числу, стоит ли вообще запускать сверку на
    бою — то есть ответ, выглядящий ответом, тут дороже всего.
    """
    from app.workers.order_audit import MAX_PER_ACCOUNT

    url, _path = feed
    extra = "\n".join(
        f'            PlatformOrder(order_id="O-{i}", barcode="{ON}", '
        f'quantity=1, raw_status="new", order_date=day),'
        for i in range(MAX_PER_ACCOUNT + 3))

    out = _run(url, orders=extra)

    assert "больше потолка" in out, out
    assert f"уедет {MAX_PER_ACCOUNT}" in out, out
    assert "в следующие сутки" in out, out


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
    assert "УЕДЕТ В 1С перемещениями:      2" in out, out
    # Чужой символ обязан ДОЕХАТЬ до вывода, иначе проверка сторожит пустоту:
    # свои строки мы и так держим в пределах cp1251. В списке он стоит в
    # названии товара, то есть на той самой строке, где `errors="replace"` и
    # работает — она печатается, но «İ» в ней заменена.
    assert "O-on" in out, "списка заказов нет — чужой символ до вывода не дошёл"
    assert "NDGO MELANJ" in out.replace("?", ""), out


def test_the_rows_say_which_pair_has_no_tick(feed):
    """«Уедет 12» без имён отвечает не на тот вопрос.

    На бою 29.10 отобранных было двенадцать, и все по парам без галочки — а
    посмотреть, ЧТО это за товары, было нечем. Теперь уедут они все, но
    последствие у них разное: по отмеченной паре остаток уйдёт на площадку
    сам, по неотмеченной заведётся аномалия и решать придётся человеку.
    Пометка стоит ПРЯМО В СТРОКЕ, а не вторым списком: решают по этим же
    строкам, и искать их во втором месте никто не станет.
    """
    url, _path = feed
    out = _run(url, "--orders")

    assert "-> O-off [БЕЗ ГАЛОЧКИ]" in out, out
    # Обратная половина: у отмеченной пары пометки быть не должно, иначе она
    # ничего не различает.
    assert "-> O-on [БЕЗ ГАЛОЧКИ]" not in out, out
    assert "-> O-on " in out, out
    assert "(пропуск: баркод не сопоставлен) O-orphan" in out, out
    # И товар назван по имени, а не одним баркодом: по баркоду строку не узнать.
    assert "4052 b" in out, out
    # У несопоставленного товара в номенклатуре нет вовсе — и это сказано.
    assert "товара в номенклатуре нет" in out, out
    # Отменённый в список не идёт: его сверка не отбирала вовсе.
    assert "O-dead" not in out, out


def test_the_unmanaged_pair_gets_its_consequence(feed):
    """Число без следствия пролистывают — скрипт говорит, что из него выйдет.

    «БЕЗ ГАЛОЧКИ» — это НЕ норма перехода: вторая система выключена, и число
    на такой карточке не обновляет больше никто. Сам заказ сверка проведёт, но
    карточка продаст ещё, и решить это может только человек.
    """
    url, _path = feed
    out = _run(url, "--orders")

    assert "НЕ норма перехода" in out, out
    assert "не обновляет больше никто" in out, out
    assert "«Аномалиях»" in out, out


def test_without_an_unmanaged_pair_the_warning_is_silent(feed):
    """А когда все пары отмечены — молчим: лишний крик приучает не читать вывод."""
    from app.models import SyncSetting

    url, path = feed
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(bind=engine, autoflush=False)()
    session.query(SyncSetting).filter(
        SyncSetting.uid_1c == "u-off", SyncSetting.account_id == 1
    ).first().enabled = True
    session.commit()
    session.close()
    engine.dispose()

    out = _run(url)

    assert "из них по парам БЕЗ галочки: 0" in out, out
    assert "НЕ норма перехода" not in out, out


def test_the_totals_point_at_the_key_that_shows_them(feed):
    """Список есть, а про ключ никто не сказал — значит списка для человека нет.

    Ровно то правило, по которому страница, к которой ведёт один элемент на
    другой странице, считается отсутствующей.
    """
    url, _path = feed
    assert "--orders" in _run(url), "итоги не говорят, чем посмотреть строки"


def test_nothing_to_skip_means_no_advice(feed):
    """А когда смотреть по именам нечего — молчим: подсказка учит не читать вывод.

    Состояние собираем честное: про заказ по неотмеченной паре и про заказ с
    несопоставленным баркодом мы УЖЕ знаем, значит отбор их не возьмёт, а
    единственный оставшийся уедет по отмеченной паре.
    """
    url, path = feed
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(bind=engine, autoflush=False)()
    for order_id in ("O-off", "O-orphan"):
        session.add(ProcessedOrder(account_id=1, order_id=order_id, uid_1c=None,
                                   quantity=1,
                                   status=OrderProcessStatus.processed))
    session.commit()
    session.close()
    engine.dispose()

    out = _run(url)

    assert "УЕДЕТ В 1С перемещениями:      1" in out, out
    assert "из них по парам БЕЗ галочки: 0" in out, out
    assert "--orders" not in out, out

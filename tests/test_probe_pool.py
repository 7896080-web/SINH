"""Пул баркодов карточки: кто её делит и что из-за этого уедет наружу.

Повод живой. 28.10 на бою по `4033 Неро Джемпер 50/50` пять строк 3XL висели в
критичной находке «площадка держит больше, чем мы отправили»: отправили 10 —
держит 28, отправили 0 — держит 28. Разобрать это по одной строке нельзя было
ничем: каждая строка про свой товар и каждая по-своему права, а 09.10 на те же
карточки ушли СУММЫ (41, 27, 17, 28, 28), то есть соседи тогда были. Ответ
лежал в пуле баркодов кабинета, а посмотреть пул было нечем.

Правил два, они живут в разных местах и ломаются порознь, поэтому и проверок
здесь две группы:

  * ячейка остатка (`stock_cell.members`/`shares`) — кто жилец и сколько даёт
    каждый; вклад заблокированного выключателем НОЛЬ, и молчать об этом нельзя:
    именно так «сложение сломалось» выглядит снаружи;
  * однозначность пула (`catalog_sync`) — привяжет ли автопривязка новые
    баркоды этой карточки или они уйдут в конфликты.

Прогоняем САМ СКРИПТ подпроцессом: предмет правки — то, что увидит человек в
консоли боевого сервера, а не поведение функций.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (Barcode, Base, Platform, PlatformAccount,
                        PlatformCatalogItem, Product, SyncSetting)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_pool.py"

CELL = "111:222"           # external_id карточки WB: nmID:chrtID, то есть размер
OUR = "2000000000001"
NEIGHBOUR = "2000000000002"
ORPHAN = "2000000000003"   # в каталоге площадки есть, в 1С не привязан


def _product(session, uid, article, stock, broadcast=True):
    session.add(Product(uid_1c=uid, article=article,
                        name=article + " SIYAH", size="3XL", color="SIYAH",
                        stock_on_hand=stock, reserve=0,
                        broadcast_enabled=broadcast))


@pytest.fixture()
def shared_card(tmp_path):
    """Два товара 1С на одной карточке каб.1 плюс кабинет без каталога.

    Файловая база: скрипт идёт ОТДЕЛЬНЫМ процессом и in-memory не увидит.
    """
    path = tmp_path / "test_pool.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()

    session.add(PlatformAccount(id=1, platform=Platform.wb, name="ИП КАРАМАН",
                                is_active=True))
    session.add(PlatformAccount(id=2, platform=Platform.wb, name="ИП ВТОРОЙ",
                                is_active=True))
    _product(session, "u-a", "4033 Неро", 16)
    _product(session, "u-b", "4052 b", 18)
    session.add(Barcode(barcode=OUR, uid_1c="u-a"))
    session.add(Barcode(barcode=NEIGHBOUR, uid_1c="u-b"))
    for uid in ("u-a", "u-b"):
        for account_id in (1, 2):
            session.add(SyncSetting(uid_1c=uid, account_id=account_id,
                                    enabled=True, min_threshold=0))
    # Каталог ТОЛЬКО у каб.1: во втором карточки нет вовсе.
    for barcode in (OUR, NEIGHBOUR, ORPHAN):
        session.add(PlatformCatalogItem(account_id=1, external_id=CELL,
                                        barcode=barcode, article="WB-4033"))
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
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    env["PYTHONIOENCODING"] = encoding
    done = subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, encoding=encoding,
                          errors="replace", env=env, cwd=str(ROOT))
    return done.stdout + done.stderr


def _switch_off(path, uid):
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(bind=engine, autoflush=False)()
    product = session.query(Product).filter(Product.uid_1c == uid).first()
    product.broadcast_enabled = False
    session.commit()
    session.close()
    engine.dispose()


# ------------------------------------------------------- правило ячейки

def test_the_neighbour_on_the_card_is_named_with_the_sum(shared_card):
    """Главное: на карточке два товара 1С, и уедет СУММА.

    Снаружи это выглядит как отказ площадки («отправили 16, держит 34»), и по
    одной строке не находится никогда.
    """
    url, _path = shared_card
    out = _run(url, OUR)

    assert "ЖИЛЬЦОВ 2" in out, out
    assert "СУММА 34" in out, "сумма по ячейке не названа:\n" + out
    assert "4052 b" in out, "сосед по карточке не назван по имени"


def test_a_muted_neighbour_is_called_out_not_silently_dropped(shared_card):
    """Сосед с выключенной трансляцией даёт НОЛЬ — и это надо сказать вслух.

    Это и есть случай 28.10: 09.10 на карточку ушла сумма 28, а 24.10 — только
    наши 10, потому что сосед замолчал. Промолчи скрипт, человек читал бы
    «жильцов 2, уедет 16» и не понял бы, куда делись восемнадцать штук, —
    ответ выглядел бы ответом.
    """
    url, path = shared_card
    _switch_off(path, "u-b")

    out = _run(url, OUR)

    assert "ЖИЛЬЦОВ 2" in out, "сосед обязан остаться жильцом ячейки"
    assert "вклад НОЛЬ при остатке 18" in out, out
    assert "выключател" in out, "причина нулевого вклада не названа"
    assert "СУММА 16" in out, "сумма обязана быть без вклада соседа"


def test_a_single_tenant_row_says_so_plainly(shared_card):
    """Одиночная строка (а это весь каталог) не должна выглядеть как общая."""
    url, path = shared_card
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(bind=engine, autoflush=False)()
    session.query(PlatformCatalogItem).filter(
        PlatformCatalogItem.barcode.in_([NEIGHBOUR, ORPHAN])).delete(
            synchronize_session=False)
    session.commit()
    session.close()
    engine.dispose()

    out = _run(url, OUR)

    assert "жилец один" in out, out
    assert "ЖИЛЬЦОВ" not in out


# ------------------------------------------------- правило однозначности пула

def test_an_ambiguous_pool_is_named_with_its_consequence(shared_card):
    """Пул, ведущий к разным товарам 1С, автопривязка не мержит вовсе.

    Следствие отложенное и потому незаметное: новые баркоды этой карточки
    уходят в конфликты и висят там, а остаток по ним не уезжает. Назвать надо
    именно следствие — само слово «неоднозначный» человеку не говорит ничего.
    """
    url, _path = shared_card
    out = _run(url, OUR)

    assert "ПУЛ НЕОДНОЗНАЧЕН" in out, out
    assert "конфликты сопоставления" in out, "следствие не названо"


def test_an_unlinked_barcode_in_a_clean_pool_is_not_an_alarm(shared_card):
    """Обратная сторона: пул из одного товара с непривязанным баркодом — норма.

    Его привяжет ближайшая загрузка каталога (`POOL_GUESS_SOURCE`), и кричать
    тут не на что. Проверка, ругающаяся на исправное, кончается тем, что её
    перестают читать.
    """
    url, path = shared_card
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(bind=engine, autoflush=False)()
    session.query(PlatformCatalogItem).filter(
        PlatformCatalogItem.barcode == NEIGHBOUR).delete(
            synchronize_session=False)
    session.commit()
    session.close()
    engine.dispose()

    out = _run(url, OUR)

    assert "ПУЛ НЕОДНОЗНАЧЕН" not in out, out
    assert "пул однозначен" in out


# ------------------------------------------------------------- нет карточки

def test_a_cabinet_without_the_card_says_what_it_costs(shared_card):
    """Каталога кабинета нет — соседей не видно В ПРИНЦИПЕ.

    Это не «всё в порядке, жилец один»: сосед на карточке может быть, просто мы
    о нём не знаем, и его остаток туда не попадёт. Разница в последствии, и
    потому в тексте.
    """
    url, _path = shared_card
    out = _run(url, OUR)

    assert "карточки в каталоге кабинета НЕТ" in out, out
    assert "загрузкой каталога кабинета" in out, "выход не назван"


def test_silence_is_explained_out_loud(shared_card):
    """Ноль в итогах не значит «всё хорошо», и скрипт обязан это сказать.

    Устаревший каталог выглядит точно так же, как честно одиночная карточка:
    соседа в нём нет. Молчание проверки и отсутствие беды неразличимы, пока об
    этом не написано.
    """
    url, _path = shared_card
    out = _run(url, OUR)

    assert "Нули тут НЕ значат" in out, out
    assert "выигрывает написавший последним" in out


def test_it_survives_the_production_console(shared_card):
    """Боевая консоль пишет в cp1251, и это надо ПРОГНАТЬ, а не грепнуть.

    В вывод попадают ЧУЖИЕ данные: имена кабинетов, названия товаров, артикулы
    с площадки. Один символ вне кодировки уронил бы весь отчёт посреди строки.
    """
    url, _path = shared_card
    out = _run(url, OUR, encoding="cp1251")

    assert "ЖИЛЬЦОВ 2" in out, out
    assert "Нули тут НЕ значат" in out, "вывод оборвался на полпути"

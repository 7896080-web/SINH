"""Сравнение снимков каталога кабинета.

Снимок (`platform_catalog_items`) перезаписывается при каждой выгрузке — истории
нет. Значит «что натворила в кабинете правка витрины» постфактум ответа не
имеет, и копию надо снимать заранее. Отсюда два режима и вся цена ошибки в них.

Сравнивать надо не «что-то поменялось», а ровно две вещи, и обе про остаток:

* **`external_id` — ключ отправки.** Сменился — число уходит по ключу, которого
  на площадке нет, запись закрывается терминально, повтора не будет.
* **Исчезнувший баркод.** По баркодам расчёт разносит продажи площадки на
  товары 1С: пропал — заказы не проведутся, остаток останется завышенным.

Третья проверка не про находки, а про молчание: когда не изменилось ничего,
скрипт обязан сказать это ВСЛУХ. «Ничего не нашлось» и «не искали» выглядят
одинаково, а по этому ответу решают, переотправлять остаток или искать причину
в другом месте.

Прогоняем САМ СКРИПТ подпроцессом: предмет правки — то, что увидит человек.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Platform, PlatformAccount, PlatformCatalogItem

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "catalog_diff.py"


def _engine(path):
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return url, engine


@pytest.fixture()
def cabinet(tmp_path):
    """Кабинет с тремя карточками — база живёт в файле: скрипт идёт отдельным
    процессом и in-memory не увидит."""
    url, engine = _engine(tmp_path / "cat.db")
    s = sessionmaker(bind=engine, autoflush=False)()
    s.add(PlatformAccount(id=1, platform=Platform.kit, name="КИТ"))
    s.add(PlatformAccount(id=2, platform=Platform.ozon, name="ОЗОН"))
    for bc, ext, art in (("2000000000001", "V-1", "3030 L"),
                         ("2000000000002", "V-2", "3030 M"),
                         ("2000000000003", "V-3", "3030 XL")):
        s.add(PlatformCatalogItem(account_id=1, external_id=ext, barcode=bc,
                                  article=art, name="Рубашка"))
    s.commit()
    s.close()
    engine.dispose()
    return url, tmp_path


def _run(url, *args):
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    done = subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, env=env, cwd=str(ROOT))
    return done.stdout + done.stderr


def _session(url):
    engine = create_engine(url, connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, autoflush=False)(), engine


def test_a_changed_key_is_the_first_thing_reported(cabinet):
    """Ключ сменился — остаток уходит в пустоту, и это дороже всего."""
    url, tmp = cabinet
    snap = tmp / "before.json"
    _run(url, "save", "КИТ", str(snap))

    s, engine = _session(url)
    row = s.query(PlatformCatalogItem).filter(
        PlatformCatalogItem.barcode == "2000000000002").first()
    row.external_id = "V-999"
    s.commit(); s.close(); engine.dispose()

    out = _run(url, "diff", "КИТ", str(snap))
    assert "СМЕНИЛИ КЛЮЧ ОТПРАВКИ: 1" in out, out
    assert "V-2  →  V-999" in out
    assert "терминально" in out, "не сказано, чем это кончится"


def test_a_vanished_barcode_is_reported(cabinet):
    """По нему расчёт больше не разнесёт продажи — остаток останется завышенным."""
    url, tmp = cabinet
    snap = tmp / "before.json"
    _run(url, "save", "КИТ", str(snap))

    s, engine = _session(url)
    s.query(PlatformCatalogItem).filter(
        PlatformCatalogItem.barcode == "2000000000003").delete()
    s.commit(); s.close(); engine.dispose()

    out = _run(url, "diff", "КИТ", str(snap))
    assert "ИСЧЕЗЛИ ИЗ КАТАЛОГА: 1" in out, out
    assert "2000000000003" in out


def test_a_new_barcode_is_reported(cabinet):
    url, tmp = cabinet
    snap = tmp / "before.json"
    _run(url, "save", "КИТ", str(snap))

    s, engine = _session(url)
    s.add(PlatformCatalogItem(account_id=1, external_id="V-4",
                              barcode="2000000000004", article="3030 XXL"))
    s.commit(); s.close(); engine.dispose()

    out = _run(url, "diff", "КИТ", str(snap))
    assert "ПОЯВИЛИСЬ: 1" in out, out
    assert "2000000000004" in out


def test_silence_is_said_out_loud(cabinet):
    """«Не нашлось» и «не искали» выглядят одинаково — а решения разные."""
    url, tmp = cabinet
    snap = tmp / "before.json"
    _run(url, "save", "КИТ", str(snap))

    out = _run(url, "diff", "КИТ", str(snap))
    assert "СМЕНИЛИ КЛЮЧ ОТПРАВКИ: 0" in out, out
    assert "НЕ ИЗМЕНИЛИСЬ" in out
    assert "Переотправить остаток" in out, "не сказано, что делать дальше"


def test_a_rename_does_not_count_as_a_key_change(cabinet):
    """Название на отправку не влияет, и путать его с ключом нельзя.

    Иначе каждая косметическая правка карточек читалась бы как «остаток больше
    не уедет», и настоящую смену ключа перестали бы замечать среди шума.
    """
    url, tmp = cabinet
    snap = tmp / "before.json"
    _run(url, "save", "КИТ", str(snap))

    s, engine = _session(url)
    row = s.query(PlatformCatalogItem).filter(
        PlatformCatalogItem.barcode == "2000000000001").first()
    row.name = "Рубашка мужская Awer"
    s.commit(); s.close(); engine.dispose()

    out = _run(url, "diff", "КИТ", str(snap))
    assert "СМЕНИЛИ КЛЮЧ ОТПРАВКИ: 0" in out, out
    assert "ПЕРЕИМЕНОВАНЫ (артикул или название): 1" in out


def test_comparing_against_another_cabinets_snapshot_is_refused(cabinet):
    """Самая правдоподобная ошибка при нескольких файлах — и выглядела бы она
    как «в кабинете поменялось всё»."""
    url, tmp = cabinet
    snap = tmp / "kit.json"
    _run(url, "save", "КИТ", str(snap))

    out = _run(url, "diff", "ОЗОН", str(snap))
    assert "снят с кабинета" in out, out
    assert "СМЕНИЛИ КЛЮЧ" not in out, "чужой снимок всё-таки сравнили"


def test_an_unknown_cabinet_is_refused_with_the_list(cabinet):
    url, tmp = cabinet
    out = _run(url, "save", "ВБ", str(tmp / "x.json"))
    assert "не найден" in out, out
    assert "1:КИТ" in out and "2:ОЗОН" in out


def test_the_saved_snapshot_says_how_fresh_it_is(cabinet):
    """Снимок обновляет только выгрузка каталога. Сохранив позавчерашний, правку
    витрины сравнивали бы с чужими изменениями и приписали бы их ей."""
    url, tmp = cabinet
    out = _run(url, "save", "КИТ", str(tmp / "before.json"))
    assert "выгружен:" in out, out
    assert "загрузите каталог" in out
    saved = json.loads((tmp / "before.json").read_text(encoding="utf-8"))
    assert saved["account_id"] == 1
    assert len(saved["items"]) == 3

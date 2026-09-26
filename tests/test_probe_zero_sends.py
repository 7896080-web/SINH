"""Разбор «на площадке были остатки, а стали нули» обязан называть ВИНОВНИКА.

Вопрос этот про кабинет целиком, а не про строку: пока не видно, одна это
карточка или шестьсот и одним ли событием они обнулились, разбирать нечего.
Ответ при этом обязан различать два случая, которые выглядят одинаково —
и вся цена ошибки здесь:

* ноль по СНЯТОЙ паре — это отзыв, то есть работа человека, и разбирать нечего;
* ноль по паре, где кабинет отмечен И трансляция включена, — это уже сбой, и
  на площадке сейчас продаётся не то, что у нас на складе.

Отдельно — два товара 1С на одну карточку площадки. У Kit пара «товар+склад»
повторяться не может, и такие товары пишут по очереди: у одного 20, у второго
0, на витрине 0. По ОДНОЙ строке это не находится никогда — каждая про свой
товар и каждая по-своему права.

Прогоняем САМ СКРИПТ подпроцессом: предмет правки — то, что увидит человек в
консоли сервера.
"""
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (AuditLog, Base, DispatchQueueItem, DispatchStatus,
                        Platform, PlatformAccount, Product, SyncSetting)
from app.timeutils import now_utc

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_zero_sends.py"


def _product(session, uid, article, size, broadcast):
    session.add(Product(uid_1c=uid, article=article, name="Рубашка", size=size,
                        color="Белый", stock_on_hand=20, reserve=0,
                        broadcast_enabled=broadcast))


def _queue(session, uid, account_id, quantity, sent, reason, when,
           sku=None, status=DispatchStatus.sent):
    session.add(DispatchQueueItem(
        uid_1c=uid, account_id=account_id, quantity=quantity, sent_quantity=sent,
        reason=reason, status=status, sent_sku=sku, created_at=when))


@pytest.fixture()
def cabinet(tmp_path):
    """Файловая база: скрипт идёт ОТДЕЛЬНЫМ процессом и in-memory не увидит."""
    path = tmp_path / "probe_zero.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine, autoflush=False)()

    s.add(PlatformAccount(id=1, platform=Platform.kit, name="КИТ"))
    s.add(PlatformAccount(id=2, platform=Platform.ozon, name="ОЗОН"))

    now = now_utc()
    recent, old = now - timedelta(hours=2), now - timedelta(hours=200)

    # A — отмечен и транслируется, а ушёл ноль. Ровно то, что надо разбирать.
    _product(s, "u-A", "3030-3032", "L", True)
    s.add(SyncSetting(uid_1c="u-A", account_id=1, enabled=True))
    _queue(s, "u-A", 1, 20, 20, "order", now - timedelta(hours=3), sku="V-A")
    _queue(s, "u-A", 1, 20, 0, "order", recent, sku="V-A")

    # B — галочку сняли, ноль ушёл отзывом. Это норма.
    _product(s, "u-B", "3030-3033", "M", False)
    s.add(SyncSetting(uid_1c="u-B", account_id=1, enabled=False))
    _queue(s, "u-B", 1, 5, 0, "broadcast_off", recent, sku="V-B")

    # C и D — РАЗНЫЕ товары 1С на ОДНОЙ карточке площадки.
    _product(s, "u-C", "3030-9001", "S", True)
    _product(s, "u-D", "3030-9002", "S", True)
    s.add(SyncSetting(uid_1c="u-C", account_id=1, enabled=True))
    s.add(SyncSetting(uid_1c="u-D", account_id=1, enabled=True))
    _queue(s, "u-C", 1, 12, 12, "order", recent - timedelta(minutes=5), sku="V-777")
    _queue(s, "u-D", 1, 0, 0, "order", recent, sku="V-777")

    # E — трансляция ВКЛЮЧЕНА, а галочку кабинета сняли: самый частый отзыв.
    # Без этой строки признак «пара отмечена» ничего не различал бы: у B
    # выключено и то и другое, и ответ выходил одинаковым, спрашивай мы про
    # галочку или не спрашивай.
    _product(s, "u-E", "3030-9003", "XL", True)
    s.add(SyncSetting(uid_1c="u-E", account_id=1, enabled=False))
    _queue(s, "u-E", 1, 8, 0, "broadcast_toggled", recent, sku="V-E")

    # Чужой кабинет и запись за пределами окна — в ответ попасть не должны.
    _queue(s, "u-A", 2, 9, 0, "reconciliation", recent, sku="OZ-A")
    _queue(s, "u-A", 1, 7, 0, "excel_import", old, sku="V-A")

    s.add(AuditLog(actor="ruslan", action="products_bulk",
                   details="broadcast_off по отбору 48 шт", created_at=recent))
    s.add(AuditLog(actor="ruslan", action="products_bulk",
                   details="это было давно", created_at=old))
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


def test_it_names_who_put_the_zeros_there(cabinet):
    """Причина постановки в очередь и есть ответ на «кто это сделал»."""
    out = _run(cabinet, "КИТ", "48")
    assert "КАБИНЕТ 1: КИТ" in out, out
    assert "ОТКУДА ВЗЯЛИСЬ НУЛИ" in out
    assert "broadcast_off" in out, "не назван отзыв человеком\n" + out
    assert "order" in out, "не названа продажа\n" + out


def test_a_zero_on_a_live_pair_is_told_apart_from_a_withdrawal(cabinet):
    """Главное различие всего разбора, и оба случая выглядят одинаково."""
    out = _run(cabinet, "КИТ", "48")
    # Живых (отмечен И транслируется) среди обнулённых ровно два: A и D.
    # E сюда не входит намеренно: трансляция у него включена, но галочку
    # кабинета сняли — это отзыв, и разбирать там нечего.
    assert "и то и другое — у 2" in out, out
    assert "трансляция включена у 3" in out, out
    assert "ноль ушёл не отзывом" in out


def test_two_products_on_one_platform_card_are_shown(cabinet):
    """По одной строке это не находится никогда: каждая по-своему права."""
    out = _run(cabinet, "КИТ", "48")
    assert "ОДИН КЛЮЧ — НЕСКОЛЬКО ТОВАРОВ" in out, out
    assert "ключ V-777" in out
    assert "3030-9001" in out and "3030-9002" in out
    # И ключ, по которому товар ОДИН, сюда попасть не должен — иначе раздел
    # перечислял бы весь кабинет и ничего не значил.
    assert "ключ V-A" not in out


def test_nonzero_sends_are_shown_next_to_the_zeros(cabinet):
    """Без них сводка нулей одинакова и при поломке, и при обычной жизни."""
    out = _run(cabinet, "КИТ", "48")
    assert "НЕПУСТЫЕ отправки за то же окно" in out, out
    assert "ушло непустых: 2" in out


def test_another_cabinet_does_not_leak_into_the_answer(cabinet):
    """Ответ про кабинет обязан быть про ЭТОТ кабинет."""
    out = _run(cabinet, "КИТ", "48")
    assert "OZ-A" not in out, "запись чужого кабинета попала в ответ\n" + out
    assert "reconciliation" not in out


def test_the_window_actually_cuts_off_old_rows(cabinet):
    """Окно — не украшение: старый ноль описывает прошлое и путает счёт."""
    out = _run(cabinet, "КИТ", "6")
    assert "excel_import" not in out, "запись вне окна попала в ответ\n" + out
    assert "это было давно" not in out


def test_bulk_actions_are_listed_so_zeros_can_be_matched_to_them(cabinet):
    """Кнопка отбора трогает много пар сразу — связывают их по ВРЕМЕНИ."""
    out = _run(cabinet, "КИТ", "48")
    assert "МАССОВЫЕ ДЕЙСТВИЯ" in out
    assert "broadcast_off по отбору 48 шт" in out, out


def test_an_unknown_cabinet_is_refused_with_the_list(cabinet):
    """Отказ со списком, а не пустой ответ: пустой читается как «нулей нет»."""
    out = _run(cabinet, "ВБ", "48")
    assert "не найден" in out, out
    assert "1:КИТ" in out and "2:ОЗОН" in out

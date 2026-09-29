"""Сводка быстрых отмен обязана называть, КТО отменяет, а не только считать их.

Повод. 25.09 на бою заказ WB был отменён через двадцать секунд после приёма, и
`probe_order.py` честно ответил: отмену принёс опрос площадки, следа человека
нет. Но дальше вопрос раздваивается, и по одной строке он не решается никогда:

  * отменяет кто-то СНАРУЖИ — вторая система с ключами от кабинета или сам WB;
  * отменяем МЫ САМИ, не зная того: приём заказа списал последнюю единицу,
    рассылка увезла на тот же кабинет ноль, площадка увидела, что товара нет, и
    сняла ещё не собранное задание.

Следствия у них противоположные — «отключайте вторую систему» против «держите
запас по этим товарам», — а в списке отмен они выглядят ОДИНАКОВО. Различает их
ровно одно: ушёл ли наш ноль МЕЖДУ приёмом и отменой. Поэтому тесты ниже про
вывод, а не про подсчёт.
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

from app.models import (Base, DispatchQueueItem, DispatchStatus,
                        OrderProcessStatus, Platform, PlatformAccount,
                        ProcessedOrder, Product)
from app.timeutils import now_utc

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_cancels.py"


@pytest.fixture()
def db_url(tmp_path):
    path = tmp_path / "probe_cancels.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    session.add(Product(uid_1c="uid-1", article="57020 5299", name="Рубашка",
                        size="L", color="Тёмно-коричневый", stock_on_hand=0))
    session.add(Product(uid_1c="uid-2", article="K.836 BSS LINEN", name="Рубашка",
                        size="34", color="WHITE", stock_on_hand=2))
    session.add(PlatformAccount(id=3, platform=Platform.wb, name="ИП КАРАМАН",
                                warehouse_id="wh-1"))
    session.commit()
    session.close()
    engine.dispose()
    return url


def _session(url):
    engine = create_engine(url, connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, autoflush=False)(), engine


def _order(url, order_id, gap_seconds, zero_after=None, days_ago=1, uid="uid-1"):
    """Заказ, отменённый через `gap_seconds`; `zero_after` — наш ноль в окне."""
    session, engine = _session(url)
    taken = now_utc() - timedelta(days=days_ago)
    session.add(ProcessedOrder(account_id=3, order_id=order_id, uid_1c=uid,
                               quantity=1, status=OrderProcessStatus.cancelled,
                               processed_at=taken,
                               cancelled_at=taken + timedelta(seconds=gap_seconds)))
    if zero_after is not None:
        session.add(DispatchQueueItem(
            uid_1c=uid, account_id=3, quantity=0, sent_quantity=0,
            reason="order", status=DispatchStatus.sent,
            sent_at=taken + timedelta(seconds=zero_after)))
    session.commit()
    session.close()
    engine.dispose()


def _run(url, *args):
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    done = subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, env=env, cwd=str(ROOT))
    return done.stdout + done.stderr


def test_our_own_zero_before_the_cancel_points_at_us(db_url):
    """Главный исход: повод для отмены дали мы сами.

    Не скажи скрипт этого, человек пошёл бы отключать вторую систему, которой
    там может уже и не быть, — а круг продолжал бы крутиться.
    """
    # Пять — это `MIN_FOR_VERDICT`: меньше, и скрипт отказывается судить о
    # причине вовсе, потому что большинство на двух строках ничего не значит.
    for i in range(6):
        _order(db_url, f"55830150{i}", gap_seconds=20, zero_after=8)

    out = _run(db_url, "КАРАМАН")

    assert "НАШ НОЛЬ ушёл" in out, out
    assert "отменяет площадка" in out
    assert "снаружи" not in out.lower()


def test_no_zero_in_the_window_points_outside(db_url):
    """Зеркальный исход, и вывод обязан отличаться.

    Нуля не было — значит повод не наш, и смотреть надо историю сборочного
    задания в кабинете.
    """
    for i in range(6):
        _order(db_url, f"55830150{i}", gap_seconds=20)

    out = _run(db_url, "КАРАМАН")

    assert "СНАРУЖИ" in out, out
    assert "отменяет площадка" not in out


def test_a_zero_after_the_cancel_is_not_a_cause(db_url):
    """Ноль ПОСЛЕ отмены — её следствие, а не причина.

    Считай мы любой ноль по паре, скрипт объявил бы виноватыми нас ровно там,
    где мы всего лишь отработали чужую отмену: она вернула единицу, и следующая
    рассылка увезла новое число.
    """
    for i in range(6):
        _order(db_url, f"55830150{i}", gap_seconds=20, zero_after=300)

    out = _run(db_url, "КАРАМАН")

    assert "НАШ НОЛЬ ушёл" not in out, out
    assert "СНАРУЖИ" in out


def test_a_slow_cancel_is_not_counted_as_fast(db_url):
    """Отмена через два часа — обычная работа человека в кабинете.

    Попади она в «быстрые», сводка объявила бы потоком автоматики то, что им не
    является, и разбор ушёл бы не туда.
    """
    _order(db_url, "5583015069", gap_seconds=7200)

    out = _run(db_url, "КАРАМАН")

    assert "быстрые отмены: 0 из 1" in out, out
    assert "Быстрых отмен нет" in out


def test_the_summary_says_whether_it_still_happens(db_url):
    """«Прекратилось такого-то» и «случилось сегодня» — два разных дела.

    В сводке по дням они выглядят одинаково, и без этой строки человек читал бы
    старую историю как текущую беду.
    """
    _order(db_url, "5583015069", gap_seconds=20, days_ago=5)

    out = _run(db_url, "КАРАМАН")

    assert "не повторялось" in out, out


def test_an_unknown_cabinet_lists_the_real_ones(db_url):
    """Отказ «не нашёлся» без списка заставляет угадывать имя с консоли."""
    out = _run(db_url, "ЯВОРСКАЯ")

    assert "не нашёлся" in out
    assert "ИП КАРАМАН" in out


def test_two_cancels_are_not_enough_to_blame_anyone(db_url):
    """Первый же боевой прогон: 1869 принятых заказов, быстрых отмен ДВЕ.

    У одной из двух наш ноль был — и разбор по большинству уверенно объявил
    причиной нас. Уверенность ложная: два случая на две тысячи заказов это не
    поток, а совпадение, и причина у каждого может быть своя. Хуже того, такой
    вердикт отправляет человека чинить запас по товарам, с которыми всё в
    порядке. Молчание тут честнее — и оно обязано называть число, иначе
    читается как «ничего не нашли».
    """
    # Товары РАЗНЫЕ, как и было на бою: ноль по одному не должен попадать в
    # окно другого — иначе тест сам себе подстроил бы «у обоих ноль был».
    _order(db_url, "5583015069", gap_seconds=20, zero_after=5)
    _order(db_url, "5590226186", gap_seconds=82, uid="uid-2")

    out = _run(db_url, "КАРАМАН")

    assert "МАЛО для вывода" in out, out
    assert "Наш ноль в окне был у 1 из 2" in out
    assert "probe_order.py" in out, "разбор по одной строке остаётся без адреса"
    assert "отменяет площадка" not in out
    assert "СНАРУЖИ" not in out


def test_the_scale_is_printed_next_to_the_verdict(db_url):
    """«Две отмены» и «две отмены на две тысячи заказов» читаются по-разному.

    По сводке масштаб надо складывать глазами по дням, и без него любой вывод
    ниже выглядит крупнее, чем он есть.
    """
    _order(db_url, "5583015069", gap_seconds=20)

    out = _run(db_url, "КАРАМАН")

    assert "при 1 принятых заказах" in out, out

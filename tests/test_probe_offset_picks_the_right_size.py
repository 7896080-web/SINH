"""Скрипт разбора порога обязан отвечать про ТУ строку, о которой спросили.

Артикул один на весь размерный ряд. Раньше по артикулу бралась `.first()` —
произвольный размер, — а размера в выводе не было вовсе: человек спрашивал про
5XL, получал числа по S и не мог этого заметить. Ответ выглядел ответом, и
проверить его было нечем. Цена не в путанице: по этим числам решают, правильный
ли порог уходит на площадки.

Прогоняем САМ СКРИПТ подпроцессом, а не его функции: предмет правки — то, что
увидит человек в консоли сервера.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Product, DiscrepancySource
from app.offset_base import set_discrepancy

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_offset.py"
SIZES = (("S", 0), ("M", 1), ("XL", 5), ("5XL", -2), ("3XL", 7))


@pytest.fixture()
def catalog(tmp_path):
    """Файловая база: скрипт запускается ОТДЕЛЬНЫМ процессом и in-memory не увидит."""
    path = tmp_path / "test_probe.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    for size, gap in SIZES:
        product = Product(uid_1c=f"u-{size}", article="36446 b", name="Свитшот",
                          size=size, color="LACIVERT MELANJ",
                          stock_on_hand=10, reserve=0)
        session.add(product)
        session.flush()
        set_discrepancy(session, product, gap, source=DiscrepancySource.manual)
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


def test_a_multi_size_article_is_refused_not_guessed(catalog):
    """Молча взять первую строку — значит ответить не про тот товар."""
    out = _run(catalog, "36446 b")
    assert "подходит 5 строк" in out, out
    assert "укажите размер" in out
    for size, _ in SIZES:
        assert f"размер {size}" in out, f"в списке нет размера {size}\n{out}"


def test_the_size_argument_finds_exactly_that_row(catalog):
    out = _run(catalog, "36446 b", "5XL")
    assert "Размер:           5XL" in out, out
    assert "Расхождение:      -2" in out
    assert "Расхождение:      7" not in out      # чужой размер в ответ не попал


def test_the_row_always_names_its_size(catalog):
    """Даже когда строка нашлась одна — иначе вывод нечем проверить."""
    out = _run(catalog, "u-XL")
    assert "Размер:           XL" in out, out


def test_a_size_that_does_not_exist_says_which_do(catalog):
    out = _run(catalog, "36446 b", "7XL")
    assert "размера «7XL» среди них НЕТ" in out, out
    assert "5XL" in out


def test_the_history_explains_where_the_number_came_from(catalog):
    """Ради этого история и заведена: по трём числам на экране происхождение
    расхождения не восстановить — надо видеть, ЧТО его поставило и когда."""
    out = _run(catalog, "36446 b", "5XL")
    assert "ИСТОРИЯ РАСХОЖДЕНИЯ" in out, out
    assert "manual" in out and "-> " in out

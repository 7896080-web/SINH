"""Скрипт независимой проверки инварианта `порог = расхождение + бронь`.

Он нужен ровно затем, что молчание находки отчёта значит «инвариант цел»
ТОЛЬКО если находке было что смотреть: проверка, которая ничего не видит,
молчит точно так же, как проверка, которой нечего сказать. Поэтому предмет
проверки здесь двойной — и то, что скрипт находит настоящую порчу, и то, что он
ГОВОРИТ ВСЛУХ, когда смотреть оказалось не на что.

И третье, ради чего он вообще отдельный от находки: скрипт считает формулу САМ.
Числа обязаны сойтись с находкой, и расхождение между ними — сигнал важнее любой
отдельной строки. Тест это тоже держит: подменяем формулу в скрипте и требуем,
чтобы сверка закричала, а не промолчала.

Прогоняем САМ СКРИПТ подпроцессом: предмет правки — то, что увидит человек в
консоли сервера, а не поведение функций.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Product

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_offset_invariant.py"


def _db(tmp_path, products):
    path = tmp_path / "test_invariant.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    for product in products:
        session.add(product)
    session.commit()
    session.close()
    engine.dispose()
    return url


def _product(uid, **kw):
    fields = dict(uid_1c=uid, article=uid.upper(), name="Свитшот", size="M",
                  stock_on_hand=50, reserve=2, stock_discrepancy=11,
                  broadcast_offset=13, broadcast_enabled=True)
    fields.update(kw)
    return Product(**fields)


def _run(url):
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    done = subprocess.run([sys.executable, str(SCRIPT)],
                          capture_output=True, text=True, env=env, cwd=str(ROOT))
    return done.returncode, done.stdout + done.stderr


def test_a_healthy_catalog_says_how_much_it_looked_at(tmp_path):
    """Молчание обязано быть ПОДКРЕПЛЁННЫМ: без числа кандидатов «расхождений
    нет» неотличимо от «смотреть было не на что»."""
    url = _db(tmp_path, [_product("u-1"), _product("u-2")])

    code, out = _run(url)

    assert code == 0, out
    assert "кандидатов на проверку ..... 2" in out, out
    assert "нет ни одного" in out


def test_an_empty_check_says_so_out_loud(tmp_path):
    """Ноль кандидатов — это НЕ «всё хорошо», и скрипт обязан сказать разницу.
    Ровно этого нельзя увидеть по зелёному отчёту."""
    url = _db(tmp_path, [_product("u-тихий", broadcast_enabled=False)])

    code, out = _run(url)

    assert code == 0, out
    assert "кандидатов на проверку ..... 0" in out, out
    assert "ВНИМАНИЕ" in out and "ничего не" in out


def test_a_broken_offset_is_found_with_its_numbers(tmp_path):
    """Случай 23.09: порог схлопнут в ноль при живом расхождении."""
    url = _db(tmp_path, [_product("u-порча", stock_discrepancy=11, reserve=2,
                                  broadcast_offset=0, stock_on_hand=22)])

    code, out = _run(url)

    assert code == 0, out
    assert "порог 0, а расхождение 11 + бронь 2 дают 13" in out, out
    assert "уходит 22, должно 9" in out
    assert "ЛИШНЕЕ В ПРОДАЖЕ" in out
    assert "лишних штук в продаже: 13" in out


def test_the_two_counts_are_printed_side_by_side(tmp_path):
    """Своё число и число находки — рядом. Ради этого скрипт и отдельный."""
    url = _db(tmp_path, [_product("u-порча", broadcast_offset=0)])

    code, out = _run(url)

    assert "=== СВЕРКА С НАХОДКОЙ ОТЧЁТА" in out, out
    assert "свой счёт ....... 1" in out
    assert "находка ......... 1" in out
    assert "сошлись" in out
    assert code == 0


def test_a_formula_that_drifted_apart_is_shouted_about(tmp_path):
    """Если счёт скрипта разойдётся со счётом находки, молчать нельзя: это
    значит, что отчёт считает не то, что здесь написано. Подменяем формулу В
    СКРИПТЕ и требуем крика и ненулевого кода возврата."""
    url = _db(tmp_path, [_product("u-1"), _product("u-2")])
    broken = ROOT / "scripts" / "_probe_invariant_broken.py"
    source = SCRIPT.read_text(encoding="utf-8")
    broken.write_text(source.replace("return disc + reserve",
                                     "return disc + reserve + 1"),
                      encoding="utf-8")
    try:
        env = dict(os.environ)
        env["DATABASE_URL"] = url
        env["SESSION_SECRET"] = "x" * 32
        env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        done = subprocess.run([sys.executable, str(broken)], capture_output=True,
                              text=True, env=env, cwd=str(ROOT))
    finally:
        broken.unlink(missing_ok=True)

    out = done.stdout + done.stderr
    assert "ЧИСЛА РАЗОШЛИСЬ" in out, out
    assert done.returncode == 1

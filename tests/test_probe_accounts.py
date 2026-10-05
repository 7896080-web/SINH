"""Погас кабинет — причина обязана быть ЧИТАЕМОЙ, а не висеть в подсказке.

Предохранитель пишет текст последней ошибки в `PlatformAccount.last_error` и
запись `account_auto_disabled` в журнал, то есть ответ в базе есть всегда. А
читателя у него почти не было: на «Диагностике» текст стоит в `title` жёлтого
бейджа — глазами не видно, по RDP наводить мышкой мучительно, скопировать в
переписку нельзя, длинный ответ площадки подсказка обрежет. Человек видел
«отключён» и число сбоёв, а чем ответила площадка — нет.

Главное, что проверяется ниже, — СВОДКА ПРО ОБЩУЮ ПРИЧИНУ. 05.10 на бою погасли
ТРИ кабинета WB сразу, а токен у каждого ИП свой: пять отказов подряд по всем
трём одновременно почти наверняка означают сторону площадки или сеть, и сказать
это надо вслух. Промолчи скрипт — человек пойдёт менять ключи по одному там,
где ключи ни при чём.

Прогоняем САМ СКРИПТ подпроцессом: предмет правки — то, что увидит человек в
консоли боевого сервера.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (AuditLog, Base, Platform, PlatformAccount,
                        WorkerHeartbeat)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_accounts.py"

# Один и тот же отказ площадки у трёх кабинетов — ровно боевой случай 05.10.
SHARED = ('401 Client Error: Unauthorized for url: '
          'https://marketplace-api.wildberries.ru/api/v3/orders/new')
OWN = "409 Conflict: warehouse is not available for this supplier"


@pytest.fixture()
def accounts(tmp_path):
    """Файловая база: скрипт идёт ОТДЕЛЬНЫМ процессом и in-memory не увидит."""
    path = tmp_path / "test_accounts.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()

    for i, name in enumerate(("ИП ПЕРВЫЙ", "ИП ВТОРОЙ", "ИП ТРЕТИЙ"), start=1):
        session.add(PlatformAccount(
            id=i, platform=Platform.wb, name=name, warehouse_id=f"wh-{i}",
            is_active=False, consecutive_failures=5, last_error=SHARED))
        session.add(AuditLog(
            actor="system", action="account_auto_disabled",
            details=f"{name}: 5 сбоев подряд. Последняя ошибка: {SHARED}",
            created_at=datetime(2026, 10, 5, 6, 12, 0)))
        session.add(WorkerHeartbeat(
            worker_name=f"poll_orders_account_{i}",
            last_run_at=datetime(2026, 10, 5, 6, 11, 30),
            last_success=False, last_error=SHARED))
    # Живой кабинет другой площадки — чтобы сводка не считала «все погасли».
    session.add(PlatformAccount(id=4, platform=Platform.kit, name="КИТ",
                                is_active=True, consecutive_failures=0))
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


def test_the_reason_is_printed_in_full(accounts):
    """Текст площадки печатается ЦЕЛИКОМ — он и есть ответ на вопрос.

    В подсказке `title` на странице длинный ответ обрезается и не копируется,
    а по нему решают, что чинить: ключи, склад или сторону площадки.
    """
    out = _run(accounts)

    assert "ПРИЧИНА" in out, out
    assert SHARED in out, "ответ площадки обязан быть в выводе целиком\n" + out


def test_one_shared_cause_is_said_out_loud(accounts):
    """Три кабинета с одной причиной — это НЕ три поломки, а одна.

    У каждого ИП свой токен. Промолчи скрипт, человек пойдёт менять ключи по
    одному там, где лёг сам API или кончилась сеть.
    """
    out = _run(accounts)

    assert "ОДНА И ТА ЖЕ ПРИЧИНА у 3 кабинетов" in out, out
    assert "ИП ПЕРВЫЙ" in out and "ИП ТРЕТИЙ" in out
    assert "НЕ в ключах" in out, "вывод обязан назвать следствие\n" + out


def test_different_causes_are_not_lumped_together(accounts):
    """А вот разные причины сводить в одну нельзя: это разные разборы."""
    engine = create_engine(accounts, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine, autoflush=False)()
    third = session.get(PlatformAccount, 3)
    third.last_error = OWN
    session.commit()
    session.close()
    engine.dispose()

    out = _run(accounts)

    assert "ОДНА И ТА ЖЕ ПРИЧИНА у 2 кабинетов" in out, out
    assert OWN in out, "своя причина третьего тоже обязана быть видна\n" + out


def test_when_the_breaker_fired_comes_from_the_audit_log(accounts):
    """Время отключения на странице не показано вовсе, а по нему и сходятся
    три кабинета между собой и с логом воркера."""
    out = _run(accounts)

    assert "КОГДА ГАСИЛ ПРЕДОХРАНИТЕЛЬ" in out, out
    assert "2026-10-05 06:12:00" in out


def test_the_account_workers_last_word_is_shown(accounts):
    """У погашенного кабинета задания сняты, значит отметка застыла на
    моменте поломки — именно она и нужна."""
    out = _run(accounts)

    assert "poll_orders_account_1" in out, out
    assert "ОШИБКА" in out


def test_only_off_narrows_to_the_disabled(accounts):
    out = _run(accounts, "--off")

    assert "КАБИНЕТЫ: 3" in out, out
    assert "КИТ" not in out, "живой кабинет в этот отбор не входит\n" + out


def test_one_account_by_number_or_name(accounts):
    by_number = _run(accounts, "3")
    by_name = _run(accounts, "третий")

    assert "КАБИНЕТЫ: 1" in by_number, by_number
    assert "ИП ТРЕТИЙ" in by_number
    assert "ИП ТРЕТИЙ" in by_name, by_name


def test_an_unknown_account_is_refused_with_the_list(accounts):
    """Отказ называет, что есть: иначе человек решит, что кабинета нет вовсе."""
    out = _run(accounts, "ИП ЧЕТВЁРТЫЙ")

    assert "ни номер, ни имя кабинета" in out, out
    assert "ИП ПЕРВЫЙ" in out and "КИТ" in out


def test_an_empty_reason_says_it_was_not_the_breaker(accounts):
    """Пустой `last_error` у выключенного кабинета значит «гасили руками».

    Успешный опрос поле ОЧИЩАЕТ, поэтому пустота тут — не «причины нет», а
    другое утверждение, и спутать их значит искать поломку, которой не было.
    """
    engine = create_engine(accounts, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine, autoflush=False)()
    account = session.get(PlatformAccount, 1)
    account.last_error = None
    account.consecutive_failures = 0
    session.commit()
    session.close()
    engine.dispose()

    out = _run(accounts, "1")

    assert "гасили не сбоями, а руками" in out, out


def test_it_says_where_to_turn_the_account_back_on(accounts):
    """Скрипт только читает: включение — действие наружу.

    Включённый кабинет сразу опрашивает площадку и рассылает остатки, поэтому
    решение принимает человек на «API-ключах», где заодно сбрасывается счётчик.
    """
    out = _run(accounts)

    assert "API-ключах" in out, out
    assert "только читает" in out

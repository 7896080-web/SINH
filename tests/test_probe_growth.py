"""`scripts/probe_growth.py` — прогоняется подпроцессом, как на сервере.

Предмет проверки — то, что увидит человек в консоли. Скрипт отвечает на один
вопрос: хватает ли чистке её потолка, — и отвечает ЧИСЛАМИ, потому что запас
живёт не в коде, а в данных: журнал сверки пишет строку на каждый товар в
снимке, то есть зависит от числа позиций с ненулевым остатком.

Переменные окружения гасятся пустой строкой, а не удаляются: на боевом сервере
есть `.env`, и `load_dotenv(override=False)` подставил бы оттуда настоящие
пути — тест стал бы зелёным здесь и красным там, то есть остановил бы накат.
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

from app.models import (AnomalyReason, AnomalyStatus, Base, Platform,
                        PlatformAccount, Product, ReconciliationClassification,
                        ReconciliationLog, SyncAnomaly, WorkerHeartbeat)
from app.timeutils import now_utc

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "probe_growth.py"


@pytest.fixture()
def base(tmp_path):
    """Своя база: свежие и просроченные строки сверки, открытая аномалия,
    отметка чистки с оговоркой про отставание."""
    url = f"sqlite:///{tmp_path / 'probe.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    for i in range(7):
        session.add(ReconciliationLog(
            uid_1c="u1", checked_at=now_utc() - timedelta(hours=i),
            python_stock=1, in_flight=0, expected_1c=1, actual_1c=1, delta=0,
            classification=ReconciliationClassification.normal))
    for i in range(3):
        session.add(ReconciliationLog(
            uid_1c="u1", checked_at=now_utc() - timedelta(days=200 + i),
            python_stock=1, in_flight=0, expected_1c=1, actual_1c=1, delta=0,
            classification=ReconciliationClassification.normal))

    account = PlatformAccount(platform=Platform.wb, name="ИП ЯВОРСКАЯ",
                              warehouse_id="wh-1")
    session.add(account)
    session.flush()
    session.add(Product(uid_1c="u1", article="А1", name="Товар", stock_on_hand=1))
    session.add(SyncAnomaly(uid_1c="u1", account_id=account.id,
                            reason=AnomalyReason.order_on_disabled,
                            status=AnomalyStatus.new, detected_at=now_utc()))
    session.add(WorkerHeartbeat(
        worker_name="retention", last_run_at=now_utc(), last_success=True,
        last_error="чистка не успевает: просроченных строк осталось 240000"))
    session.commit()
    session.close()
    engine.dispose()
    return url


def _run(url, tmp_path):
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["SESSION_SECRET"] = "x" * 32
    env["SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    env["BACKUP_DIR"] = str(tmp_path / "backups")
    env["BACKUP_MIRROR_DIR"] = ""
    env["BACKUP_RCLONE_REMOTE"] = ""
    done = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True,
                          encoding="utf-8", errors="replace", env=env, cwd=ROOT)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def test_it_names_the_cap_and_the_daily_arrival(base, tmp_path):
    """Главное число скрипта — приход за сутки рядом с потолком."""
    out = _run(base, tmp_path)

    assert "потолок чистки: 100000" in out
    assert "самый быстрый приход: 7 строк в сутки" in out, (
        "приход за сутки — то единственное, по чему видно, хватит ли потолка")
    assert "запас до потолка" in out


def test_every_table_with_a_horizon_is_listed(base, tmp_path):
    """Таблица, выпавшая из списка, — это таблица, про которую не спросят.

    Скрипт берёт сроки из `retention`, а имена таблиц — свои; разойдись они со
    схемой, вместо числа будет видно «ТАБЛИЦЫ НЕТ»."""
    out = _run(base, tmp_path)

    assert "ТАБЛИЦЫ НЕТ" not in out, "скрипт отстал от схемы"
    for human in ("журнал сверки", "очередь рассылки", "журнал действий",
                  "задания 1С", "аномалии", "журнал тестирования",
                  "история расхождений", "применённые дельты 1С", "возвраты"):
        assert human in out, f"в списке нет таблицы «{human}»"


def test_it_counts_the_overdue_rows(base, tmp_path):
    """Всего, за сутки, старше срока — три разных числа в одной строке.

    Семь свежих и три старше девяноста суток: спутай скрипт колонки, и человек
    читал бы приход как отставание, а это решения в противоположные стороны."""
    out = _run(base, tmp_path)

    line = next(l for l in out.splitlines()
                if l.strip().startswith("журнал сверки"))
    numbers = [int(x) for x in line.split() if x.isdigit()]
    assert numbers[:3] == [10, 7, 3], line


def test_tables_cleaned_by_another_rule_are_not_called_unlimited(base, tmp_path):
    """«Без срока хранения» про таблицу-ребёнка было бы неправдой.

    Прочитав такой список буквально, человек пошёл бы заводить срок там, где
    чистка уже работает, — и завёл бы его ПЕРЕД родителем, то есть осиротил бы
    историю."""
    out = _run(base, tmp_path)

    assert "вместе с самим возвратом" in out
    assert "по числу заявок, а не по сроку" in out


def test_it_echoes_what_the_job_itself_said(base, tmp_path):
    """Своё число и число задания печатаются рядом: расхождение между ними
    важнее любой отдельной строки — значит отчёт считает не то, что скрипт."""
    out = _run(base, tmp_path)

    assert "чистка не успевает" in out
    assert "240000" in out


def test_silence_is_explained_out_loud(base, tmp_path):
    """Проверка, которой нечего сказать, и проверка, которая ничего не видит,
    выглядят одинаково. Скрипт обязан говорить, что значит ноль."""
    out = _run(base, tmp_path)

    assert "НЕ значат" in out

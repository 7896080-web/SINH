"""Разовый бэкфилл `sync_settings.last_nonzero_sent_at` (миграция 96775c61f31f).

Он выполняется на боевой базе ОДИН раз и после этого не повторится никогда, а
ошибиться может в обе стороны, и обе дорогие. Насчитает лишнее — `ever_transmitted`
ответит «туда отправляли», и снятие галочки пошлёт ноль на карточку, которой мы
не управляли: обнуление чужих живых продаж. Насчитает меньше — отзыв не уйдёт, на
площадке останется наше число, а заказы по снятой паре живой опрос уже пропускает:
оверселл. Поэтому условие бэкфилла сверяется здесь со всеми видами записей очереди,
а не только со счастливым случаем.

Заодно тест меряет то, ради чего бэкфилл переписан на агрегат: коррелированный
подзапрос на каждую настройку SQLite выполнял через индекс по статусу, то есть
читал всю очередь на каждую строку — 134,9 с эксклюзивной блокировки записи при
`busy_timeout` в 30 с у живых служб.
"""
import os
import subprocess
import sqlite3
import sys

import pytest

from app.transmit import WITHDRAWAL_REASONS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION = os.path.join(ROOT, "alembic", "versions",
                         "96775c61f31f_last_nonzero_sent_at.py")
BEFORE = "5c7ec8206dc3"
AFTER = "96775c61f31f"


def _alembic(url, target):
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env.setdefault("SESSION_SECRET", "x" * 32)
    # Не `-m alembic`: у части установок пакет без `__main__`, и тест падал бы
    # не по делу. Точка входа консольной команды доступна всегда.
    r = subprocess.run([sys.executable, "-c",
                        "from alembic.config import main; main()",
                        "upgrade", target],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]


@pytest.fixture()
def migrated(tmp_path):
    """База на ревизии ДО правки, с набором записей очереди, потом миграция."""
    db = tmp_path / "alembic_backfill_test.db"
    url = "sqlite:///" + str(db)
    _alembic(url, BEFORE)

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("INSERT INTO platform_accounts (name, platform, is_active, "
                "dispatch_enabled, consecutive_failures) VALUES ('WB','wb',1,1,0)")
    aid = cur.lastrowid

    # (uid, [записи очереди]) — по одному виду записи на пару, чтобы видеть,
    # какой именно случай посчитан не так.
    cases = {
        # непустая отправка — отметка обязана появиться, и по САМОЙ ПОЗДНЕЙ
        "sent_nonzero":  [dict(q=5, sent_q=5, status="sent", sent_at="2026-09-01 10:00:00"),
                          dict(q=7, sent_q=7, status="sent", sent_at="2026-09-05 10:00:00")],
        # ноль — это не отправка остатка, а сообщение «не продавать»
        "sent_zero":     [dict(q=0, sent_q=0, status="sent", sent_at="2026-09-05 10:00:00")],
        # запись старше колонки `sent_quantity`: судим по причине
        "old_enable":    [dict(q=5, sent_q=None, status="sent", sent_at="2026-09-05 10:00:00",
                               reason="manual_enable")],
        "old_withdraw":  [dict(q=0, sent_q=None, status="sent", sent_at="2026-09-05 10:00:00",
                               reason="broadcast_off")],
        # на площадку ничего не ушло
        "error":         [dict(q=5, sent_q=None, status="error", sent_at=None)],
        "pending":       [dict(q=5, sent_q=None, status="pending", sent_at=None)],
        # симуляция со страницы «Тестирование» отправкой не считается никогда
        "test_only":     [dict(q=5, sent_q=5, status="sent", sent_at="2026-09-05 10:00:00",
                               is_test=1)],
        # у пары вообще нет записей очереди
        "no_rows":       [],
    }
    for uid, rows in cases.items():
        cur.execute("INSERT INTO products (uid_1c, article, name, stock_on_hand, reserve, "
                    "broadcast_enabled) VALUES (?,?,?,?,0,0)", (uid, "A", "Т", 5))
        cur.execute("INSERT INTO sync_settings (uid_1c, account_id, enabled, has_proposal, "
                    "min_threshold) VALUES (?,?,1,0,0)", (uid, aid))
        for r in rows:
            cur.execute(
                "INSERT INTO dispatch_queue (uid_1c, account_id, quantity, sent_quantity, "
                "reason, status, attempts, created_at, sent_at, is_test) "
                "VALUES (?,?,?,?,?,?,1,'2026-09-01 09:00:00',?,?)",
                (uid, aid, r["q"], r["sent_q"], r.get("reason", "manual_enable"),
                 r["status"], r["sent_at"], r.get("is_test", 0)))
    conn.commit()
    conn.close()

    _alembic(url, AFTER)
    conn = sqlite3.connect(str(db))
    marks = dict(conn.execute(
        "SELECT uid_1c, last_nonzero_sent_at FROM sync_settings").fetchall())
    conn.close()
    return marks


def test_a_nonzero_send_is_remembered_by_its_latest_time(migrated):
    assert migrated["sent_nonzero"] is not None
    assert migrated["sent_nonzero"].startswith("2026-09-05")


@pytest.mark.parametrize("uid", ["sent_zero", "old_withdraw", "error", "pending",
                                 "test_only", "no_rows"])
def test_what_never_carried_a_stock_leaves_no_mark(migrated, uid):
    """Отметка означает «мы посылали ТУДА непустой остаток». Ноль, отзыв, отказ,
    неотправленная запись и симуляция этого не означают, а лишняя отметка
    разрешила бы отзыв — то есть ноль — по карточке, которой мы не управляли."""
    assert migrated[uid] is None


def test_an_old_row_without_a_quantity_is_judged_by_its_reason(migrated):
    """`sent_quantity` появилась позже самих записей. У старых единственный
    признак — причина: по ней видно, остаток уезжал или отзыв."""
    assert migrated["old_enable"] is not None


def test_the_backfill_and_the_code_agree_on_what_a_withdrawal_is():
    """Разойдись список причин в миграции и в `transmit`, бэкфилл обещал бы не
    то, что потом считает `ever_transmitted`."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("mig_96775c61f31f", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod._WITHDRAWAL_REASONS) == set(WITHDRAWAL_REASONS)


def test_the_backfill_makes_one_pass_over_the_queue_not_one_per_setting():
    """Коррелированный подзапрос на каждую из 16 132 настроек SQLite выполнял
    через индекс по СТАТУСУ — то есть читал всю очередь на каждую строку: замер
    на боевом масштабе дал 134,9 с монопольной записи при `busy_timeout` 30 с у
    служб. Агрегат с `GROUP BY` даёт то же за 0,9 с. Форма запроса тут и есть
    предмет правки, поэтому она закреплена, а не оставлена на совесть."""
    src = open(MIGRATION, encoding="utf-8").read()
    body = src[src.index("def upgrade"):src.index("def downgrade")]
    assert "GROUP BY d.uid_1c, d.account_id" in body
    assert "SELECT MAX(d.sent_at) FROM dispatch_queue d" not in body


def test_an_interrupted_migration_can_be_repeated(tmp_path):
    """Alembic на SQLite оборванную миграцию НЕ откатывает: проверено 21.09 —
    после падения посередине новая колонка и временная таблица остались в базе,
    а версия не сдвинулась. Повторный `alembic upgrade head` падал на «duplicate
    column name», и накат вставал намертво: продолжить нечем, повторить нечем,
    службы не перезапущены, бой на старом коде. Оборваться посередине можно от
    чего угодно — кончилось место, `database is locked` от живых служб, закрытая
    консоль, — поэтому оба шага переживают повтор.

    Тест имитирует остатки оборванного прогона (колонка, временная таблица
    бэкфилла, индекс и временная таблица batch-режима уже на месте) и требует,
    чтобы повтор дошёл до конца.

    Последняя из них добавлена аудитом 22.09: `batch_alter_table` на SQLite
    перестраивает таблицу через `_alembic_tmp_ftp_tasks`, это самый долгий шаг
    миграции, и единственный, что оставался без защиты. Повтор падал на «table
    _alembic_tmp_ftp_tasks already exists» — навсегда.
    """
    db = tmp_path / "alembic_repeat_test.db"
    url = "sqlite:///" + str(db)
    _alembic(url, BEFORE)

    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE sync_settings ADD COLUMN last_nonzero_sent_at DATETIME")
    conn.execute("CREATE TABLE _backfill_nonzero_sent (uid_1c VARCHAR(100) NOT NULL, "
                 "account_id INTEGER NOT NULL, sent_at DATETIME NOT NULL, "
                 "PRIMARY KEY (uid_1c, account_id))")
    conn.execute("CREATE INDEX ix_ftp_tasks_barcode ON ftp_tasks (barcode)")
    conn.execute("CREATE TABLE _alembic_tmp_ftp_tasks (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    _alembic(url, "head")

    conn = sqlite3.connect(str(db))
    leftovers = [t[0] for t in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE '%backfill%'")]
    indexes = [i[1] for i in conn.execute("PRAGMA index_list(ftp_tasks)")]
    conn.close()
    assert leftovers == [], "временная таблица бэкфилла осталась в базе"
    assert "ix_ftp_tasks_barcode" in indexes
    conn = sqlite3.connect(str(db))
    tmp = [t[0] for t in conn.execute(
        "SELECT name FROM sqlite_master WHERE name = '_alembic_tmp_ftp_tasks'")]
    conn.close()
    assert tmp == [], "временная таблица batch-режима осталась в базе"

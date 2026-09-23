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


# ---------------------------------------------------------------------------
# Миграция 3af1c62b7d05 — `dispatch_queue.card_missing`
# ---------------------------------------------------------------------------

CARD_BEFORE = "cfa36a1f1f2f"
CARD_AFTER = "3af1c62b7d05"


def _queue_row(cur, aid, uid, error, card_missing=0):
    cur.execute("INSERT INTO products (uid_1c, stock_on_hand, reserve, "
                "broadcast_enabled) VALUES (?,0,0,0)", (uid,))
    cur.execute(
        "INSERT INTO dispatch_queue (uid_1c, account_id, quantity, reason, "
        "status, attempts, last_error, is_test) VALUES (?,?,?,?,?,?,?,0)",
        (uid, aid, 1, "order", "error", 5, error))


def test_the_card_missing_backfill_marks_old_rows(tmp_path):
    """Записи, лежащие в базе с прежних времён, флага не имеют.

    Без бэкфилла они вернулись бы в «рассылка не доехала» — критичную находку
    про оверселл, которого по отсутствующей карточке быть не может.
    """
    db = tmp_path / "card_missing_test.db"
    url = "sqlite:///" + str(db)
    _alembic(url, CARD_BEFORE)

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("INSERT INTO platform_accounts (name, platform, is_active, "
                "dispatch_enabled, consecutive_failures) VALUES ('WB','wb',1,1,0)")
    aid = cur.lastrowid
    _queue_row(cur, aid, "u-wb",
               "не отправлено за 5 попыток: площадка не знает этот sku на складе 1")
    _queue_row(cur, aid, "u-nocard",
               "нет карточки в каталоге кабинета — остаток отправить не по чему")
    _queue_row(cur, aid, "u-net", "HTTPSConnectionPool: read timed out")
    conn.commit()
    conn.close()

    _alembic(url, CARD_AFTER)

    conn = sqlite3.connect(str(db))
    marked = dict(conn.execute(
        "SELECT uid_1c, card_missing FROM dispatch_queue").fetchall())
    conn.close()
    assert marked == {"u-wb": 1, "u-nocard": 1, "u-net": 0}


def test_the_card_missing_migration_survives_its_own_interruption(tmp_path):
    """Alembic на SQLite оборванную миграцию НЕ откатывает.

    Колонка осталась, версия не сдвинулась — повторный `upgrade head` без
    проверки падал бы на «duplicate column name» НАВСЕГДА: продолжить нечем,
    повторить нечем, службы не перезапущены.
    """
    db = tmp_path / "card_missing_broken.db"
    url = "sqlite:///" + str(db)
    _alembic(url, CARD_BEFORE)

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("INSERT INTO platform_accounts (name, platform, is_active, "
                "dispatch_enabled, consecutive_failures) VALUES ('WB','wb',1,1,0)")
    aid = cur.lastrowid
    _queue_row(cur, aid, "u-wb", "площадка не знает этот sku на складе 1")
    # Остаток оборванного прогона: колонка добавлена, бэкфилл не дошёл.
    cur.execute("ALTER TABLE dispatch_queue ADD COLUMN card_missing "
                "BOOLEAN DEFAULT '0' NOT NULL")
    conn.commit()
    conn.close()

    _alembic(url, CARD_AFTER)

    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT card_missing FROM dispatch_queue").fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------
# Миграция 930c4c4db5e9 — `products.stock_discrepancy`
# ---------------------------------------------------------------------------

GAP_BEFORE = "b4e9d1c07a52"
GAP_AFTER = "930c4c4db5e9"


def _seed_products(db):
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO products (uid_1c, article, name, stock_on_hand, reserve, "
                 "broadcast_offset, offset_pinned, broadcast_enabled) "
                 "VALUES ('u1','A','Товар',22,2,13,7,0)")
    conn.execute("INSERT INTO products (uid_1c, article, name, stock_on_hand, reserve, "
                 "broadcast_offset, broadcast_enabled) VALUES ('u2','B','Товар',5,0,-7,0)")
    conn.execute("INSERT INTO products (uid_1c, article, name, stock_on_hand, reserve, "
                 "broadcast_enabled) VALUES ('u3','C','Товар',5,3,0)")
    conn.commit()
    conn.close()


def test_the_backfill_keeps_every_number_that_goes_out_today(tmp_path):
    """Бэкфилл берёт ПОРОГ, а не «учёт минус факт», и это главное про него.

    У части строк факт ПОДОБРАН прежним механизмом удержания (`pin_offset`), и
    расхождение, посчитанное из учёта и факта, разошлось бы с текущим порогом.
    Разойдись оно хоть на единицу — на площадки уехало бы другое число, причём
    сразу по всему каталогу и в тот же час, когда накатили миграцию.

    Знак сохраняется тоже: минус значит «на складе больше, чем знает 1С», и на
    бою таких товаров 53, до −213.
    """
    db = tmp_path / "gap_backfill.db"
    url = "sqlite:///" + str(db)
    _alembic(url, GAP_BEFORE)
    _seed_products(db)

    _alembic(url, GAP_AFTER)

    conn = sqlite3.connect(str(db))
    rows = dict(conn.execute(
        "SELECT uid_1c, stock_discrepancy FROM products").fetchall())
    offsets = dict(conn.execute(
        "SELECT uid_1c, broadcast_offset FROM products").fetchall())
    conn.close()

    assert rows["u1"] == 11, "13 − бронь 2"
    assert rows["u2"] == -7, "знак не потерян"
    assert rows["u3"] is None, "порога не было — измерять нечего, и ноль тут неправда"
    assert offsets == {"u1": 13, "u2": -7, "u3": None}, "ни одно число не дрогнуло"


def test_the_backfill_writes_the_history_once(tmp_path):
    """Число появилось не из воздуха — через месяц надо понимать, откуда.

    И повтор после обрыва не должен его удваивать: `INSERT ... SELECT` с
    проверкой «такой строки ещё нет» идемпотентен по построению.
    """
    db = tmp_path / "gap_history.db"
    url = "sqlite:///" + str(db)
    _alembic(url, GAP_BEFORE)
    _seed_products(db)
    _alembic(url, GAP_AFTER)

    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE alembic_version SET version_num = ?", (GAP_BEFORE,))
    conn.commit()
    conn.close()
    _alembic(url, GAP_AFTER)         # повтор, как после обрыва

    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT uid_1c, old_value, new_value, source "
                        "FROM stock_discrepancy_log ORDER BY uid_1c").fetchall()
    conn.close()
    assert rows == [("u1", None, 11, "migration"), ("u2", None, -7, "migration")]


def test_the_dead_column_is_put_out(tmp_path):
    """`offset_pinned` осиротел этой правкой и обязан быть погашен.

    Колонку не роняем — `DROP COLUMN` на SQLite перестраивает таблицу в 152
    тысячи строк при живых службах, — но непустые значения в ней стали бы
    ловушкой: видно в базе и в `probe_offset`, а не действует ничего.
    """
    db = tmp_path / "gap_pinned.db"
    url = "sqlite:///" + str(db)
    _alembic(url, GAP_BEFORE)
    _seed_products(db)

    _alembic(url, GAP_AFTER)

    conn = sqlite3.connect(str(db))
    left = conn.execute("SELECT count(*) FROM products "
                        "WHERE offset_pinned IS NOT NULL").fetchone()[0]
    conn.close()
    assert left == 0


def test_the_discrepancy_migration_survives_its_own_interruption(tmp_path):
    """Alembic на SQLite оборванную миграцию не откатывает — повтор обязан дойти.

    Имитируем остатки оборванного прогона: колонка уже добавлена, таблица
    истории и один из её индексов уже созданы, часть бэкфилла применена. Повтор
    не должен ни упасть на «duplicate column name», ни переписать уже
    перенесённое расхождение — у него мог быть свой смысл, если после обрыва
    строку успели поправить руками.
    """
    db = tmp_path / "gap_interrupted.db"
    url = "sqlite:///" + str(db)
    _alembic(url, GAP_BEFORE)
    _seed_products(db)

    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE products ADD COLUMN stock_discrepancy INTEGER")
    conn.execute("UPDATE products SET stock_discrepancy = 99 WHERE uid_1c = 'u1'")
    conn.execute("CREATE TABLE stock_discrepancy_log ("
                 "id INTEGER NOT NULL PRIMARY KEY, uid_1c VARCHAR(36) NOT NULL, "
                 "old_value INTEGER, new_value INTEGER, source VARCHAR(9) NOT NULL, "
                 "username VARCHAR(64), base_date DATE, base_stock INTEGER, "
                 "fact INTEGER, note VARCHAR(255), created_at DATETIME)")
    conn.execute("CREATE INDEX ix_stock_discrepancy_log_uid_1c "
                 "ON stock_discrepancy_log (uid_1c)")
    conn.commit()
    conn.close()

    _alembic(url, GAP_AFTER)

    conn = sqlite3.connect(str(db))
    rows = dict(conn.execute("SELECT uid_1c, stock_discrepancy FROM products").fetchall())
    indexes = [i[1] for i in conn.execute("PRAGMA index_list(stock_discrepancy_log)")]
    conn.close()
    assert rows["u1"] == 99, "уже перенесённое не переписано"
    assert rows["u2"] == -7, "остальное доделано"
    assert "ix_stock_discrepancy_log_created_at" in indexes

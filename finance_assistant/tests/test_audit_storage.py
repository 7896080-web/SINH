"""Регрессии по аудиту хранилища: номера не переиспользуются, миграция, правила."""
import sqlite3

from finance.storage import SCHEMA, Storage


def test_ids_not_reused_after_delete(tmp_path):
    db = Storage(str(tmp_path / "f.db"))
    card = db.add_card("Сбер", "1111", "Сбер")
    first = db.add_expense(op_date="2026-09-01", amount=100, card_id=card.id)
    db.delete_expense(first)
    assert db.add_expense(op_date="2026-09-02", amount=200, card_id=card.id) != first


def test_old_database_migrated_to_autoincrement(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.replace(" AUTOINCREMENT", ""))
    conn.executescript("""
        INSERT INTO cards (id, name, last4, bank) VALUES (1, 'Сбер', '1111', 'Сбер');
        INSERT INTO categories (id, name) VALUES (1, 'Прочее');
        INSERT INTO expenses (id, op_date, amount, kind, purpose, card_id, category_id)
            VALUES (7, '2026-09-01', 500, 'expense', 'business', 1, 1);
        INSERT INTO statements (id, card_id, month) VALUES (3, 1, '2026-09');
        INSERT INTO statement_lines (id, statement_id, op_date, amount, direction)
            VALUES (9, 3, '2026-09-01', 500, 'out');
    """)
    conn.commit()
    conn.close()
    db = Storage(path)
    sql = db.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'expenses'").fetchone()[0]
    assert "AUTOINCREMENT" in sql
    e = db.expense(7)
    assert (e.amount, e.card, e.category) == (500, "Сбер", "Прочее")
    assert [ln.id for ln in db.statement(1, "2026-09")[1]] == [9]
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.delete_expense(7)
    assert db.add_expense(op_date="2026-09-02", amount=1, card_id=1) == 8
    db.close()
    Storage(path).close()  # повторное открытие — миграция не нужна и не ломает


def test_generic_descriptions_never_become_rules(tmp_path):
    db = Storage(str(tmp_path / "f.db"))
    cat = db.category_id("Прочее")
    for name in ("Оплата по QR-коду через СБП", "Перевод по номеру телефона через СБП",
                 "Оплата товаров и услуг", "ИП", "по выписке"):
        assert not db.set_rule(name, cat), name
    assert db.set_rule("СКБ Контур", cat)

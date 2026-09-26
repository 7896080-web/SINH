"""SQLite-хранилище: карты, статьи, расходы, выписки, состояние диалога.

Суммы — положительные копейки (int). Направление задаёт kind:
  expense        — оплата с личной карты (purpose: business | personal);
  reimbursement  — бизнес вернул деньги на личную карту.
"""

import json
import sqlite3
from dataclasses import dataclass

BUSINESS, PERSONAL = "business", "personal"
EXPENSE, REIMBURSEMENT = "expense", "reimbursement"

DEFAULT_CATEGORIES = [
    "Закупка товара",
    "Логистика и доставка",
    "Маркетплейсы: комиссии и услуги",
    "Реклама и продвижение",
    "Упаковка и расходники",
    "Аренда",
    "Связь, сервисы, подписки",
    "Подрядчики и зарплата",
    "Налоги и взносы",
    "Банковские комиссии",
    "Транспорт и такси",
    "Прочее",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    last4 TEXT NOT NULL DEFAULT '',
    bank TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    op_date TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    kind TEXT NOT NULL CHECK (kind IN ('expense', 'reimbursement')),
    purpose TEXT NOT NULL CHECK (purpose IN ('business', 'personal')),
    card_id INTEGER NOT NULL REFERENCES cards(id),
    category_id INTEGER REFERENCES categories(id),
    merchant TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    receipt_path TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_expenses_date ON expenses(op_date);
CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL REFERENCES cards(id),
    month TEXT NOT NULL,
    total_in INTEGER,
    total_out INTEGER,
    UNIQUE (card_id, month)
);
CREATE TABLE IF NOT EXISTS statement_lines (
    id INTEGER PRIMARY KEY,
    statement_id INTEGER NOT NULL REFERENCES statements(id) ON DELETE CASCADE,
    op_date TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    description TEXT NOT NULL DEFAULT '',
    own_transfer INTEGER NOT NULL DEFAULT 0,
    UNIQUE (statement_id, op_date, amount, direction, description)
);
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL
);
"""


@dataclass
class Card:
    id: int
    name: str
    last4: str
    bank: str

    @property
    def label(self) -> str:
        return f"{self.name} ·{self.last4}" if self.last4 else self.name


@dataclass
class Expense:
    id: int
    op_date: str
    amount: int
    kind: str
    purpose: str
    card_id: int
    card: str
    category: str | None
    merchant: str
    description: str
    receipt_path: str


@dataclass
class StatementLine:
    id: int
    op_date: str
    amount: int
    direction: str
    description: str
    own_transfer: bool


class Storage:
    def __init__(self, path: str):
        # Бот обрабатывает апдейты по одному, но распознавание идёт в рабочем
        # потоке (asyncio.to_thread), поэтому соединение делим между потоками.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        if not self.conn.execute("SELECT 1 FROM categories").fetchone():
            with self.conn:
                self.conn.executemany(
                    "INSERT INTO categories (name) VALUES (?)", [(c,) for c in DEFAULT_CATEGORIES]
                )

    def close(self):
        self.conn.close()

    # --- карты ---------------------------------------------------------

    def add_card(self, name: str, last4: str = "", bank: str = "") -> Card:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO cards (name, last4, bank) VALUES (?, ?, ?)", (name, last4, bank)
            )
        return self.card(cur.lastrowid)

    def cards(self) -> list[Card]:
        return [Card(**dict(r)) for r in self.conn.execute("SELECT * FROM cards ORDER BY id")]

    def card(self, card_id: int) -> Card | None:
        row = self.conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return Card(**dict(row)) if row else None

    def delete_card(self, card_id: int) -> bool:
        """Удаляет только карту без записей — иначе потеряется история."""
        used = self.conn.execute(
            "SELECT 1 FROM expenses WHERE card_id = ? UNION SELECT 1 FROM statements WHERE card_id = ?",
            (card_id, card_id),
        ).fetchone()
        if used:
            return False
        with self.conn:
            self.conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        return True

    # --- статьи --------------------------------------------------------

    def categories(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT id, name FROM categories ORDER BY id").fetchall()

    def category_id(self, name: str) -> int | None:
        row = self.conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
        return row["id"] if row else None

    def add_category(self, name: str) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
        return cur.lastrowid or self.category_id(name)

    # --- операции ------------------------------------------------------

    def add_expense(self, *, op_date, amount, card_id, kind=EXPENSE, purpose=BUSINESS,
                    category_id=None, merchant="", description="", receipt_path="") -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO expenses (op_date, amount, kind, purpose, card_id, category_id,"
                " merchant, description, receipt_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (op_date, amount, kind, purpose, card_id, category_id, merchant, description,
                 receipt_path),
            )
        return cur.lastrowid

    def update_expense(self, expense_id: int, **fields):
        allowed = {"purpose", "category_id", "card_id", "op_date", "amount", "kind"}
        assert set(fields) <= allowed, fields
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.conn:
            self.conn.execute(
                f"UPDATE expenses SET {sets} WHERE id = ?", (*fields.values(), expense_id)
            )

    def delete_expense(self, expense_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,)).rowcount > 0

    _EXPENSE_SELECT = (
        "SELECT e.id, e.op_date, e.amount, e.kind, e.purpose, e.card_id, c.name AS card,"
        " g.name AS category, e.merchant, e.description, e.receipt_path"
        " FROM expenses e JOIN cards c ON c.id = e.card_id"
        " LEFT JOIN categories g ON g.id = e.category_id"
    )

    def expense(self, expense_id: int) -> Expense | None:
        row = self.conn.execute(self._EXPENSE_SELECT + " WHERE e.id = ?", (expense_id,)).fetchone()
        return Expense(**dict(row)) if row else None

    def expenses(self, month: str, card_id: int | None = None) -> list[Expense]:
        sql = self._EXPENSE_SELECT + " WHERE substr(e.op_date, 1, 7) = ?"
        params: list = [month]
        if card_id is not None:
            sql += " AND e.card_id = ?"
            params.append(card_id)
        sql += " ORDER BY e.op_date, e.id"
        return [Expense(**dict(r)) for r in self.conn.execute(sql, params)]

    def find_duplicate(self, *, op_date, amount, card_id) -> Expense | None:
        row = self.conn.execute(
            self._EXPENSE_SELECT + " WHERE e.op_date = ? AND e.amount = ? AND e.card_id = ?",
            (op_date, amount, card_id),
        ).fetchone()
        return Expense(**dict(row)) if row else None

    # --- выписки -------------------------------------------------------

    def statement_id(self, card_id: int, month: str) -> int:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO statements (card_id, month) VALUES (?, ?)", (card_id, month)
            )
        return self.conn.execute(
            "SELECT id FROM statements WHERE card_id = ? AND month = ?", (card_id, month)
        ).fetchone()["id"]

    def reset_statement(self, card_id: int, month: str):
        with self.conn:
            self.conn.execute(
                "DELETE FROM statements WHERE card_id = ? AND month = ?", (card_id, month)
            )

    def add_statement_lines(self, statement_id: int, lines: list[dict]) -> int:
        """Повторно присланный тот же скриншот выписки не задвоит строки."""
        added = 0
        with self.conn:
            for ln in lines:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO statement_lines"
                    " (statement_id, op_date, amount, direction, description, own_transfer)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (statement_id, ln["op_date"], ln["amount"], ln["direction"],
                     ln.get("description", ""), int(ln.get("own_transfer", False))),
                )
                added += cur.rowcount
        return added

    def set_statement_totals(self, statement_id: int, total_in=None, total_out=None):
        with self.conn:
            if total_in is not None:
                self.conn.execute("UPDATE statements SET total_in = ? WHERE id = ?",
                                  (total_in, statement_id))
            if total_out is not None:
                self.conn.execute("UPDATE statements SET total_out = ? WHERE id = ?",
                                  (total_out, statement_id))

    def statement(self, card_id: int, month: str) -> tuple[sqlite3.Row, list[StatementLine]] | None:
        head = self.conn.execute(
            "SELECT * FROM statements WHERE card_id = ? AND month = ?", (card_id, month)
        ).fetchone()
        if not head:
            return None
        lines = [
            StatementLine(r["id"], r["op_date"], r["amount"], r["direction"], r["description"],
                          bool(r["own_transfer"]))
            for r in self.conn.execute(
                "SELECT * FROM statement_lines WHERE statement_id = ? ORDER BY op_date, id",
                (head["id"],),
            )
        ]
        return head, lines

    # --- состояние диалога ---------------------------------------------

    def get_state(self, chat_id: int) -> dict:
        row = self.conn.execute("SELECT state FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
        return json.loads(row["state"]) if row else {}

    def set_state(self, chat_id: int, state: dict):
        with self.conn:
            if state:
                self.conn.execute(
                    "INSERT INTO chat_state (chat_id, state) VALUES (?, ?)"
                    " ON CONFLICT (chat_id) DO UPDATE SET state = excluded.state",
                    (chat_id, json.dumps(state, ensure_ascii=False)),
                )
            else:
                self.conn.execute("DELETE FROM chat_state WHERE chat_id = ?", (chat_id,))

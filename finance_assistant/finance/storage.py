"""SQLite-хранилище: карты, статьи, расходы, выписки, состояние диалога.

Суммы — положительные копейки (int). Направление задаёт kind:
  expense        — оплата с личной карты (purpose: business | personal);
  reimbursement  — бизнес вернул деньги на личную карту.
"""

import json
import sqlite3
from dataclasses import dataclass

BUSINESS, PERSONAL = "business", "personal"
# Тип карты/счёта: личная карта владельца или расчётный счёт ИП (с бизнес-картой).
PERSONAL_CARD, BUSINESS_ACCOUNT = "personal", "business"
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
    last4 TEXT NOT NULL DEFAULT '',  -- последние 4 цифры карт и счетов через пробел
    bank TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'personal' CHECK (kind IN ('personal', 'business'))
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
    op_time TEXT NOT NULL DEFAULT '',
    amount INTEGER NOT NULL CHECK (amount > 0),
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    description TEXT NOT NULL DEFAULT '',
    own_transfer INTEGER NOT NULL DEFAULT 0,
    suggested_category TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_statement_lines_key
    ON statement_lines(statement_id, op_date, amount, direction);
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
    kind: str = PERSONAL_CARD

    @property
    def is_business(self) -> bool:
        return self.kind == BUSINESS_ACCOUNT

    @property
    def numbers(self) -> list[str]:
        """У одной «карты» бывает несколько номеров: сама карта и её счёт
        (ВТБ: «Карта для жизни •1234» и «Мастер-счет •5678» — одни деньги)."""
        return self.last4.split()

    @property
    def label(self) -> str:
        base = f"{self.name} ·{self.numbers[0]}" if self.numbers else self.name
        return base + (" (бизнес-счёт)" if self.is_business else "")


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
    card_kind: str = PERSONAL_CARD

    @property
    def owed_effect(self) -> int:
        """Насколько запись меняет долг бизнеса перед владельцем.

        Бизнес-расход с личной карты — бизнес должен вернуть; возмещение и
        личная трата с бизнес-счёта — владелец уже получил деньги бизнеса.
        Бизнес-расход с бизнес-счёта долга не создаёт.
        """
        if self.kind == REIMBURSEMENT:
            return -self.amount
        if self.card_kind == BUSINESS_ACCOUNT:
            return -self.amount if self.purpose == PERSONAL else 0
        return self.amount if self.purpose == BUSINESS else 0


@dataclass
class StatementLine:
    id: int
    op_date: str
    amount: int
    direction: str
    description: str
    own_transfer: bool
    suggested_category: str = ""
    op_time: str = ""


class Storage:
    def __init__(self, path: str):
        # Бот обрабатывает апдейты по одному, но распознавание идёт в рабочем
        # потоке (asyncio.to_thread), поэтому соединение делим между потоками.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        if not self.conn.execute("SELECT 1 FROM categories").fetchone():
            with self.conn:
                self.conn.executemany(
                    "INSERT INTO categories (name) VALUES (?)", [(c,) for c in DEFAULT_CATEGORIES]
                )

    def close(self):
        self.conn.close()

    def _add_missing_columns(self):
        """Базы, созданные прошлыми версиями, дополняем новыми колонками."""
        added = {("cards", "kind"): "TEXT NOT NULL DEFAULT 'personal'",
                 ("statement_lines", "suggested_category"): "TEXT NOT NULL DEFAULT ''",
                 ("statement_lines", "op_time"): "TEXT NOT NULL DEFAULT ''"}
        for (table, column), ddl in added.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self.conn.commit()

    # --- карты ---------------------------------------------------------

    def add_card(self, name: str, last4: str = "", bank: str = "",
                 kind: str = PERSONAL_CARD) -> Card:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO cards (name, last4, bank, kind) VALUES (?, ?, ?, ?)",
                (name, last4, bank, kind),
            )
        return self.card(cur.lastrowid)

    def add_card_number(self, card_id: int, number: str):
        card = self.card(card_id)
        if card and number not in card.numbers:
            with self.conn:
                self.conn.execute("UPDATE cards SET last4 = ? WHERE id = ?",
                                  (" ".join(card.numbers + [number]), card_id))

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
        " g.name AS category, e.merchant, e.description, e.receipt_path, c.kind AS card_kind"
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
        """Добавить строки выписки, не задваивая уже загруженные.

        Две одинаковые покупки в один день — это две строки, поэтому сравниваем
        количество: если в новом файле таких строк N, а в базе уже M, добавляем
        только N − M. Так повторно присланный скриншот ничего не задвоит, а
        настоящие одинаковые операции не потеряются.
        """
        groups: dict[tuple, list[dict]] = {}
        for ln in lines:
            key = (ln["op_date"], ln.get("op_time", ""), ln["amount"], ln["direction"],
                   ln.get("description", ""))
            groups.setdefault(key, []).append(ln)
        added = 0
        with self.conn:
            for (op_date, op_time, amount, direction, description), group in groups.items():
                have = self.conn.execute(
                    "SELECT COUNT(*) FROM statement_lines WHERE statement_id = ? AND op_date = ?"
                    " AND op_time = ? AND amount = ? AND direction = ? AND description = ?",
                    (statement_id, op_date, op_time, amount, direction, description),
                ).fetchone()[0]
                for ln in group[have:]:
                    self.conn.execute(
                        "INSERT INTO statement_lines (statement_id, op_date, op_time, amount,"
                        " direction, description, own_transfer, suggested_category)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (statement_id, op_date, op_time, amount, direction, description,
                         int(ln.get("own_transfer", False)), ln.get("suggested_category", "")),
                    )
                    added += 1
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
                          bool(r["own_transfer"]), r["suggested_category"], r["op_time"])
            for r in self.conn.execute(
                "SELECT * FROM statement_lines WHERE statement_id = ? ORDER BY op_date, op_time, id",
                (head["id"],),
            )
        ]
        return head, lines

    def statement_line(self, line_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT l.*, s.card_id, s.month FROM statement_lines l"
            " JOIN statements s ON s.id = l.statement_id WHERE l.id = ?", (line_id,)
        ).fetchone()

    def clear_suggestion(self, line_id: int):
        with self.conn:
            self.conn.execute(
                "UPDATE statement_lines SET suggested_category = '' WHERE id = ?", (line_id,))

    def months_with_data(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT substr(op_date, 1, 7) AS m FROM expenses"
            " UNION SELECT month FROM statements ORDER BY m"
        ).fetchall()
        return [r["m"] for r in rows]

    def owed_until(self, month: str) -> int:
        """Долг бизнеса перед владельцем нарастающим итогом по конец месяца."""
        rows = self.conn.execute(
            self._EXPENSE_SELECT + " WHERE substr(e.op_date, 1, 7) <= ?", (month,)
        ).fetchall()
        return sum(Expense(**dict(r)).owed_effect for r in rows)

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

"""Хранилище и вся бизнес-логика учёта. CLI только вызывает методы Ledger.

Суммы — всегда положительные копейки; направление задаёт kind операции:
income (приход на счёт), expense (расход со счёта), transfer (со счёта на счёт).
"""

import csv
import sqlite3
from calendar import monthrange
from dataclasses import dataclass
from datetime import date

from .money import format_amount, parse_amount

INCOME, EXPENSE, TRANSFER = "income", "expense", "transfer"
KIND_LABELS = {INCOME: "доход", EXPENSE: "расход", TRANSFER: "перевод"}

DEFAULT_CATEGORIES = [
    ("Продукты", EXPENSE), ("Кафе и рестораны", EXPENSE), ("Транспорт", EXPENSE),
    ("ЖКХ", EXPENSE), ("Связь и интернет", EXPENSE), ("Здоровье", EXPENSE),
    ("Одежда", EXPENSE), ("Развлечения", EXPENSE), ("Прочие расходы", EXPENSE),
    ("Зарплата", INCOME), ("Прочие доходы", INCOME),
]

CSV_FIELDS = ["date", "type", "amount", "account", "category", "to_account", "note"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    opening_balance INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('income', 'expense'))
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('income', 'expense', 'transfer')),
    amount INTEGER NOT NULL CHECK (amount > 0),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    to_account_id INTEGER REFERENCES accounts(id),
    category_id INTEGER REFERENCES categories(id),
    note TEXT NOT NULL DEFAULT '',
    recurring_id INTEGER REFERENCES recurring(id),
    recurring_month TEXT,
    UNIQUE (recurring_id, recurring_month)
);
CREATE INDEX IF NOT EXISTS ix_transactions_date ON transactions(date);
CREATE TABLE IF NOT EXISTS budgets (
    category_id INTEGER NOT NULL REFERENCES categories(id),
    month TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    PRIMARY KEY (category_id, month)
);
CREATE TABLE IF NOT EXISTS recurring (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
    amount INTEGER NOT NULL CHECK (amount > 0),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    day INTEGER NOT NULL CHECK (day BETWEEN 1 AND 31)
);
"""


class FinanceError(ValueError):
    """Ошибка ввода, понятная пользователю (показывается как есть)."""


@dataclass
class Transaction:
    id: int
    date: str
    kind: str
    amount: int
    account: str
    to_account: str | None
    category: str | None
    note: str


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def parse_month(text: str) -> str:
    try:
        year, month = (int(p) for p in text.split("-"))
        return date(year, month, 1).strftime("%Y-%m")
    except (ValueError, TypeError):
        raise FinanceError(f"месяц должен быть в формате ГГГГ-ММ: {text!r}") from None


def parse_date(text: str | None) -> str:
    if text is None or text == "":
        return date.today().isoformat()
    for candidate in (text, _ru_to_iso(text)):
        try:
            return date.fromisoformat(candidate).isoformat()
        except (ValueError, TypeError):
            continue
    raise FinanceError(f"дата должна быть ГГГГ-ММ-ДД или ДД.ММ.ГГГГ: {text!r}")


def _ru_to_iso(text: str) -> str | None:
    parts = text.split(".")
    if len(parts) != 3:
        return None
    d, m, y = parts
    return f"{y}-{m.zfill(2)}-{d.zfill(2)}"


def _month_bounds(month: str) -> tuple[str, str]:
    year, mon = (int(p) for p in month.split("-"))
    return date(year, mon, 1).isoformat(), date(year, mon, monthrange(year, mon)[1]).isoformat()


def _shift_month(month: str, delta: int) -> str:
    year, mon = (int(p) for p in month.split("-"))
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _amount(value) -> int:
    if isinstance(value, int):
        kopecks = value
    else:
        try:
            kopecks = parse_amount(value)
        except ValueError as exc:
            raise FinanceError(str(exc)) from None
    if kopecks <= 0:
        raise FinanceError("сумма должна быть больше нуля")
    return kopecks


class Ledger:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        fresh = not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'categories'"
        ).fetchone()
        self.conn.executescript(SCHEMA)
        if fresh:
            with self.conn:
                self.conn.executemany(
                    "INSERT INTO categories (name, kind) VALUES (?, ?)", DEFAULT_CATEGORIES
                )

    def close(self):
        self.conn.close()

    # --- справочники ---------------------------------------------------

    def add_account(self, name: str, opening_balance=0) -> int:
        name = name.strip()
        if not name:
            raise FinanceError("имя счёта не может быть пустым")
        balance = opening_balance if isinstance(opening_balance, int) else parse_amount(opening_balance)
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO accounts (name, opening_balance) VALUES (?, ?)", (name, balance)
                )
        except sqlite3.IntegrityError:
            raise FinanceError(f"счёт «{name}» уже есть") from None
        return cur.lastrowid

    def add_category(self, name: str, kind: str) -> int:
        name = name.strip()
        if kind not in (INCOME, EXPENSE):
            raise FinanceError("тип категории: income или expense")
        if not name:
            raise FinanceError("имя категории не может быть пустым")
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO categories (name, kind) VALUES (?, ?)", (name, kind)
                )
        except sqlite3.IntegrityError:
            raise FinanceError(f"категория «{name}» уже есть") from None
        return cur.lastrowid

    def categories(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM categories ORDER BY kind, name").fetchall()

    def _account_id(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
        if not row:
            raise FinanceError(f"нет счёта «{name}» (добавьте: account add)")
        return row["id"]

    def _category(self, name: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM categories WHERE name = ?", (name,)).fetchone()
        if not row:
            raise FinanceError(f"нет категории «{name}» (добавьте: category add)")
        return row

    def _category_for(self, name: str, kind: str) -> int:
        row = self._category(name)
        if row["kind"] != kind:
            raise FinanceError(
                f"категория «{name}» — для {KIND_LABELS[row['kind']]}ов, а операция — {KIND_LABELS[kind]}"
            )
        return row["id"]

    # --- операции ------------------------------------------------------

    def add_income(self, amount, account, category, on=None, note="") -> int:
        return self._insert(INCOME, amount, account, category, on, note)

    def add_expense(self, amount, account, category, on=None, note="") -> int:
        return self._insert(EXPENSE, amount, account, category, on, note)

    def add_transfer(self, amount, from_account, to_account, on=None, note="") -> int:
        if from_account == to_account:
            raise FinanceError("перевод на тот же самый счёт не имеет смысла")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO transactions (date, kind, amount, account_id, to_account_id, note)"
                " VALUES (?, 'transfer', ?, ?, ?, ?)",
                (parse_date(on), _amount(amount), self._account_id(from_account),
                 self._account_id(to_account), note),
            )
        return cur.lastrowid

    def _insert(self, kind, amount, account, category, on, note, recurring=None) -> int:
        recurring_id, recurring_month = recurring or (None, None)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO transactions (date, kind, amount, account_id, category_id, note,"
                " recurring_id, recurring_month) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (parse_date(on), kind, _amount(amount), self._account_id(account),
                 self._category_for(category, kind), note, recurring_id, recurring_month),
            )
        return cur.lastrowid

    def delete_transaction(self, tx_id: int):
        with self.conn:
            cur = self.conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        if cur.rowcount == 0:
            raise FinanceError(f"нет операции №{tx_id}")

    def transactions(self, month=None, account=None, category=None) -> list[Transaction]:
        sql = (
            "SELECT t.id, t.date, t.kind, t.amount, a.name AS account, b.name AS to_account,"
            " c.name AS category, t.note FROM transactions t"
            " JOIN accounts a ON a.id = t.account_id"
            " LEFT JOIN accounts b ON b.id = t.to_account_id"
            " LEFT JOIN categories c ON c.id = t.category_id WHERE 1=1"
        )
        params: list = []
        if month:
            sql += " AND t.date BETWEEN ? AND ?"
            params += _month_bounds(parse_month(month))
        if account:
            account_id = self._account_id(account)
            sql += " AND (t.account_id = ? OR t.to_account_id = ?)"
            params += [account_id, account_id]
        if category:
            sql += " AND t.category_id = ?"
            params.append(self._category(category)["id"])
        sql += " ORDER BY t.date, t.id"
        return [Transaction(**dict(r)) for r in self.conn.execute(sql, params)]

    # --- остатки и отчёты ----------------------------------------------

    def balances(self) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT a.name, a.opening_balance
                + COALESCE(SUM(CASE
                    WHEN t.kind = 'income' AND t.account_id = a.id THEN t.amount
                    WHEN t.kind = 'expense' AND t.account_id = a.id THEN -t.amount
                    WHEN t.kind = 'transfer' AND t.account_id = a.id THEN -t.amount
                    WHEN t.kind = 'transfer' AND t.to_account_id = a.id THEN t.amount
                  END), 0) AS balance
            FROM accounts a
            LEFT JOIN transactions t ON t.account_id = a.id OR t.to_account_id = a.id
            GROUP BY a.id ORDER BY a.name
            """
        ).fetchall()
        return {r["name"]: r["balance"] for r in rows}

    def _totals_by_category(self, month: str, kind: str) -> dict[str, int]:
        start, end = _month_bounds(month)
        rows = self.conn.execute(
            "SELECT c.name, SUM(t.amount) AS total FROM transactions t"
            " JOIN categories c ON c.id = t.category_id"
            " WHERE t.kind = ? AND t.date BETWEEN ? AND ?"
            " GROUP BY c.id ORDER BY total DESC, c.name",
            (kind, start, end),
        ).fetchall()
        return {r["name"]: r["total"] for r in rows}

    def month_summary(self, month: str) -> dict:
        month = parse_month(month)
        income = self._totals_by_category(month, INCOME)
        expense = self._totals_by_category(month, EXPENSE)
        total_in, total_out = sum(income.values()), sum(expense.values())
        return {
            "month": month,
            "income": income,
            "expense": expense,
            "total_income": total_in,
            "total_expense": total_out,
            "net": total_in - total_out,
            # доля сбережений от дохода, %; None — если дохода в месяце не было
            "savings_rate": round((total_in - total_out) * 100 / total_in, 1) if total_in else None,
        }

    def trend(self, months: int, until: str | None = None) -> list[dict]:
        last = parse_month(until or current_month())
        result = []
        for offset in range(months - 1, -1, -1):
            summary = self.month_summary(_shift_month(last, -offset))
            result.append({k: summary[k] for k in ("month", "total_income", "total_expense", "net")})
        return result

    # --- бюджеты -------------------------------------------------------

    def set_budget(self, category: str, amount, month: str):
        category_id = self._category_for(category, EXPENSE)
        with self.conn:
            self.conn.execute(
                "INSERT INTO budgets (category_id, month, amount) VALUES (?, ?, ?)"
                " ON CONFLICT (category_id, month) DO UPDATE SET amount = excluded.amount",
                (category_id, parse_month(month), _amount(amount)),
            )

    def copy_budgets(self, from_month: str, to_month: str) -> int:
        """Перенести лимиты на новый месяц; уже заданные там не трогаем."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO budgets (category_id, month, amount)"
                " SELECT category_id, ?, amount FROM budgets WHERE month = ?",
                (parse_month(to_month), parse_month(from_month)),
            )
        return cur.rowcount

    def budget_status(self, month: str, today: date | None = None) -> list[dict]:
        month = parse_month(month)
        spent = self._totals_by_category(month, EXPENSE)
        rows = self.conn.execute(
            "SELECT c.name, b.amount FROM budgets b JOIN categories c ON c.id = b.category_id"
            " WHERE b.month = ? ORDER BY c.name",
            (month,),
        ).fetchall()
        # Какая доля месяца прошла — чтобы предупреждать о перерасходе заранее,
        # а не только когда лимит уже пробит.
        today = today or date.today()
        start, end = (date.fromisoformat(d) for d in _month_bounds(month))
        if today < start:
            elapsed = 0.0
        elif today > end:
            elapsed = 1.0
        else:
            elapsed = today.day / end.day
        result = []
        for r in rows:
            used = spent.get(r["name"], 0)
            percent = used * 100 / r["amount"]
            if used > r["amount"]:
                state = "over"
            elif elapsed and percent > elapsed * 100 + 10:
                state = "fast"  # тратится быстрее, чем идёт месяц
            else:
                state = "ok"
            result.append({
                "category": r["name"], "limit": r["amount"], "spent": used,
                "left": r["amount"] - used, "percent": round(percent, 1), "state": state,
            })
        return result

    # --- регулярные платежи --------------------------------------------

    def add_recurring(self, name, kind, amount, account, category, day: int) -> int:
        if kind not in (INCOME, EXPENSE):
            raise FinanceError("тип регулярной операции: income или expense")
        if not 1 <= day <= 31:
            raise FinanceError("день месяца должен быть от 1 до 31")
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO recurring (name, kind, amount, account_id, category_id, day)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (name, kind, _amount(amount), self._account_id(account),
                     self._category_for(category, kind), day),
                )
        except sqlite3.IntegrityError:
            raise FinanceError(f"регулярная операция «{name}» уже есть") from None
        return cur.lastrowid

    def recurring(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.id, r.name, r.kind, r.amount, r.day, a.name AS account, c.name AS category"
            " FROM recurring r JOIN accounts a ON a.id = r.account_id"
            " JOIN categories c ON c.id = r.category_id ORDER BY r.day, r.name"
        ).fetchall()

    def apply_recurring(self, month: str, today: date | None = None) -> list[str]:
        """Провести регулярные операции месяца, у которых уже наступил день.

        Повторный запуск за тот же месяц ничего не задваивает — это
        гарантирует UNIQUE (recurring_id, recurring_month).
        """
        month = parse_month(month)
        today = today or date.today()
        year, mon = (int(p) for p in month.split("-"))
        last_day = monthrange(year, mon)[1]
        applied = []
        for r in self.recurring():
            on = date(year, mon, min(r["day"], last_day))
            if on > today:
                continue
            try:
                self._insert(r["kind"], r["amount"], r["account"], r["category"],
                             on.isoformat(), r["name"], recurring=(r["id"], month))
            except sqlite3.IntegrityError:
                continue  # уже проведена в этом месяце
            applied.append(r["name"])
        return applied

    # --- CSV -----------------------------------------------------------

    def export_csv(self, path: str) -> int:
        txs = self.transactions()
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for t in txs:
                writer.writerow({
                    "date": t.date, "type": t.kind, "amount": format_amount(t.amount).replace(" ", ""),
                    "account": t.account, "category": t.category or "",
                    "to_account": t.to_account or "", "note": t.note,
                })
        return len(txs)

    def import_csv(self, path: str) -> int:
        """Импорт целиком или никак: при ошибке в любой строке ничего не записывается.

        Недостающие счета и категории создаются автоматически.
        """
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        self.conn.execute("SAVEPOINT import_csv")
        try:
            for line_no, row in enumerate(rows, start=2):
                try:
                    self._import_row(row)
                except (FinanceError, ValueError, KeyError) as exc:
                    raise FinanceError(f"строка {line_no}: {exc}") from None
        except BaseException:
            self.conn.execute("ROLLBACK TO import_csv")
            self.conn.execute("RELEASE import_csv")
            raise
        self.conn.execute("RELEASE import_csv")
        self.conn.commit()
        return len(rows)

    def _import_row(self, row: dict):
        kind = (row.get("type") or "").strip()
        if not (row.get("date") or "").strip():
            raise FinanceError("не указана дата")
        account = (row.get("account") or "").strip()
        self._ensure_account(account)
        if kind == TRANSFER:
            to_account = (row.get("to_account") or "").strip()
            self._ensure_account(to_account)
            self._raw_insert(kind, row, account, to_account=to_account)
        elif kind in (INCOME, EXPENSE):
            category = (row.get("category") or "").strip()
            if not self.conn.execute("SELECT 1 FROM categories WHERE name = ?", (category,)).fetchone():
                if not category:
                    raise FinanceError("не указана категория")
                self.conn.execute("INSERT INTO categories (name, kind) VALUES (?, ?)", (category, kind))
            self._raw_insert(kind, row, account, category=category)
        else:
            raise FinanceError(f"неизвестный тип операции {kind!r}")

    def _ensure_account(self, name: str):
        if not name:
            raise FinanceError("не указан счёт")
        self.conn.execute("INSERT OR IGNORE INTO accounts (name) VALUES (?)", (name,))

    def _raw_insert(self, kind, row, account, to_account=None, category=None):
        # Без `with self.conn`: транзакцией управляет import_csv (SAVEPOINT).
        if to_account == account:
            raise FinanceError("перевод на тот же самый счёт")
        self.conn.execute(
            "INSERT INTO transactions (date, kind, amount, account_id, to_account_id, category_id, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (parse_date(row.get("date")), kind, _amount(row.get("amount", "")),
             self._account_id(account),
             self._account_id(to_account) if to_account else None,
             self._category_for(category, kind) if category else None,
             (row.get("note") or "").strip()),
        )

"""SQLite: схема и доступ к данным. Одна база data/awer.db."""
from __future__ import annotations

import calendar
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from .planning.engine import History, month_add

SCHEMA = """
CREATE TABLE IF NOT EXISTS sellers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,            -- как в 1С, без суффикса магазина
    role TEXT NOT NULL DEFAULT 'seller',  -- seller | admin
    active INTEGER NOT NULL DEFAULT 1,
    first_name TEXT,                      -- обращение в сообщениях
    max_user_id INTEGER UNIQUE,           -- кому отправлять в MAX
    invite_code TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS seller_aliases (     -- другие написания в 1С / ведомостях
    alias TEXT PRIMARY KEY,
    seller_id INTEGER NOT NULL REFERENCES sellers(id)
);
CREATE TABLE IF NOT EXISTS sales_lines (        -- строки чеков из 1С
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,                    -- YYYY-MM-DD
    store TEXT NOT NULL,
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    receipt TEXT NOT NULL,
    item TEXT,
    category TEXT,
    qty REAL NOT NULL,
    amount REAL NOT NULL,                 -- со скидкой; возврат — отрицательный
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_sales_day ON sales_lines(day);
CREATE INDEX IF NOT EXISTS ix_sales_seller_day ON sales_lines(seller_id, day);
CREATE TABLE IF NOT EXISTS imported_days (      -- признак «день закрыт» по магазину
    day TEXT NOT NULL,
    store TEXT NOT NULL,
    source TEXT,
    lines INTEGER,
    imported_at TEXT,
    PRIMARY KEY (day, store)
);
CREATE TABLE IF NOT EXISTS monthly_history (    -- история из ведомостей ЗП (до начала ежедневных данных)
    month TEXT NOT NULL,
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    sales REAL NOT NULL,
    fte REAL NOT NULL,
    source TEXT,
    PRIMARY KEY (month, seller_id)
);
CREATE TABLE IF NOT EXISTS month_coverage (     -- доля месяца в данных (история: 0.5, если только 16–31)
    month TEXT PRIMARY KEY,
    coverage REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule (           -- график смен (необязательно); иначе норма смен
    day TEXT NOT NULL,
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    PRIMARY KEY (day, seller_id)
);
CREATE TABLE IF NOT EXISTS plans (              -- план фиксируется 1-го числа и не меняется в течение месяца
    month TEXT NOT NULL,
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    plan_full REAL,
    method TEXT,
    level REAL,
    trend REAL,
    store_plan REAL,
    coef REAL,
    frozen_at TEXT,
    PRIMARY KEY (month, seller_id)
);
CREATE TABLE IF NOT EXISTS results (            -- итоги месяца (для ведомости и стабилизатора)
    month TEXT NOT NULL,
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    sales REAL, fte REAL, performance REAL, steps INTEGER, rate REAL,
    closed_at TEXT,
    PRIMARY KEY (month, seller_id)
);
CREATE TABLE IF NOT EXISTS message_log (
    id INTEGER PRIMARY KEY,
    sent_at TEXT, day TEXT, kind TEXT, seller_id INTEGER, max_user_id INTEGER,
    status TEXT, text TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS month_settings (     -- ручные настройки месяца (страница «Настройки месяца»)
    month TEXT PRIMARY KEY,
    trend_manual REAL,                    -- ручной коэффициент год к году (NULL = расчётный)
    step REAL,                            -- шаг перевыполнения, доля (0.10)
    step_bonus REAL,                      -- надбавка за шаг, ₽
    base_rate REAL,
    max_rate REAL,
    note TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_overrides (     -- ручной личный план на полный месяц
    month TEXT NOT NULL,
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    plan_manual REAL NOT NULL,
    note TEXT,
    updated_at TEXT,
    PRIMARY KEY (month, seller_id)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")  # бот и админка работают с базой одновременно
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args):
        r = self.conn.execute(sql, args).fetchone()
        return r

    # ------------------------------------------------------------------ kv
    def get_kv(self, k: str, default: str | None = None) -> str | None:
        r = self.one("SELECT v FROM kv WHERE k=?", k)
        return r["v"] if r else default

    def set_kv(self, k: str, v: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # ------------------------------------------------------------------ sellers
    def seller_id(self, name: str, create: bool = True) -> int | None:
        name = " ".join(name.split())
        r = self.one("SELECT seller_id FROM seller_aliases WHERE alias=?", name)
        if r:
            return r["seller_id"]
        r = self.one("SELECT id FROM sellers WHERE name=?", name)
        if r:
            return r["id"]
        if not create:
            return None
        with self.tx() as c:
            cur = c.execute("INSERT INTO sellers(name, first_name) VALUES(?,?)",
                            (name, name.split()[-1] if len(name.split()) > 1 else name))
            return cur.lastrowid

    def sellers(self, active_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sellers" + (" WHERE active=1" if active_only else "") + " ORDER BY role DESC, name"
        return self.q(sql)

    def seller(self, sid: int) -> sqlite3.Row:
        return self.one("SELECT * FROM sellers WHERE id=?", sid)

    def seller_by_user(self, user_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sellers WHERE max_user_id=?", user_id)

    def new_invite(self, sid: int) -> str:
        code = secrets.token_hex(3).upper()
        with self.tx() as c:
            c.execute("UPDATE sellers SET invite_code=? WHERE id=?", (code, sid))
        return code

    def link_by_code(self, code: str, user_id: int) -> sqlite3.Row | None:
        r = self.one("SELECT * FROM sellers WHERE invite_code=?", code.strip().upper())
        if not r:
            return None
        with self.tx() as c:
            c.execute("UPDATE sellers SET max_user_id=NULL WHERE max_user_id=?", (user_id,))
            c.execute("UPDATE sellers SET max_user_id=?, invite_code=NULL WHERE id=?", (user_id, r["id"]))
        return self.seller(r["id"])

    def roles(self) -> dict[int, str]:
        return {r["id"]: r["role"] for r in self.q("SELECT id, role FROM sellers")}

    # ------------------------------------------------------------------ history view
    def daily_months(self) -> list[str]:
        return [r["m"] for r in self.q("SELECT DISTINCT substr(day,1,7) m FROM imported_days ORDER BY m")]

    def history(self, norm_shifts: int, until_month: str | None = None) -> History:
        """Помесячная история для движка. Ключ продавца — str(seller_id).

        Месяцы с ежедневными данными из 1С берутся из sales_lines (отработано = смены / норма),
        остальные — из monthly_history (ведомости ЗП)."""
        h = History(roles={str(k): ("admin" if v == "admin" else "seller") for k, v in self.roles().items()})
        daily = set(self.daily_months())
        for r in self.q("SELECT month, seller_id, sales, fte FROM monthly_history"):
            if r["month"] not in daily:
                h.add(r["month"], str(r["seller_id"]), r["sales"], r["fte"])
        for r in self.q("SELECT month, coverage FROM month_coverage"):
            if r["month"] not in daily:
                h.coverage[r["month"]] = r["coverage"]
        for m in daily:
            if until_month and m > until_month:
                continue
            y, mm = map(int, m.split("-"))
            days_in = calendar.monthrange(y, mm)[1]
            n_days = self.one("SELECT COUNT(DISTINCT day) n FROM imported_days WHERE substr(day,1,7)=?", m)["n"]
            h.coverage[m] = min(1.0, n_days / days_in) if n_days else 1.0
            for r in self.q(
                "SELECT seller_id, SUM(amount) s, COUNT(DISTINCT day) shifts FROM sales_lines "
                "WHERE substr(day,1,7)=? GROUP BY seller_id", m):
                h.add(m, str(r["seller_id"]), r["s"], r["shifts"] / norm_shifts)
        return h

    def worked_shifts(self, sid: int, month: str, upto: date | None = None) -> int:
        sql = "SELECT COUNT(DISTINCT day) n FROM sales_lines WHERE seller_id=? AND substr(day,1,7)=?"
        args: list = [sid, month]
        if upto:
            sql += " AND day<=?"
            args.append(upto.isoformat())
        return self.one(sql, *args)["n"]

    def planned_shifts(self, sid: int, month: str) -> int | None:
        n = self.one("SELECT COUNT(*) n FROM schedule WHERE seller_id=? AND substr(day,1,7)=?", sid, month)["n"]
        return n or None

    def day_closed(self, day: date, stores: list[str]) -> list[str]:
        """Список магазинов, по которым ещё нет выгрузки за день (пусто = день закрыт)."""
        have = {r["store"] for r in self.q("SELECT store FROM imported_days WHERE day=?", day.isoformat())}
        return [s for s in stores if s not in have]

    def log_message(self, day: str, kind: str, sid: int | None, uid: int | None, status: str, text: str,
                    error: str | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO message_log(sent_at,day,kind,seller_id,max_user_id,status,text,error) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (datetime.now().isoformat(timespec="seconds"), day, kind, sid, uid, status, text, error))

    def already_sent(self, day: str, kind: str, sid: int | None) -> bool:
        r = self.one("SELECT 1 FROM message_log WHERE day=? AND kind=? AND seller_id IS ? AND status='ok'",
                     day, kind, sid)
        return bool(r)


def prev_month(m: str) -> str:
    return month_add(m, -1)

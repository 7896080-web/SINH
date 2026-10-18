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
    role TEXT NOT NULL DEFAULT 'seller',  -- seller | admin | system («(без продавца)» — только в общий выторг)
    active INTEGER NOT NULL DEFAULT 1,
    first_name TEXT,                      -- обращение в сообщениях
    max_user_id INTEGER UNIQUE,           -- кому отправлять в MAX
    invite_code TEXT UNIQUE,
    hired_at TEXT,                        -- добавлен в админке (YYYY-MM-DD); из 1С — пусто, первая смена по продажам
    fired_at TEXT                         -- уволен (YYYY-MM-DD): active=0, отвязан от MAX
);
CREATE TABLE IF NOT EXISTS seller_aliases (     -- другие написания в 1С / ведомостях
    alias TEXT PRIMARY KEY,
    seller_id INTEGER NOT NULL REFERENCES sellers(id)
);
CREATE TABLE IF NOT EXISTS sales_lines (        -- строки «Отчета о розничных продажах» (ТЧ Товары / Возвращенные)
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,                    -- YYYY-MM-DD
    store TEXT NOT NULL,
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    doc TEXT,                             -- номер ОРП или чека
    receipt TEXT,                         -- номер чека (если выгрузка из чеков; из ОРП — пусто)
    time TEXT,                            -- время чека HH:MM (если из чеков)
    item TEXT,
    category TEXT,
    qty REAL NOT NULL,
    amount REAL NOT NULL,                 -- со скидкой; возврат — отрицательный
    discount REAL NOT NULL DEFAULT 0,     -- сумма скидки по строке (цена × кол-во − сумма)
    manual_discount REAL NOT NULL DEFAULT 0, -- из неё ручная скидка, ₽
    card TEXT,                            -- дисконтная карта (пусто — без карты)
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_sales_card ON sales_lines(card);
CREATE TABLE IF NOT EXISTS stock (              -- остатки на складах магазинов (последний снимок на дату)
    day TEXT NOT NULL,
    store TEXT NOT NULL,
    item TEXT NOT NULL,
    category TEXT,
    size TEXT NOT NULL DEFAULT '',
    qty REAL NOT NULL,
    PRIMARY KEY (day, store, item, size)
);
CREATE TABLE IF NOT EXISTS arrivals (           -- поступления на склады магазинов
    day TEXT NOT NULL,
    store TEXT NOT NULL,
    item TEXT NOT NULL,
    category TEXT,
    size TEXT NOT NULL DEFAULT '',
    qty REAL NOT NULL,
    doc TEXT
);
CREATE INDEX IF NOT EXISTS ix_arrivals_day ON arrivals(day);
CREATE TABLE IF NOT EXISTS store_days (         -- шапка ОРП: чеков и выручка по кассе за день
    day TEXT NOT NULL,
    store TEXT NOT NULL,
    kassa TEXT NOT NULL,
    doc TEXT,
    checks INTEGER NOT NULL DEFAULT 0,
    return_checks INTEGER NOT NULL DEFAULT 0,
    revenue REAL NOT NULL DEFAULT 0,
    source TEXT,
    PRIMARY KEY (day, store, kassa)
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
CREATE TABLE IF NOT EXISTS schedule (           -- график работы: рабочие дни продавцов (необязательно; иначе норма)
    day TEXT NOT NULL,
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    store TEXT,                           -- магазин в этот день; пусто — магазин его смены (crews.py)
    PRIMARY KEY (day, seller_id)
);
CREATE TABLE IF NOT EXISTS crews (              -- смены: состав продавцов одного магазина; по средам меняются магазинами
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    store TEXT NOT NULL,                  -- магазин смены на неделю, которая начинается в среду `anchor`
    anchor TEXT NOT NULL                  -- среда YYYY-MM-DD; дальше магазины чередуются каждую среду
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
CREATE TABLE IF NOT EXISTS weekly_goals (       -- цель недели продавца (понедельник–воскресенье), goals.py
    week TEXT NOT NULL,                   -- понедельник, YYYY-MM-DD
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    metric TEXT NOT NULL,                 -- upt | items_per_shift | avg_price | discount_share
    baseline REAL,                        -- у продавца за 4 недели до цели
    team REAL,                            -- у команды за те же 4 недели
    target REAL NOT NULL,
    result REAL,                          -- за неделю цели (после её окончания)
    done INTEGER,                         -- 1 выполнена, 0 нет, NULL — не работал на неделе
    closed_at TEXT,
    PRIMARY KEY (week, seller_id)
);
CREATE TABLE IF NOT EXISTS content (            -- обучение: content.py; стартовые тексты — content/*.yaml
    id TEXT PRIMARY KEY,                  -- obraz-1, onb-3, tkan-hlopok, test-tkani-1 …
    kind TEXT NOT NULL,                   -- lesson | onboarding | article | test | survey | checklist
    topic TEXT,                           -- урок: тема цели недели; статья: раздел базы знаний
    title TEXT NOT NULL,
    body TEXT,
    try_today TEXT,
    data TEXT,                            -- JSON: quiz / questions / items / shift / tags / article_ids
    active INTEGER NOT NULL DEFAULT 1,
    origin TEXT,                          -- yaml — из файла; admin — правка владельца (файлом не перезаписывается)
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS campaigns (          -- рассылка заданий: тест, опрос, чек-лист (tasks.py)
    id INTEGER PRIMARY KEY,
    content_id TEXT NOT NULL,             -- content.id (kind test | survey | checklist)
    audience TEXT NOT NULL,               -- all | newcomers | store:<магазин> | crew:<id>
    repeat TEXT NOT NULL,                 -- once | daily | weekly
    start_day TEXT NOT NULL,
    weekday INTEGER,                      -- weekly: 0 = пн
    hour_from TEXT, hour_to TEXT,         -- тестовые часы этой рассылки; пусто — training.test_hours
    only_working INTEGER NOT NULL DEFAULT 1,  -- только тем, у кого сегодня рабочий день по графику
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS deliveries (         -- кому и когда ушло задание; score — тест: доля верных, иначе 1
    campaign_id INTEGER NOT NULL, seller_id INTEGER NOT NULL, day TEXT NOT NULL,
    sent_at TEXT, done_at TEXT, score REAL,
    PRIMARY KEY (campaign_id, seller_id, day)
);
CREATE TABLE IF NOT EXISTS answers (            -- ответы на тесты, опросы (в т.ч. своими словами), отметки чек-листов
    campaign_id INTEGER NOT NULL, seller_id INTEGER NOT NULL, day TEXT NOT NULL, q INTEGER NOT NULL,
    answer INTEGER, text TEXT, correct INTEGER, at TEXT,
    PRIMARY KEY (campaign_id, seller_id, day, q)
);
CREATE TABLE IF NOT EXISTS kb_views (           -- кто какие статьи базы знаний открывал (kb.py)
    seller_id INTEGER NOT NULL, article_id TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons_sent (       -- какие уроки и когда ушли продавцу, lessons.py
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    lesson_id TEXT NOT NULL,              -- id из content/lessons.yaml; программа новичка — onb-<смена>
    sent_at TEXT NOT NULL,
    PRIMARY KEY (seller_id, lesson_id)
);
CREATE TABLE IF NOT EXISTS quiz_answers (       -- ответы на квизы после уроков
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    lesson_id TEXT NOT NULL,
    q INTEGER NOT NULL,                   -- номер вопроса с 0
    answer INTEGER NOT NULL,
    correct INTEGER NOT NULL,
    answered_at TEXT NOT NULL,
    PRIMARY KEY (seller_id, lesson_id, q)
);
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
        self._migrate()

    def _migrate(self) -> None:
        """Новые колонки в базах, созданных раньше (CREATE TABLE IF NOT EXISTS их не добавляет)."""
        new = {"arrivals": [("brand", "TEXT"), ("color", "TEXT")],
               "sellers": [("hired_at", "TEXT"), ("fired_at", "TEXT"), ("crew_id", "INTEGER"),
                           ("weekend_key", "INTEGER NOT NULL DEFAULT 0"),  # сильный продавец — ставить на сб и вс
                           ("training_user_id", "INTEGER"),  # user_id в боте «Обучение»
                           ("training_code", "TEXT")],  # код подключения к боту «Обучение»
               "schedule": [("store", "TEXT")]}
        for table, cols in new.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols:
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.commit()

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
        sql = ("SELECT * FROM sellers WHERE role<>'system'" + (" AND active=1" if active_only else "")
               + " ORDER BY role DESC, name")
        return self.q(sql)

    def seller(self, sid: int) -> sqlite3.Row:
        return self.one("SELECT * FROM sellers WHERE id=?", sid)

    def seller_by_user(self, user_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sellers WHERE max_user_id=? AND active=1", user_id)

    def seller_by_training_user(self, user_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sellers WHERE training_user_id=? AND active=1", user_id)

    def new_training_code(self, sid: int) -> str:
        code = secrets.token_hex(3).upper()
        with self.tx() as c:
            c.execute("UPDATE sellers SET training_code=? WHERE id=?", (code, sid))
        return code

    def link_training(self, code: str, user_id: int) -> sqlite3.Row | None:
        r = self.one("SELECT * FROM sellers WHERE training_code=? AND active=1", code.strip().upper())
        if not r:
            return None
        with self.tx() as c:
            c.execute("UPDATE sellers SET training_user_id=NULL WHERE training_user_id=?", (user_id,))
            c.execute("UPDATE sellers SET training_user_id=?, training_code=NULL WHERE id=?", (user_id, r["id"]))
        return self.seller(r["id"])

    def add_seller(self, name: str, first_name: str | None = None, role: str = "seller") -> int:
        """Продавец, добавленный в админке до первой выгрузки 1С. ValueError — такое имя уже есть."""
        name = " ".join(name.split())
        if not name:
            raise ValueError("Пустое имя")
        if self.seller_id(name, create=False):
            raise ValueError(f"«{name}» уже есть в списке")
        with self.tx() as c:
            cur = c.execute("INSERT INTO sellers(name, first_name, role, hired_at) VALUES(?,?,?,?)",
                            (name, first_name or (name.split()[-1] if len(name.split()) > 1 else name), role,
                             date.today().isoformat()))
            return cur.lastrowid

    def fire(self, sid: int, day: date | None = None) -> None:
        """Увольнение: не участвует в планах и рассылке, отвязан от бота. Продажи и итоги остаются."""
        with self.tx() as c:
            c.execute("UPDATE sellers SET active=0, fired_at=?, max_user_id=NULL, invite_code=NULL, "
                      "training_user_id=NULL, training_code=NULL WHERE id=?",
                      ((day or date.today()).isoformat(), sid))

    def restore(self, sid: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE sellers SET active=1, fired_at=NULL WHERE id=?", (sid,))

    def shift_range(self, sid: int) -> tuple[str | None, str | None]:
        r = self.one("SELECT MIN(day) a, MAX(day) b FROM sales_lines WHERE seller_id=?", sid)
        return r["a"], r["b"]

    def new_invite(self, sid: int) -> str:
        code = secrets.token_hex(3).upper()
        with self.tx() as c:
            c.execute("UPDATE sellers SET invite_code=? WHERE id=?", (code, sid))
        return code

    def link_by_code(self, code: str, user_id: int) -> sqlite3.Row | None:
        r = self.one("SELECT * FROM sellers WHERE invite_code=? AND active=1", code.strip().upper())
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
        h = History(roles={str(k): (v if v in ("admin", "system") else "seller") for k, v in self.roles().items()})
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

    def scheduled_after(self, sid: int, month: str, day: date) -> int | None:
        """Смен по графику после `day` до конца месяца; None — графика на этот месяц у продавца нет."""
        r = self.one("SELECT COUNT(*) n, SUM(day>?) ahead FROM schedule WHERE seller_id=? AND substr(day,1,7)=?",
                     day.isoformat(), sid, month)
        return int(r["ahead"] or 0) if r["n"] else None

    def schedule_days(self, month: str) -> dict[int, dict[int, str | None]]:
        """{seller_id: {число месяца: магазин или None — магазин его смены}}."""
        out: dict[int, dict[int, str | None]] = {}
        for r in self.q("SELECT seller_id, day, store FROM schedule WHERE substr(day,1,7)=?", month):
            out.setdefault(r["seller_id"], {})[int(r["day"][8:10])] = r["store"]
        return out

    def set_schedule(self, month: str, sid: int, days) -> None:
        """days: {число: магазин или None} или просто множество чисел (магазин — по смене)."""
        y, m = map(int, month.split("-"))
        days = days if isinstance(days, dict) else {d: None for d in days}
        with self.tx() as c:
            c.execute("DELETE FROM schedule WHERE seller_id=? AND substr(day,1,7)=?", (sid, month))
            c.executemany("INSERT INTO schedule(day, seller_id, store) VALUES(?,?,?)",
                          [(date(y, m, d).isoformat(), sid, st or None) for d, st in sorted(days.items())])

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

    def already_sent_to(self, day: str, kind: str, uid: int) -> bool:
        return bool(self.one("SELECT 1 FROM message_log WHERE day=? AND kind=? AND max_user_id=? AND status='ok'",
                             day, kind, uid))

    def already_sent(self, day: str, kind: str, sid: int | None) -> bool:
        r = self.one("SELECT 1 FROM message_log WHERE day=? AND kind=? AND seller_id IS ? AND status='ok'",
                     day, kind, sid)
        return bool(r)


def prev_month(m: str) -> str:
    return month_add(m, -1)

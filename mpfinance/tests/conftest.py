"""Окружение тестов «Финансов площадок». Переменные — ДО импорта `finapp`:
`database.py`, `crypto.py`, `config.py` читают их на импорте. База — свой файл
на процесс; база sync_admin — поддельный SQLite-файл той же формы (фикстура
`sync_db`), по умолчанию его нет — как на сервере без sync_admin."""
import os
import sqlite3
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="mpfinance-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ.setdefault("MPFIN_SECRETS_KEY", "q0Vk1gYz3m0cZ8m4mYl3Hq0z3zZkq5c1j9p7n4Qm2wQ=")
os.environ.setdefault("MPFIN_SESSION_SECRET", "test-secret-32-characters-long-xx")
os.environ["MPFIN_ONEC_TASKS_DIR"] = str(_TMP / "sync" / "tasks")
os.environ["MPFIN_ONEC_RESULTS_DIR"] = str(_TMP / "sync" / "results" / "finance")
os.environ["MPFIN_ONEC_ARCHIVE_DIR"] = str(_TMP / "sync" / "archive" / "finance")
os.environ["MPFIN_BACKUP_DIR"] = str(_TMP / "backups")
os.environ["MPFIN_SYNC_DB"] = str(_TMP / "sync_admin.db")
os.environ["MPFIN_BACKGROUND"] = "0"

import pytest  # noqa: E402

from finapp import config  # noqa: E402
from finapp.database import Base, SessionLocal, engine  # noqa: E402
import finapp.models  # noqa: E402,F401

# Схема — подмножество таблиц sync_admin, которые читает `syncdb.py`, в том
# виде, в каком их создаёт его SQLAlchemy (Enum — строкой имени, DateTime —
# «ГГГГ-ММ-ДД ЧЧ:ММ:СС[.мкс]»).
SYNC_SCHEMA = """
CREATE TABLE platform_accounts (id INTEGER PRIMARY KEY, platform VARCHAR(4) NOT NULL,
    name VARCHAR(128) NOT NULL, is_active BOOLEAN NOT NULL DEFAULT 1);
CREATE TABLE products (uid_1c VARCHAR(36) PRIMARY KEY, article VARCHAR(64), name VARCHAR(255),
    size VARCHAR(32), color VARCHAR(128), stock_on_hand INTEGER);
CREATE TABLE barcodes (id INTEGER PRIMARY KEY, barcode VARCHAR(64) UNIQUE NOT NULL,
    uid_1c VARCHAR(36) NOT NULL, source_platform VARCHAR(16), created_at DATETIME);
CREATE TABLE processed_orders (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
    order_id VARCHAR(128) NOT NULL, uid_1c VARCHAR(36), quantity INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(9) NOT NULL, processed_at DATETIME, cancelled_at DATETIME);
"""


class FakeSyncDb:
    def __init__(self, path: Path):
        self.path = path
        conn = sqlite3.connect(path)
        conn.executescript(SYNC_SCHEMA)
        conn.commit()
        conn.close()

    def _run(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def account(self, account_id, platform="wb", name="ИП Тест"):
        self._run("INSERT INTO platform_accounts (id, platform, name) VALUES (?, ?, ?)",
                  (account_id, platform, name))

    def product(self, uid, article="A-1", name="Джинсы", size="46", color="синий"):
        self._run("INSERT INTO products (uid_1c, article, name, size, color, stock_on_hand) "
                  "VALUES (?, ?, ?, ?, ?, 0)", (uid, article, name, size, color))

    def barcode(self, barcode, uid, source="1c_dict"):
        self._run("INSERT INTO barcodes (barcode, uid_1c, source_platform) VALUES (?, ?, ?)",
                  (barcode, uid, source))

    def order(self, account_id, order_id, status="confirmed", at="2026-09-03 12:00:00",
              cancelled_at=None, quantity=1):
        self._run("INSERT INTO processed_orders (account_id, order_id, quantity, status, "
                  "processed_at, cancelled_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (account_id, order_id, quantity, status, at, cancelled_at))


@pytest.fixture
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_sync_db():
    """По умолчанию базы sync_admin нет; фикстура `sync_db` её заводит."""
    path = Path(config.SYNC_DB_PATH)
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.fixture
def sync_db():
    return FakeSyncDb(Path(config.SYNC_DB_PATH))


@pytest.fixture
def exchange_dirs():
    import shutil
    for d in (config.ONEC_TASKS_DIR, config.ONEC_RESULTS_DIR, config.ONEC_ARCHIVE_DIR):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def onec_on(db):
    """Обмен с 1С включён на «Диагностике» (по умолчанию он выключен)."""
    from finapp import settings
    settings.put(db, settings.ONEC_ENABLED, "1")
    db.commit()


@pytest.fixture
def epf_ready(db, onec_on):
    """Обмен включён, и обработка 1С ответила на FIN_PING версией mark-4."""
    from finapp import settings
    settings.put(db, settings.ONEC_EPF_VERSION, "mark-4")
    db.commit()


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from finapp.main import app
    from finapp.models import User
    from finapp.security import hash_password

    db.add(User(username="admin", password_hash=hash_password("password1")))
    db.commit()
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "admin", "password": "password1"},
                   follow_redirects=False)
        assert r.status_code == 303
        yield c


@pytest.fixture
def anon_client(db):
    from fastapi.testclient import TestClient

    from finapp.main import app
    with TestClient(app) as c:
        yield c

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
os.environ["MPFIN_CRM_DB"] = str(_TMP / "kitcrm.db")
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


def _test_file(raw: str) -> Path:
    """Путь, который фикстура вправе удалить: ТОЛЬКО файл во временной папке
    тестов. Тесты гоняются на боевом сервере при каждом накате, а рядом лежат
    живые базы sync_admin и AWER CRM (`C:\\sync_admin\\sync_admin.db`,
    `C:\\awer_crm_data\\kitcrm.db`). Укажи путь туда — правкой окружения выше или
    порядком импорта, — и фикстура удалила бы их. Здесь отказ, а не удаление."""
    path = Path(raw).resolve()
    if _TMP.resolve() not in path.parents:
        raise RuntimeError(f"тест хотел удалить {path} — это не временный файл тестов; "
                           f"ничего не тронуто")
    return path


@pytest.fixture(autouse=True)
def _no_sync_db():
    """По умолчанию базы sync_admin нет; фикстура `sync_db` её заводит."""
    path = _test_file(config.SYNC_DB_PATH)
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.fixture
def sync_db():
    return FakeSyncDb(_test_file(config.SYNC_DB_PATH))


# Схема — те таблицы базы AWER CRM (kitcrm.db), которые читает
# `finance/sources/crm.py`, в том виде, в каком их создаёт SQLAlchemy CRM (снято
# с её базы 18.10.2026; лишние колонки оставлены — так и выглядит настоящая).
CRM_SCHEMA = """
CREATE TABLE orders (id VARCHAR NOT NULL PRIMARY KEY, order_number INTEGER, customer_id VARCHAR,
    status VARCHAR NOT NULL, payment_status VARCHAR, payment_method VARCHAR,
    total_price NUMERIC(12, 2) NOT NULL DEFAULT 0, total_final_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
    kit_created_at DATETIME, raw_payload JSON NOT NULL, pickup_notified_chunks JSON NOT NULL DEFAULT '[]',
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, purchased_price NUMERIC(12, 2));
CREATE TABLE order_items (id VARCHAR NOT NULL PRIMARY KEY, order_id VARCHAR NOT NULL,
    product_variant_id VARCHAR, quantity INTEGER NOT NULL, price NUMERIC(12, 2) NOT NULL,
    final_price NUMERIC(12, 2) NOT NULL, product_name VARCHAR, product_sku VARCHAR,
    is_returned BOOLEAN, product_barcode VARCHAR, refused_count INTEGER);
CREATE TABLE order_status_history (id VARCHAR NOT NULL PRIMARY KEY, order_id VARCHAR NOT NULL,
    old_status VARCHAR, new_status VARCHAR NOT NULL, changed_at DATETIME NOT NULL);
CREATE TABLE product_cache (variant_id VARCHAR NOT NULL PRIMARY KEY, name VARCHAR NOT NULL,
    sku VARCHAR NOT NULL, fetched_at DATETIME NOT NULL, barcode VARCHAR);
"""


class FakeCrm:
    """Поддельная база AWER CRM. Заказ — как его хранит CRM: строка `orders` с
    ответом KIT в `raw_payload` (доставки и позиции) плюс `order_items`."""

    def __init__(self, path: Path):
        self.path = path
        self._n = 0
        conn = sqlite3.connect(path)
        conn.executescript(CRM_SCHEMA)
        conn.commit()
        conn.close()

    def _run(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def order(self, number, chunks, status="COMPLETED", kit_updated_at="2026-09-01T12:00:00+03:00",
              kit_created_at="2026-09-01 09:00:00", crm_updated_at="2026-10-14 16:00:00",
              completed_at=None):
        """chunks: [(delivered_at или None, [(variant, final_price, quantity, refused, sku, barcode)])]."""
        import json
        oid = f"order-{number}"
        payload = {"id": oid, "order_number": number, "status": status, "updated_at": kit_updated_at,
                   "delivery_chunks": []}
        for delivered, items in chunks:
            info = {"raw_status": "DELIVERED" if delivered else "IN_DELIVERY_SERVICE"}
            if delivered:
                info["delivered_at"] = delivered
            payload["delivery_chunks"].append({"delivery_info": info, "items": [
                {"product_variant_id": vid, "final_price": str(price), "price": str(price),
                 "quantity": qty, "refused_count": refused, "name": f"Товар {sku}"}
                for vid, price, qty, refused, sku, barcode in items]})
            for vid, price, qty, refused, sku, barcode in items:
                self._n += 1
                self._run("INSERT INTO order_items (id, order_id, product_variant_id, quantity, price, "
                          "final_price, product_name, product_sku, is_returned, product_barcode, refused_count) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (f"item-{self._n}", oid, vid, qty, price, price, f"Товар {sku}", sku,
                           bool(refused), barcode, refused))
        self._run("INSERT INTO orders (id, order_number, status, kit_created_at, raw_payload, created_at, "
                  "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (oid, number, status, kit_created_at, json.dumps(payload, ensure_ascii=False),
                   kit_created_at, crm_updated_at))
        if completed_at:
            self._n += 1
            self._run("INSERT INTO order_status_history (id, order_id, old_status, new_status, changed_at) "
                      "VALUES (?, ?, ?, ?, ?)", (f"hist-{self._n}", oid, "WAIT_FOR_DELIVERY", "COMPLETED",
                                                 completed_at))

    def status(self, number, status):
        self._run("UPDATE orders SET status = ? WHERE order_number = ?", (status, number))

    def cached(self, variant, sku, barcode):
        self._run("INSERT INTO product_cache (variant_id, name, sku, fetched_at, barcode) VALUES (?, ?, ?, ?, ?)",
                  (variant, f"Товар {sku}", sku, "2026-09-01 00:00:00", barcode))


@pytest.fixture(autouse=True)
def _no_crm_db():
    """По умолчанию базы AWER CRM нет; фикстура `crm_db` её заводит."""
    path = _test_file(config.CRM_DB_PATH)
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.fixture
def crm_db():
    return FakeCrm(_test_file(config.CRM_DB_PATH))


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

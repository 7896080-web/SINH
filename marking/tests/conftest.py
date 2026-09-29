"""Окружение тестов программы маркировки.

Переменные задаются ДО импорта `markapp`: `database.py` и `crypto.py` читают их
на импорте. База — свой файл на процесс: общий файл при параллельных прогонах
сносил бы таблицы друг у друга (у sync_admin это выглядело сотней ошибок
«UNIQUE constraint failed», то есть как поломка кода).
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="marking-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ.setdefault("MARKING_SECRETS_KEY", "q0Vk1gYz3m0cZ8m4mYl3Hq0z3zZkq5c1j9p7n4Qm2wQ=")
os.environ.setdefault("MARKING_SESSION_SECRET", "test-secret-32-characters-long-xx")
os.environ["MARKING_ONEC_TASKS_DIR"] = str(_TMP / "sync" / "tasks")
os.environ["MARKING_ONEC_RESULTS_DIR"] = str(_TMP / "sync" / "results" / "marking")
os.environ["MARKING_ONEC_ARCHIVE_DIR"] = str(_TMP / "sync" / "archive" / "marking")
os.environ["MARKING_BACKUP_DIR"] = str(_TMP / "backups")
os.environ["MARKING_RCLONE_REMOTE"] = ""

import pytest  # noqa: E402

from markapp import config  # noqa: E402
from markapp.database import Base, SessionLocal, engine  # noqa: E402
from markapp.settings import ensure_defaults  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    ensure_defaults(session)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def exchange_dirs():
    """Чистые папки обмена с 1С на каждый тест."""
    import shutil
    for d in (config.ONEC_TASKS_DIR, config.ONEC_RESULTS_DIR, config.ONEC_ARCHIVE_DIR):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from markapp.main import app
    from markapp.models import User
    from markapp.security import hash_password

    db.add(User(username="op", password_hash=hash_password("password1")))
    db.commit()
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "op", "password": "password1"})
        assert r.status_code in (200, 303)
        yield c


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()

"""Окружение тестов «Репрайсера». Переменные — ДО импорта `priceapp`:
`database.py` и `crypto.py` читают их на импорте. База — свой файл на процесс."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="repricer-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ.setdefault("REPRICER_SECRETS_KEY", "q0Vk1gYz3m0cZ8m4mYl3Hq0z3zZkq5c1j9p7n4Qm2wQ=")
os.environ.setdefault("REPRICER_SESSION_SECRET", "test-secret-32-characters-long-xx")
os.environ["REPRICER_ONEC_TASKS_DIR"] = str(_TMP / "sync" / "tasks")
os.environ["REPRICER_ONEC_RESULTS_DIR"] = str(_TMP / "sync" / "results" / "pricing")
os.environ["REPRICER_ONEC_ARCHIVE_DIR"] = str(_TMP / "sync" / "archive" / "pricing")
os.environ["REPRICER_BACKUP_DIR"] = str(_TMP / "backups")
os.environ["REPRICER_BACKGROUND"] = "0"
os.environ["REPRICER_ONEC_SFTP_HOST"] = ""

import pytest  # noqa: E402

from priceapp import config  # noqa: E402
from priceapp.database import Base, SessionLocal, engine  # noqa: E402
from priceapp.settings import ensure_defaults  # noqa: E402


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
    import shutil
    for d in (config.ONEC_TASKS_DIR, config.ONEC_RESULTS_DIR, config.ONEC_ARCHIVE_DIR):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from priceapp.main import app
    from priceapp.models import User
    from priceapp.security import hash_password

    db.add(User(username="op", password_hash=hash_password("password1")))
    db.commit()
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "op", "password": "password1"})
        assert r.status_code in (200, 303)
        yield c

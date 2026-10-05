"""Окружение задаётся ДО импорта kizapp: config и crypto читают его на импорте."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="kiz-tests-"))
os.environ["KIZ_DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["KIZ_SECRETS_KEY"] = "q0Vk1gYz3m0cZ8m4mYl3Hq0z3zZkq5c1j9p7n4Qm2wQ="

import pytest  # noqa: E402

from kizapp.db import Base, SessionLocal, engine  # noqa: E402


@pytest.fixture
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from kizapp.web import app
    with TestClient(app) as c:
        yield c

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SESSION_SECRET", "test-secret-32-characters-long-x")
os.environ.setdefault("SECRETS_ENCRYPTION_KEY", "EfYA4-I-29JUEh7MiU9I4odQtKEB87pU5nac-bgqY8U=")

# Файловая, а не :memory: SQLite — TestClient гоняет запросы через пул потоков
# (anyio.to_thread), а SQLite ":memory:" при подключении из разных потоков
# каждый раз видит новую пустую базу. Файл на диске от этой проблемы избавлен.
_web_db_path = os.path.join(tempfile.gettempdir(), "sync_admin_web_tests.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_web_db_path}")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base


@pytest.fixture()
def db():
    """Изолированная in-memory БД — для юнит-тестов воркеров (свой движок,
    не связан с app.database.engine, который используют веб-тесты ниже)."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def web_db():
    """Для тестов веб-приложения: использует ТОТ ЖЕ движок, что и сам
    FastAPI-app (app.database.engine), т.к. роутеры берут сессии через
    app.database.get_db, а не через фикстуру выше."""
    from app.database import engine as app_engine
    Base.metadata.create_all(bind=app_engine)
    Session = sessionmaker(bind=app_engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=app_engine)


@pytest.fixture()
def client(web_db):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def logged_in_client(client, web_db):
    from app.models import User
    from app.security import hash_password

    web_db.add(User(username="admin", password_hash=hash_password("secret123")))
    web_db.commit()
    client.post("/login", data={"username": "admin", "password": "secret123"})
    return client

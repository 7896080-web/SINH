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
_default_url = f"sqlite:///{_web_db_path}"


def looks_like_test_database(url: str) -> bool:
    """Можно ли безопасно гонять по этому адресу тесты.

    Фикстура `web_db` после КАЖДОГО веб-теста делает `drop_all` — то есть сносит
    все таблицы. Пока DATABASE_URL не задан в окружении, это временный файл в
    %TEMP% и всё хорошо. Но тесты запускаются в том числе на боевом сервере (блок
    FINISH каждого патча), и одна случайная строка `$env:DATABASE_URL=...` в той
    же сессии PowerShell означала бы снос боевой базы. Поэтому принимаем только
    файловый SQLite, у которого в пути явно видно, что он тестовый."""
    if not url.startswith("sqlite:///"):
        return False                      # PostgreSQL и прочее — точно не тест
    path = url[len("sqlite:///"):].replace("\\", "/").lower()
    if path in ("", ":memory:"):
        return True
    if path == _web_db_path.replace("\\", "/").lower():
        return True
    return "test" in os.path.basename(path)


if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = _default_url
elif not looks_like_test_database(os.environ["DATABASE_URL"]):
    raise RuntimeError(
        "DATABASE_URL в окружении указывает на НЕ тестовую базу: "
        f"{os.environ['DATABASE_URL']}\n"
        "Тесты сносят все таблицы после каждого веб-теста — запуск по этому "
        "адресу уничтожил бы данные. Уберите переменную из окружения "
        "(в PowerShell: Remove-Item Env:DATABASE_URL) и запустите заново."
    )

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
    # autoflush=False — КАК В БОЮ (app/database.py). Раньше фикстура брала
    # умолчание SQLAlchemy (autoflush=True), и тестовая сессия вела себя иначе,
    # чем боевая: незаписанные изменения были видны запросам. На этом проехал
    # настоящий дефект — расчёт порога читал строки ответа 1С, ещё не ушедшие в
    # базу, в тестах видел их, а на боевом получал пустой снимок и считал порог
    # всему каталогу по нулям.
    Session = sessionmaker(bind=engine, autoflush=False)
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
    Session = sessionmaker(bind=app_engine, autoflush=False)   # как в бою
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

"""Предохранитель запуска тестов: фикстура `web_db` после каждого веб-теста
сносит ВСЕ таблицы (`drop_all`). Тесты гоняются в том числе на боевом сервере —
в блоке FINISH каждого патча, — поэтому адрес базы обязан быть заведомо тестовым.
Одна строка `$env:DATABASE_URL=...` в той же сессии PowerShell без этой проверки
означала бы снос боевой базы.
"""
import pytest

from tests.conftest import looks_like_test_database


@pytest.mark.parametrize("url", [
    "sqlite:///C:/Users/admin/AppData/Local/Temp/sync_admin_web_tests.db",
    "sqlite:///./alembic_test.db",
    "sqlite:///:memory:",
    "sqlite:///",
])
def test_test_databases_are_allowed(url):
    assert looks_like_test_database(url) is True


@pytest.mark.parametrize("url", [
    "sqlite:///C:/sync_admin/sync_admin.db",          # боевая база сервера
    "sqlite:////var/lib/sync/sync_admin.db",
    "postgresql://sync_user:pwd@localhost:5432/sync_admin",
    "postgresql+psycopg://user@host/db",
    "mysql://user@host/db",
])
def test_production_databases_are_refused(url):
    assert looks_like_test_database(url) is False


def test_windows_backslashes_do_not_fool_the_check():
    """PowerShell отдаёт путь с обратными слэшами — подмена разделителя не должна
    превращать боевую базу в «тестовую»."""
    assert looks_like_test_database(r"sqlite:///C:\sync_admin\sync_admin.db") is False


def test_only_the_file_name_counts_not_the_folder():
    """Папка с именем вроде C:\\test\\ не делает базу тестовой: решает имя файла,
    иначе боевая база в каталоге «...\\latest\\» прошла бы проверку."""
    assert looks_like_test_database("sqlite:///C:/test/sync_admin.db") is False
    assert looks_like_test_database("sqlite:///C:/prod/sync_admin_test.db") is True

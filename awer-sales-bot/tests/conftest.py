"""Общее для всех тестов: окружение без боевых секретов (аудит У1).

Тесты запускаются и в папке установленной программы (install.ps1 при обновлении), где рядом лежит настоящий .env
с токенами MAX. Ни один тест не должен видеть эти значения: каждый тест начинает с чистого окружения, а нужные
значения ставит сам через monkeypatch."""
import pytest

SECRETS = ("MAX_BOT_TOKEN", "MAX_TRAINING_BOT_TOKEN", "ADMIN_PASSWORD", "UPLOAD_TOKEN", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch):
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)

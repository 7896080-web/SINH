import subprocess
import sys
import os

from app.models import User
from app.security import verify_password, hash_password
from datetime import datetime, timedelta
from app.timeutils import now_utc


def _run_reset(env, *args):
    # Кодировку задаём с ОБЕИХ сторон и одинаковую. Раньше её не задавали вовсе:
    # дочерний процесс печатал в своей, родитель декодировал в локальной, и на
    # Windows это совпадало только пока никто не трогал PYTHONIOENCODING. Стоило
    # выставить его в консоли перед прогоном (например, разбирая логи), как все
    # три теста падали на кириллице — при полностью исправном скрипте.
    return subprocess.run(
        [sys.executable, "reset_password.py", *args],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _base_env():
    env = os.environ.copy()
    env["DATABASE_URL"] = os.environ["DATABASE_URL"]
    env["SESSION_SECRET"] = os.environ.get("SESSION_SECRET", "x")
    env["SECRETS_ENCRYPTION_KEY"] = os.environ["SECRETS_ENCRYPTION_KEY"]
    # Не наследуем то, что оператор выставил в консоли: тест должен зависеть от
    # самого скрипта, а не от окружения, в котором его случайно запустили.
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def test_reset_rejects_short_password(web_db):
    result = _run_reset(_base_env(), "admin", "short")
    assert result.returncode == 1
    assert "слишком короткий" in result.stdout


def test_reset_creates_user_if_not_exists(web_db):
    result = _run_reset(_base_env(), "brandnew", "long-enough-password")
    assert result.returncode == 0
    assert "не существовал — создан" in result.stdout

    user = web_db.query(User).filter(User.username == "brandnew").first()
    assert user is not None
    assert verify_password("long-enough-password", user.password_hash)


def test_reset_changes_password_and_unlocks_existing_user(web_db):
    user = User(
        username="admin", password_hash=hash_password("old-password"),
        failed_login_attempts=10, locked_until=now_utc() + timedelta(minutes=30),
    )
    web_db.add(user)
    web_db.commit()

    result = _run_reset(_base_env(), "admin", "new-password-123")
    assert result.returncode == 0
    assert "обновлён" in result.stdout
    assert "Блокировка" in result.stdout

    web_db.refresh(user)
    assert verify_password("new-password-123", user.password_hash)
    assert not verify_password("old-password", user.password_hash)
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


def test_reset_on_unlocked_user_does_not_mention_lock(web_db):
    user = User(username="admin", password_hash=hash_password("old-password"))
    web_db.add(user)
    web_db.commit()

    result = _run_reset(_base_env(), "admin", "new-password-123")
    assert result.returncode == 0
    assert "Блокировка" not in result.stdout


def test_reset_writes_audit_log_entry(web_db):
    from app.models import AuditLog

    user = User(username="admin", password_hash=hash_password("old-password"))
    web_db.add(user)
    web_db.commit()

    _run_reset(_base_env(), "admin", "new-password-123")

    entry = web_db.query(AuditLog).filter(AuditLog.action == "password_reset_via_script").first()
    assert entry is not None
    assert entry.actor == "cli"
    assert "admin" in entry.details

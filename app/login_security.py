from datetime import datetime, timedelta
from app.timeutils import now_utc

from app.models import User
from app.audit import log_action

MAX_FAILED_ATTEMPTS = 10
LOCKOUT_MINUTES = 30


def is_locked(user: User) -> bool:
    return user.locked_until is not None and user.locked_until > now_utc()


def lockout_remaining_minutes(user: User) -> int:
    if not is_locked(user):
        return 0
    remaining = user.locked_until - now_utc()
    return max(1, int(remaining.total_seconds() // 60) + 1)  # округляем вверх, "1 минута" не 0


def record_login_failure(db, user: User) -> bool:
    """Увеличивает счётчик неудачных попыток; при достижении порога
    блокирует аккаунт на LOCKOUT_MINUTES. Возвращает True, если именно
    этот вызов поставил блокировку (для логирования/сообщения)."""
    user.failed_login_attempts += 1

    if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
        user.locked_until = now_utc() + timedelta(minutes=LOCKOUT_MINUTES)
        log_action(
            db, "system", "account_locked",
            f"{user.username}: {user.failed_login_attempts} неудачных попыток подряд, "
            f"блокировка на {LOCKOUT_MINUTES} мин.",
        )
        return True

    return False


def record_login_success(db, user: User):
    """Сбрасывает счётчик и снимает блокировку при успешном входе."""
    if user.failed_login_attempts > 0 or user.locked_until is not None:
        user.failed_login_attempts = 0
        user.locked_until = None

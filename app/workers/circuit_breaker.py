from datetime import datetime

from sqlalchemy.orm import Session

from app.models import PlatformAccount
from app.audit import log_action

FAILURE_THRESHOLD = 5  # подряд идущих сбоев до автоотключения кабинета


def record_success(db: Session, account: PlatformAccount):
    """Сбрасывает счётчик сбоев — кабинет снова считается здоровым."""
    if account.consecutive_failures > 0 or account.last_error is not None:
        account.consecutive_failures = 0
        account.last_error = None


def record_failure(db: Session, account: PlatformAccount, error_message: str) -> bool:
    """Увеличивает счётчик сбоев; при достижении порога сам отключает
    кабинет — раздел про стабильность работы. Возвращает True, если
    кабинет только что был автоматически отключён этим вызовом."""
    account.consecutive_failures += 1
    account.last_error = error_message[:2000] if error_message else None

    if account.consecutive_failures >= FAILURE_THRESHOLD and account.is_active:
        account.is_active = False
        log_action(
            db, "system", "account_auto_disabled",
            f"{account.name}: {account.consecutive_failures} сбоев подряд. Последняя ошибка: {error_message}",
        )
        return True

    return False

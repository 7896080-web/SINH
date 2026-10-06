from datetime import datetime

import requests
from sqlalchemy.orm import Session

from app.models import PlatformAccount
from app.audit import log_action

FAILURE_THRESHOLD = 5  # подряд идущих сбоев до автоотключения кабинета


def survives_a_retry(error: BaseException) -> bool:
    """Переживёт ли этот отказ повтор — то есть пройдёт ли он сам.

    Предохранитель заведён против ОДНОГО случая: сломанный, просроченный или
    отозванный ключ. Такой отказ повтор не переживёт никогда, долбить его
    бессмысленно, и кабинет надо гасить. Всё остальное — противоположность:
    площадка сейчас недоступна, через минуту ответит, а выключенный кабинет САМ
    НЕ ВКЛЮЧИТСЯ. Его per-account задания сняты, заказы не опрашиваются,
    остатки стоят — до прихода человека, и `/health` при этом зелёный:
    выключенные кабинеты он пропускает намеренно.

    06.10 на бою это стоило трёх кабинетов WB разом, ОДНОЙ СЕКУНДОЙ (02:29:04):
    WB перестал отвечать на `/api/v3/orders/new` (`Read timed out`), опрос упал
    пять циклов подряд — и защита от сломанных ключей выключила три кабинета с
    исправными ключами. Ночью, при зелёном мониторинге.

    Различаем по ТИПУ исключения и коду ответа, а не по тексту: текст меняется
    и с версией `requests`, и со стороной площадки, и правило, завязанное на
    него, молча перестало бы работать. 429 входит сюда обязательно — это лимит
    запросов, то есть почти всегда наша же пачка (у WB по спеке интервал 200 мс
    и «один 4XX считается как 10 запросов»), и гасить за него кабинет значит
    наказывать его за работу соседнего задания.
    """
    if isinstance(error, requests.HTTPError):
        response = getattr(error, "response", None)
        status = response.status_code if response is not None else None
        if status is None:
            return True            # кода нет — судить не о чем, не гасим
        return status == 429 or status >= 500
    # Таймаут чтения и соединения, обрыв, отказ DNS, недоступный хост — всё это
    # `RequestException`. Пройдёт само.
    return isinstance(error, requests.RequestException)


def record_success(db: Session, account: PlatformAccount):
    """Сбрасывает счётчик сбоев — кабинет снова считается здоровым."""
    if account.consecutive_failures > 0 or account.last_error is not None:
        account.consecutive_failures = 0
        account.last_error = None


def record_failure(db: Session, account: PlatformAccount, error_message: str,
                   transient: bool = False) -> bool:
    """Записывает сбой; при достижении порога сам отключает кабинет.

    Возвращает True, если кабинет только что был отключён этим вызовом.

    `transient=True` — отказ, который переживёт повтор (`survives_a_retry`):
    площадка не ответила, прислала 429 или 5xx. Такой сбой ЗАПИСЫВАЕТСЯ, но
    счётчик не растит и кабинет не гасит, и причина тут в несимметричной цене:
    недоступность площадки проходит сама, а выключенный кабинет сам не
    включится — заказы не опрашиваются и остатки стоят, пока не придёт человек.

    Текст при этом сохраняется ОБЯЗАТЕЛЬНО: его показывает «Диагностика» и
    печатает `scripts/probe_accounts.py`, а неуспешный heartbeat красит
    `/health` — у ЖИВОГО кабинета он не пропускается. То есть молчания не
    будет, просто кабинет останется работать и сам продолжит пробовать.
    """
    account.last_error = error_message[:2000] if error_message else None
    if transient:
        return False

    account.consecutive_failures += 1

    if account.consecutive_failures >= FAILURE_THRESHOLD and account.is_active:
        account.is_active = False
        log_action(
            db, "system", "account_auto_disabled",
            f"{account.name}: {account.consecutive_failures} сбоев подряд. Последняя ошибка: {error_message}",
        )
        return True

    return False

import time
import logging

import requests

logger = logging.getLogger("sync_worker.http_retry")

MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 2
# Потолок на паузу внутри одного вызова. Площадка вправе прислать
# `Retry-After: 3600`, и раньше это значение уходило в `sleep` как есть —
# ДВАЖДЫ за вызов, то есть два часа сна в потоке планировщика. Рассылка на это
# время просто не отправляет остатки, а снаружи всё выглядит работающим, пока
# не протухнет heartbeat.
#
# Повтор внутри вызова имеет смысл ровно пока он дешевле следующего цикла:
# рассылка ходит раз в 45 секунд и имеет собственный backoff на 1/2/5/15 минут.
# Всё, что дольше минуты, обязано возвращаться наверх ошибкой и ехать по нему.
MAX_SLEEP_SECONDS = 60


def with_retry(func, max_attempts: int = MAX_ATTEMPTS):
    """Вызывает func() с повтором при 429/5xx. Понимает как generic
    Retry-After, так и специфичный для WB X-Ratelimit-Retry (раздел 12.1
    спецификации — у WB это не то же самое, что стандартный Retry-After).
    4xx, отличные от 429, не ретраятся — это ошибка запроса, а не лимита,
    повтор её не исправит."""

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except requests.HTTPError as e:
            last_exc = e
            status = e.response.status_code if e.response is not None else None

            if status == 429:
                asked = _retry_after_seconds(e.response) or DEFAULT_BACKOFF_SECONDS * attempt
                wait = min(asked, MAX_SLEEP_SECONDS)
                if asked > wait:
                    logger.warning("площадка просит ждать %.0fс — это дольше цикла рассылки; "
                                   "ждём %.0fс и отдаём ошибку наверх, дальше сработает "
                                   "штатный повтор", asked, wait)
                logger.warning("429 от площадки, попытка %d/%d, ждём %.1fс", attempt, max_attempts, wait)
                if attempt < max_attempts:
                    time.sleep(wait)
                    continue
                raise
            elif status is not None and status >= 500:
                wait = DEFAULT_BACKOFF_SECONDS * attempt
                logger.warning("%d от площадки, попытка %d/%d, ждём %.1fс", status, attempt, max_attempts, wait)
                if attempt < max_attempts:
                    time.sleep(wait)
                    continue
                raise
            else:
                # 4xx (не 429) — ошибка запроса/авторизации, повторять бессмысленно
                raise
        except requests.RequestException as e:
            # Сетевая проблема (таймаут, обрыв) — тоже стоит попробовать снова
            last_exc = e
            if attempt < max_attempts:
                time.sleep(DEFAULT_BACKOFF_SECONDS * attempt)
                continue
            raise

    if last_exc:
        raise last_exc


def _retry_after_seconds(response) -> float | None:
    # WB — X-Ratelimit-Retry (раздел 12.1 спецификации)
    wb_retry = response.headers.get("X-Ratelimit-Retry")
    if wb_retry:
        try:
            return float(wb_retry)
        except ValueError:
            pass

    # Стандартный заголовок, который может отдавать Ozon/Kit
    generic_retry = response.headers.get("Retry-After")
    if generic_retry:
        try:
            return float(generic_retry)
        except ValueError:
            pass

    return None

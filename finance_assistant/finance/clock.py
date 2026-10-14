"""«Сегодня» в часовом поясе владельца.

Часовой пояс берётся из FINANCE_TZ (или TZ), например Europe/Moscow. На Linux
TZ понимает и сама система, а на Windows переменную TZ в таком виде система
не понимает (и путает время), поэтому там супервизор передаёт её боту как
FINANCE_TZ, а дату считаем сами через zoneinfo (база поясов — пакет tzdata).
"""

import logging
import os
from datetime import date, datetime

log = logging.getLogger(__name__)


def _zone():
    name = os.environ.get("FINANCE_TZ") or os.environ.get("TZ") or ""
    if not name or name.startswith(":"):
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # неизвестный пояс или нет базы поясов — берём системное время
        log.warning("Часовой пояс «%s» не найден — беру время сервера", name)
        return None


def local_today() -> date:
    zone = _zone()
    return datetime.now(zone).date() if zone else date.today()

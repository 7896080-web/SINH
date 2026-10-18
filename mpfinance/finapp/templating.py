"""Один шаблонизатор на всю программу — фильтры денег и времени, версия статики.

Ссылка на статику несёт версию (`/static/style.css?v=<метка>`): после наката
браузер иначе взял бы вчерашний стиль из кэша, и вёрстка разъехалась бы.
"""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from finapp.finance import charts, values as v

BASE = Path(__file__).resolve().parent
STATIC_DIR = BASE / "static"


def _static_version() -> str:
    marks = []
    for name in ("style.css", "progress.js"):
        try:
            marks.append(f"{(STATIC_DIR / name).stat().st_mtime:.0f}")
        except OSError:
            marks.append("0")
    mark = "-".join(marks)
    return hashlib.sha256(mark.encode()).hexdigest()[:10]


STATIC_VERSION = _static_version()


def static_url(name: str) -> str:
    return f"/static/{name}?v={STATIC_VERSION}"


def local(value, fmt: str = "%d.%m %H:%M") -> str:
    """Отметка времени (наивное UTC) — по МЕСТНОМУ времени сервера."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime(fmt)


ONEC_TASK_LABELS = {"pending": "в очереди", "sent": "ждём 1С", "done": "готово",
                    "failed": "ошибка", "timeout": "1С не ответила"}

NAV = [
    ("finance", "/finance", "Дашборд площадок"),
    ("finance-reports", "/finance/reports", "Отчёты площадок"),
    ("finance-statements", "/finance/statements", "Сверка со сводом WB"),
    ("finance-ue", "/finance/unit-economics", "Юнит-экономика"),
    ("finance-costs", "/finance/costs", "Себестоимость"),
    ("accounts", "/api-keys", "API-ключи"),
    ("diagnostics", "/diagnostics", "Диагностика"),
]

templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["static_url"] = static_url
templates.env.globals["nav_items"] = NAV
templates.env.filters["local"] = local
templates.env.filters["rub"] = v.format_rub
templates.env.filters["rub_short"] = v.format_rub_short
templates.env.filters["pct"] = v.format_percent
templates.env.filters["compact_rub"] = lambda kop: charts.compact_rub((kop or 0) / 100)

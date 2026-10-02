from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates

from priceapp.timeutils import ru

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.filters["ru"] = ru


def money(v, digits: int = 2) -> str:
    if v is None or v == "":
        return ""
    return f"{Decimal(str(v)):,.{digits}f}".replace(",", " ").replace(".", ",")


templates.env.filters["money"] = money


def plain(v) -> str:
    """Число для поля ввода без хвостовых нулей: 2.500 -> 2,5."""
    if v is None:
        return ""
    d = Decimal(str(v)).normalize()
    return f"{d:f}".replace(".", ",")


templates.env.filters["plain"] = plain


def local(v, fmt: str = "%d.%m %H:%M") -> str:
    """Отметка времени (UTC, наивная, или ISO-строка) — по МЕСТНОМУ времени. Оператор
    живёт по часам своего компьютера; «16:21 UTC» для него — загадка на три часа."""
    from datetime import datetime, timezone
    if not v:
        return ""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone().strftime(fmt)


templates.env.filters["local"] = local

NAV = [
    ("attention", "/attention", "Внимание"),
    ("sku_prices", "/sku-prices", "Цены товаров"),
    ("prices", "/prices?view=rules", "Правила и журнал"),
    ("mapping", "/mapping", "Сопоставление"),
    ("rate", "/rate", "Курс $"),
    ("accounts", "/api-keys", "API-ключи"),
    ("diagnostics", "/diagnostics", "Диагностика"),
    ("help", "/help/prices", "Справка"),
]
templates.env.globals["nav_items"] = NAV

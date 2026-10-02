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

NAV = [
    ("attention", "/attention", "Внимание"),
    ("prices", "/prices", "Цены"),
    ("sku_prices", "/sku-prices", "Цены товаров"),
    ("mapping", "/mapping", "Сопоставление"),
    ("rate", "/rate", "Курс $"),
    ("accounts", "/api-keys", "API-ключи"),
    ("diagnostics", "/diagnostics", "Диагностика"),
]
templates.env.globals["nav_items"] = NAV

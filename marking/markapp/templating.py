from pathlib import Path

from fastapi.templating import Jinja2Templates

from markapp.models import STATUS_LABELS, SupplyStatus
from markapp.timeutils import ru

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.filters["ru"] = ru
templates.env.globals["status_label"] = lambda s: STATUS_LABELS.get(SupplyStatus(s), s)


def money(v) -> str:
    if v is None:
        return ""
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


templates.env.filters["money"] = money

NAV = [
    ("supplies", "/supplies", "Поставки Lamoda"),
    ("catalog", "/catalog", "Одежда полный"),
    ("gtin", "/gtin", "Справочник GTIN"),
    ("labels", "/labels", "Этикетки"),
    ("organizations", "/organizations", "Организации"),
    ("diagnostics", "/diagnostics", "Диагностика"),
]
templates.env.globals["nav_items"] = NAV

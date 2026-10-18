"""Настройки финансового расчёта: налог, доп. расходы на единицу, склады 1С,
ручной курс доллара.

Хранятся общим механизмом настроек (`finapp/settings.py`); здесь — значения
по умолчанию и разбор в типы.
Неразборчивое значение не роняет страницы, а заменяется умолчанием: настройки
правит человек руками.
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from finapp import settings as store

TAX_MODES = {
    "revenue": "УСН «Доходы» — % от выручки",
    "profit": "УСН «Доходы − расходы» — % от прибыли до налога",
    "none": "Не учитывать налог",
}

# Склад продаж площадки в 1С. Для WB/Ozon/KIT — тот же склад, куда sync_admin
# при приёме заказа перемещает товар (`SOLD_WAREHOUSE_NAME` в его
# app/workers/scheduler.py на 18.10.2026). Склад Lamoda по умолчанию пустой: в
# боевой базе он заведён с опечаткой («Lamaoda_Склад», см. поставки в модуле
# 1С), и пусть точное имя впишет человек — пока он пустой, отчёт Lamoda в 1С не
# отправляется.
DEFAULT_WAREHOUSES = {
    "wb": "Wildberries_Склад_FBO",
    "ozon": "OZON_Склад",
    "kit": "Сайт AWER",
    "lamoda": "",
}

WAREHOUSE_KEYS = {p: f"FIN_WAREHOUSE_{p.upper()}" for p in DEFAULT_WAREHOUSES}
DEFAULTS = {
    "FIN_TAX_MODE": "revenue",
    "FIN_TAX_RATE": "6",
    "FIN_EXTRA_COST_PER_UNIT": "0",
    "FIN_USD_RATE_MANUAL": "",
    "FIN_AUTO_FETCH": "1",
    **{key: DEFAULT_WAREHOUSES[p] for p, key in WAREHOUSE_KEYS.items()},
}


@dataclass
class FinanceSettings:
    tax_mode: str = "revenue"
    tax_rate: Decimal = Decimal("6")             # проценты
    extra_cost_kop: int = 0                     # упаковка, маркировка и пр. на проданную единицу
    warehouses: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_WAREHOUSES))
    usd_rate_manual: Decimal | None = None      # ₽ за доллар, если курса ЦБ нет


def _decimal(text: str, default: str | None) -> Decimal | None:
    try:
        value = Decimal(str(text).replace(",", ".").replace(" ", "").strip())
    except InvalidOperation:
        return Decimal(default) if default is not None else None
    if not value.is_finite() or value < 0:
        return Decimal(default) if default is not None else None
    return value


def raw_settings(db: Session) -> dict[str, str]:
    """Как есть — для формы редактирования."""
    return {key: store.get(db, key, default) for key, default in DEFAULTS.items()}


def load_settings(db: Session) -> FinanceSettings:
    raw = raw_settings(db)
    mode = raw["FIN_TAX_MODE"] if raw["FIN_TAX_MODE"] in TAX_MODES else DEFAULTS["FIN_TAX_MODE"]
    extra = _decimal(raw["FIN_EXTRA_COST_PER_UNIT"], "0")
    manual = _decimal(raw["FIN_USD_RATE_MANUAL"], None) if raw["FIN_USD_RATE_MANUAL"] else None
    return FinanceSettings(
        tax_mode=mode,
        tax_rate=_decimal(raw["FIN_TAX_RATE"], DEFAULTS["FIN_TAX_RATE"]),
        extra_cost_kop=int((extra * 100).to_integral_value()),
        warehouses={p: raw[key].strip() for p, key in WAREHOUSE_KEYS.items()},
        usd_rate_manual=manual if manual and manual > 0 else None,
    )


def save_setting(db: Session, key: str, value: str) -> bool:
    """Записать настройку (без коммита). True — значение изменилось."""
    if key not in DEFAULTS:
        raise KeyError(key)
    return store.put(db, key, value)

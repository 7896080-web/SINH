"""Денежные поля Lamoda: целые числа в МИНОРНЫХ единицах (копейки RUB/BYN, тиыны KZT),
в v2 — объекты `{"amount": 199900, "currency": "RUB"}`. Делим на 100 ровно здесь, на
границе приёма; дальше в коде и в БД (NUMERIC(x,2)) — только рубли.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_INT_RE = re.compile(r"^-?\d+$")


class MoneyFormatError(ValueError):
    pass


def money_from_api(value) -> Decimal | None:
    """`{"amount": 199900, "currency": "RUB"}` → Decimal('1999.00'); голое целое — тоже копейки.

    Дробное значение — не формат v2 (там только целые), это значит, что поле пришло уже
    в рублях или схема поменялась: молча поделить на 100 значило бы занизить цену в 100 раз,
    поэтому — ошибка, а не догадка."""
    if value is None:
        return None
    if isinstance(value, dict):
        amount = value.get("amount")
        currency = value.get("currency")
        if currency not in (None, "RUB"):
            logger.warning("Lamoda: сумма не в рублях (%s) — сохранена как есть, конвертации валют нет", currency)
    else:
        amount = value
    if amount is None:
        return None
    if isinstance(amount, bool):
        raise MoneyFormatError(f"денежное поле — не число: {value!r}")
    if isinstance(amount, int):
        minor = amount
    elif isinstance(amount, float) and amount.is_integer():
        minor = int(amount)
    elif isinstance(amount, str) and _INT_RE.match(amount.strip()):
        minor = int(amount.strip())
    else:
        raise MoneyFormatError(f"денежное поле не в минорных единицах: {value!r}")
    return (Decimal(minor) / 100).quantize(_CENT)


def money_to_api(rubles: Decimal | float | int, currency: str = "RUB") -> dict:
    """Обратное преобразование — для будущих методов записи цен (Фаза 2)."""
    minor = (Decimal(str(rubles)) * 100).quantize(Decimal("1"))
    return {"amount": int(minor), "currency": currency}

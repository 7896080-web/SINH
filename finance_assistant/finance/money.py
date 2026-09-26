"""Деньги храним в копейках (int), чтобы не ловить ошибки округления float."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def parse_amount(text: str) -> int:
    """'1 234,50' / '1234.5' / '-10' -> копейки. Бросает ValueError на мусоре."""
    cleaned = str(text).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not cleaned:
        raise ValueError("пустая сумма")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"не похоже на сумму: {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"не похоже на сумму: {text!r}")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_amount(kopecks: int) -> str:
    """12345678 -> '123 456,78'."""
    sign = "-" if kopecks < 0 else ""
    rub, kop = divmod(abs(kopecks), 100)
    return f"{sign}{rub:,}".replace(",", " ") + f",{kop:02d}"

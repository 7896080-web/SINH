"""Деньги храним в копейках (int), чтобы не ловить ошибки округления float."""

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Потолок разумной суммы: 100 млрд ₽. Больше — почти наверняка ошибка
# распознавания, а огромные числа не влезли бы в целое SQLite.
MAX_KOPECKS = 100_000_000_000 * 100

# Все виды пробелов и разделителей разрядов, которые встречаются в банковских
# приложениях: обычный, неразрывный, узкий неразрывный, тонкий, апостроф.
_SPACES = re.compile(r"[\s   '’]")


def parse_amount(text: str) -> int:
    """'1 234,50' / '1234.5' / '1.234,56' / '1,234.56' / '−110' -> копейки.

    Бросает ValueError на мусоре, экспоненте и нереальных суммах.
    """
    cleaned = _SPACES.sub("", str(text)).replace("−", "-").replace("–", "-")
    cleaned = cleaned.replace("₽", "").replace("руб.", "").replace("руб", "").replace("р.", "")
    if not cleaned:
        raise ValueError("пустая сумма")
    if not re.fullmatch(r"[-+]?[\d.,]+", cleaned) or not re.search(r"\d", cleaned):
        raise ValueError(f"не похоже на сумму: {text!r}")
    sign = -1 if cleaned.startswith("-") else 1
    digits = cleaned.lstrip("+-")
    if "," in digits and "." in digits:
        # Десятичный разделитель — тот, что правее: «1.234,56» и «1,234.56».
        decimal_sep = "," if digits.rfind(",") > digits.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        digits = digits.replace(thousands, "").replace(decimal_sep, ".")
    else:
        sep = "," if "," in digits else "."
        parts = digits.split(sep)
        if len(parts) > 2:
            # «1.234.567» — только разряды; «1,2,3» — мусор.
            if all(len(p) == 3 for p in parts[1:]):
                digits = "".join(parts)
            else:
                raise ValueError(f"не похоже на сумму: {text!r}")
        else:
            digits = digits.replace(",", ".")
    try:
        value = Decimal(digits)
    except InvalidOperation:
        raise ValueError(f"не похоже на сумму: {text!r}") from None
    if value.adjusted() > 12:  # больше триллиона — до округления, пока точности хватает
        raise ValueError(f"слишком большая сумма: {text!r}")
    kopecks = sign * int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if abs(kopecks) > MAX_KOPECKS:
        raise ValueError(f"слишком большая сумма: {text!r}")
    return kopecks


def format_amount(kopecks: int) -> str:
    """12345678 -> '123 456,78'."""
    sign = "-" if kopecks < 0 else ""
    rub, kop = divmod(abs(kopecks), 100)
    return f"{sign}{rub:,}".replace(",", " ") + f",{kop:02d}"

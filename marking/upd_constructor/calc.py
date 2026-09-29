"""Денежная арифметика на Decimal. Округление half-up до копеек."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def to_dec(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v).replace(",", ".").replace(" ", ""))


@dataclass
class Line:
    name: str
    gtin: str
    kiz: str
    qty: Decimal
    unit_bez: Decimal   # ЦенаТов
    bez: Decimal        # СтТовБезНДС
    nal: Decimal        # СумНал
    uch: Decimal        # СтТовУчНал


def compute_line(name, gtin, kiz, qty, price, rate_pct: Decimal, price_mode: str = "gross") -> Line:
    """price_mode='gross': price в файле — цена С налогом (так ведут себя выгрузки Lamoda, см. CLAUDE.md).
    price_mode='net': price — цена без НДС."""
    qty = to_dec(qty)
    price = to_dec(price)
    rate = rate_pct / Decimal(100)
    if price_mode == "gross":
        uch = q2(price * qty)
        bez = q2(uch / (1 + rate))
        nal = q2(uch - bez)
    elif price_mode == "net":
        bez = q2(price * qty)
        nal = q2(bez * rate)
        uch = q2(bez + nal)
    else:
        raise ValueError(price_mode)
    return Line(name, str(gtin), kiz, qty, q2(bez / qty), bez, nal, uch)


@dataclass
class Totals:
    qty: Decimal
    bez: Decimal
    nal: Decimal
    uch: Decimal


def compute_totals(lines, rate_pct: Decimal, mode: str = "rows", price_mode: str = "gross") -> Totals:
    """mode='rows'      — итоги = суммы округлённых строк (шапка == сумма строк).
       mode='reference' — как в принятых эталонах: uch = сумма строк, bez = round(uch/(1+ставка)), nal = uch-bez.
                          В эталонах шапка НЕ равна сумме строк (дрейф до ~1 руб. на 300-500 строк)."""
    qty = sum((l.qty for l in lines), Decimal(0))
    uch = sum((l.uch for l in lines), Decimal(0))
    if mode == "reference" and price_mode == "gross":
        bez = q2(uch / (1 + rate_pct / Decimal(100)))
        nal = uch - bez
    else:
        bez = sum((l.bez for l in lines), Decimal(0))
        nal = sum((l.nal for l in lines), Decimal(0))
    return Totals(qty, bez, nal, uch)

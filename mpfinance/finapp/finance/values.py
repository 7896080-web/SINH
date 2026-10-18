"""Разбор значений из ячеек отчётов и описание единой формы строки.

Отчёты площадок приходят из Excel и CSV, поэтому одна и та же сумма бывает
числом (1234.5), строкой с запятой ("1 234,50"), с неразрывным пробелом в
разрядах, со знаком рубля или с юникодным минусом. Всё это сводится к целым
КОПЕЙКАМ: суммы сверяются с выплатой площадки и с документами 1С до копейки.

Нераспознанное значение в денежной колонке — ошибка, а не ноль: тихий ноль
вместо суммы означал бы неверную выручку и прибыль, которых никто не заметит.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class ValueParseError(ValueError):
    """Значение ячейки не разобралось как число/дата."""


# ---------------------------------------------------------------- операции

SALE = "sale"
RETURN = "return"
LOGISTICS = "logistics"
STORAGE = "storage"
PENALTY = "penalty"
ACCEPTANCE = "acceptance"
DEDUCTION = "deduction"
COMPENSATION = "compensation"
OTHER = "other"

OPERATION_LABELS = {
    SALE: "Продажа",
    RETURN: "Возврат",
    LOGISTICS: "Логистика",
    STORAGE: "Хранение",
    PENALTY: "Штраф",
    ACCEPTANCE: "Приёмка",
    DEDUCTION: "Удержание",
    COMPENSATION: "Компенсация",
    OTHER: "Прочее",
}
# Продажи и возвраты двигают товар: только они попадают в документ 1С и в
# количество проданного; остальные операции — деньги без товара.
GOODS_OPERATIONS = (SALE, RETURN)

PLATFORM_LABELS = {
    "wb": "Wildberries",
    "ozon": "Ozon",
    "kit": "Яндекс KIT",
    "lamoda": "Lamoda",
}
REPORT_PLATFORMS = ("wb", "lamoda", "kit", "ozon")

# Денежные поля строки единой формы (в базе — с суффиксом _kop).
MONEY_FIELDS = ("revenue", "commission", "payout", "logistics", "storage", "penalty",
                "acceptance", "deduction", "compensation")
# Расходы, вычитаемые из «к перечислению за товар».
EXPENSE_FIELDS = ("logistics", "storage", "penalty", "acceptance", "deduction")


@dataclass(frozen=True)
class Column:
    field: str
    header: str


# Единая форма — одна и та же для экспорта отчёта и для шаблона импорта
# (отчёт любой площадки, которую мы не умеем читать, оператор может привести к
# этой форме сам и загрузить).
UNIFIED_COLUMNS = (
    Column("operation", "Тип операции"),
    Column("source_operation", "Операция площадки"),
    Column("operation_date", "Дата операции"),
    Column("order_date", "Дата заказа"),
    Column("order_id", "Номер заказа"),
    Column("srid", "Идентификатор строки площадки"),
    Column("barcode", "Баркод"),
    Column("article", "Артикул"),
    Column("platform_sku", "Код товара площадки"),
    Column("name", "Наименование"),
    Column("size", "Размер"),
    Column("quantity", "Кол-во"),
    Column("revenue", "Выручка"),
    Column("commission", "Комиссия площадки"),
    Column("payout", "К перечислению за товар"),
    Column("logistics", "Логистика"),
    Column("storage", "Хранение"),
    Column("penalty", "Штрафы"),
    Column("acceptance", "Приёмка"),
    Column("deduction", "Прочие удержания"),
    Column("compensation", "Компенсации и доплаты"),
)


def empty_line() -> dict:
    """Строка единой формы с нулями — разборщики заполняют только своё."""
    line = {
        "operation": OTHER, "source_operation": "", "operation_date": None,
        "order_date": None, "order_id": "", "srid": "", "barcode": "", "article": "",
        "platform_sku": "", "name": "", "size": "", "quantity": 0,
    }
    for field in MONEY_FIELDS:
        line[field] = 0
    return line


def line_total(line: dict) -> int:
    """Итог строки к перечислению, копейки."""
    return (line["payout"] - sum(line[f] for f in EXPENSE_FIELDS) + line["compensation"])


# ---------------------------------------------------------------- разбор

MAX_AMOUNT_RUB = Decimal(10) ** 12
_JUNK = ("\u00a0", "\u202f", "\u2009", " ", "₽", "руб.", "руб", "р.")


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in ("", "-", "—"))


def parse_money(value) -> int | None:
    """Сумма в копейках. Пустая ячейка → None (колонка есть, значения нет)."""
    if _is_blank(value):
        return None
    if isinstance(value, bool):
        raise ValueParseError(f"«{value}» — не сумма")
    if isinstance(value, (int, Decimal)):
        amount = Decimal(value)
    elif isinstance(value, float):
        # str(), а не Decimal(float): Decimal(0.1) дал бы 0.1000000000000000055…
        amount = Decimal(str(value))
    else:
        text = str(value).strip().lower()
        for junk in _JUNK:
            text = text.replace(junk, "")
        text = text.replace("−", "-").replace("–", "-")
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        if "," in text and "." in text:
            # Оба разделителя: десятичный — тот, что правее (1,234.56 и 1.234,56).
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "")
            else:
                text = text.replace(",", "")
        text = text.replace(",", ".")
        try:
            amount = Decimal(text)
        except InvalidOperation:
            raise ValueParseError(f"«{value}» — не сумма") from None
        if negative:
            amount = -amount
    # Сумма больше триллиона рублей в отчёте площадки — это не деньги, а
    # прочитанная не та колонка (номер заказа, штрихкод).
    if not amount.is_finite() or abs(amount) >= MAX_AMOUNT_RUB:
        raise ValueParseError(f"«{value}» — не сумма")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_quantity(value) -> int | None:
    if _is_blank(value):
        return None
    if isinstance(value, bool):
        raise ValueParseError(f"«{value}» — не количество")
    if isinstance(value, int):
        return value
    try:
        amount = Decimal(str(value).strip().replace("\u00a0", "").replace(" ", "")
                         .replace(",", ".").replace("−", "-"))
    except InvalidOperation:
        raise ValueParseError(f"«{value}» — не количество") from None
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise ValueParseError(f"«{value}» — не целое количество")
    return int(amount)


_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                 "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%dT%H:%M:%S.%f", "%d/%m/%Y")
_EXCEL_EPOCH = date(1899, 12, 30)


def parse_date(value) -> date | None:
    if _is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Дата, которую Excel отдал серийным числом (ячейка без формата даты).
        if 20000 < value < 80000:
            return _EXCEL_EPOCH + timedelta(days=int(value))
        raise ValueParseError(f"«{value}» — не дата")
    text = str(value).strip()
    # Пояс и доли секунды площадки пишут по-разному (Z, +03:00, .123456789) —
    # для отчёта важна только дата.
    if "T" in text and len(text) > 19:
        text = text[:19]
    elif len(text) > 19 and text[4:5] == "-":
        text = text[:19]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueParseError(f"«{value}» — не дата")


def parse_text(value) -> str:
    """Текст ячейки. Баркод и номер заказа Excel часто хранит числом —
    2000000000012.0 должен стать «2000000000012», а не «2000000000012.0»."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    return str(value).strip()


# ---------------------------------------------------------------- вывод

def kop_to_rub(kop: int | None) -> Decimal | None:
    """Копейки → рубли (Decimal) — для Excel и строк 1С."""
    if kop is None:
        return None
    return (Decimal(kop) / 100).quantize(Decimal("0.01"))


def format_rub(kop: int | None, signed: bool = False) -> str:
    """1234567 → «12 345,67». Для интерфейса; в файлы — kop_to_rub."""
    if kop is None:
        return "—"
    sign = "−" if kop < 0 else ("+" if signed and kop > 0 else "")
    rub, cents = divmod(abs(int(kop)), 100)
    return f"{sign}{rub:,}".replace(",", "\u00a0") + f",{cents:02d}"


def format_rub_short(kop: int | None) -> str:
    """Без копеек — для плиток дашборда, где важен порядок, а не копейка."""
    if kop is None:
        return "—"
    rub = int((Decimal(kop) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    sign = "−" if rub < 0 else ""
    return f"{sign}{abs(rub):,}".replace(",", "\u00a0") + "\u00a0₽"


def format_percent(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}".replace(".", ",").replace("-", "−") + "\u00a0%"

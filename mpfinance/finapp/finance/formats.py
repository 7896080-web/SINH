"""Разборщики отчётов площадок → строки единой формы (`values.empty_line`).

Каждый формат — это словарь «поле единой формы → возможные заголовки колонки»
плюс функция, превращающая одну строку отчёта в строку единой формы. Колонки
ищутся по заголовкам, а не по позиции: площадки регулярно добавляют колонки в
середину отчёта, и разбор по номеру колонки молча читал бы не те суммы.

Насколько формату можно доверять, написано в его `note` и показывается на
странице загрузки. Если отчёт не опознан, оператор видит, какие колонки мы
нашли в файле, и может привести отчёт к единой форме сам (шаблон на странице).
"""
import re
from decimal import ROUND_HALF_UP, Decimal
from dataclasses import dataclass, field
from typing import Callable

from finapp.finance import values as v
from finapp import progress
from finapp.finance.readers import find_header, header_preview, normalize_header

# Сколько ошибок разбора показываем оператору (остальные — числом).
MAX_ERRORS_SHOWN = 15


class Row:
    """Строка отчёта с доступом к ячейкам по полю единой формы."""

    def __init__(self, cells: list, mapping: dict[str, int], headers: dict[str, str]):
        self.cells = cells
        self.mapping = mapping
        self.headers = headers

    def has(self, name: str) -> bool:
        return name in self.mapping

    def raw(self, name: str):
        idx = self.mapping.get(name)
        if idx is None or idx >= len(self.cells):
            return None
        return self.cells[idx]

    def _parse(self, name: str, parser):
        try:
            return parser(self.raw(name))
        except v.ValueParseError as e:
            raise v.ValueParseError(f"колонка «{self.headers.get(name, name)}»: {e}") from None

    def text(self, name: str) -> str:
        return v.parse_text(self.raw(name))

    def money(self, name: str) -> int:
        return self._parse(name, v.parse_money) or 0

    def money_or_none(self, name: str) -> int | None:
        return self._parse(name, v.parse_money)

    def quantity(self, name: str) -> int | None:
        return self._parse(name, v.parse_quantity)

    def date(self, name: str):
        return self._parse(name, v.parse_date)


class SkipRow(Exception):
    """Строка отчёта сознательно не учитывается (отменённый заказ и т.п.) —
    причина попадает в предупреждения импорта."""


@dataclass
class ReportFormat:
    code: str
    label: str
    platforms: tuple[str, ...]
    aliases: dict[str, tuple[str, ...]]
    build: Callable[[Row], dict]
    required: tuple[str, ...] = ()
    required_any: tuple[tuple[str, ...], ...] = ()
    min_fields: int = 3
    note: str = ""
    # Чей итог стоит в строке «Итого» отчёта: «payout» — к перечислению (Lamoda),
    # «revenue» — продажи до удержаний (AWER CRM).
    total_field: str = "payout"


_PERIOD_RE = re.compile(r"\bс\s*(\d{2}\.\d{2}\.\d{4})\s*по\s*(\d{2}\.\d{2}\.\d{4})", re.IGNORECASE)


@dataclass
class ParseResult:
    format: ReportFormat | None = None
    sheet: str = ""
    lines: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    errors_total: int = 0
    warnings: list[str] = field(default_factory=list)
    report_numbers: set[str] = field(default_factory=set)
    period: tuple | None = None          # (с, по), если площадка указала его в строках или шапке
    # «К перечислению» из строки «Итого» самого отчёта (Lamoda и др.) — подставляется
    # как сумма площадки для сверки с итогом строк.
    declared_payout: int | None = None
    # Итог продаж из строки «Итого», когда там выручка, а не выплата.
    declared_revenue: int | None = None


def _norm(text: str) -> str:
    return normalize_header(text)


# ======================================================================
# Wildberries — «Детализация еженедельного отчёта реализации» (.xlsx из ЛК).
#
# Заголовки сверены со справкой WB «Детализация еженедельного отчёта
# реализации» (ред. 07.10.2026) и схемой finance-api `SalesReportsDetailedRes`.
# Английские имена — поля API (старый reportDetailByPeriod и новый
# /api/finance/v1/sales-reports/detailed): выгрузка по API, когда она появится,
# ляжет в этот же разбор. Итог строки повторяет формулу «Итого к оплате» из
# справки WB «Еженедельные отчёты реализации» (ред. 02.09.2026).
# ======================================================================

WB_ALIASES = {
    "report_number": ("№ отчета", "Номер отчета", "realizationreport_id", "reportId"),
    "date_from": ("Дата начала отчетного периода", "date_from", "dateFrom"),
    "date_to": ("Дата конца отчетного периода", "date_to", "dateTo"),
    "doc_type": ("Тип документа", "doc_type_name", "docTypeName"),
    "oper": ("Обоснование для оплаты", "supplier_oper_name", "sellerOperName"),
    "order_date": ("Дата заказа покупателем", "order_dt", "orderDt"),
    "sale_date": ("Дата продажи", "sale_dt", "saleDt"),
    "rr_date": ("Дата операции", "rr_dt", "rrDate"),
    "quantity": ("Кол-во", "Количество", "quantity"),
    "retail_amount": ("Вайлдберриз реализовал Товар (Пр)", "retail_amount", "retailAmount"),
    "for_pay": ("К перечислению Продавцу за реализованный Товар", "ppvz_for_pay", "forPay"),
    "delivery": ("Услуги по доставке товара покупателю", "delivery_rub", "deliveryService"),
    "penalty": ("Общая сумма штрафов", "penalty"),
    # До 2026 колонка называлась «Доплаты» и была выплатой продавцу. Теперь это
    # «Корректировка ВВ» с правилом WB «удержание без знака, выплата продавцу
    # со знаком −» — то есть ВЫЧИТАЕТСЯ. Поэтому два разных поля.
    "additional_old": ("Доплаты",),
    "wb_correction": ("Корректировка Вознаграждения Вайлдберриз (ВВ)", "additional_payment",
                      "additionalPayment"),
    "storage": ("Хранение", "storage_fee", "paidStorage"),
    "deduction": ("Удержания", "deduction"),
    "acceptance": ("Услуга по обработке товара", "Платная приемка", "Операции на приемке",
                   "acceptance", "paidAcceptance"),
    "loyalty_fee": ("Стоимость участия в программе лояльности", "cashback_commission_change",
                    "cashbackCommissionChange"),
    "loyalty_points": ("Сумма, удержанная за начисленные баллы программы лояльности",
                       "cashback_amount", "cashbackAmount"),
    "loyalty_comp": ("Компенсация скидки по программе лояльности", "cashback_discount",
                     "cashbackDiscount"),
    "payment_schedule": ("Разовое изменение срока перечисления денежных средств",
                         "payment_schedule", "paymentSchedule"),
    "barcode": ("Баркод", "barcode", "sku"),
    "article": ("Артикул поставщика", "Артикул продавца", "sa_name", "vendorCode"),
    "nm_id": ("Код номенклатуры", "nm_id", "nmId"),
    "name": ("Название", "title", "Предмет", "subject_name", "subjectName"),
    "size": ("Размер", "ts_name", "techSize"),
    "assembly_id": ("Номер сборочного задания", "assembly_id", "orderId"),
    "srid": ("Srid", "srid"),
}
# «Возмещение издержек по перевозке/по складским операциям с товаром»
# (rebill_logistic_cost; в файле с 2026 — «Возмещение издержек по перемещению и
# операционной обработке товара») НЕ разбирается намеренно: по справке WB эти
# строки «не влияют на доход продавца», и вычитание дало бы итог меньше выплаты
# WB. На настоящем отчёте №848966494 (14–20.09.2026): у таких строк «К
# перечислению» и «Услуги по доставке» нулевые, сумма только в этой колонке.

_WB_SALE_OPS = {"продажа", "корректная продажа", "сторно возвратов", "коррекция продаж"}
_WB_RETURN_OPS = {"возврат", "корректный возврат", "сторно продаж", "коррекция возвратов",
                  "коррекция возврата"}


def classify_wb(oper: str, doc_type: str) -> str:
    oper_n = _norm(oper)
    doc_n = _norm(doc_type)
    if oper_n in _WB_SALE_OPS or oper_n in _WB_RETURN_OPS:
        # Направление движения товара определяет «Тип документа», а не
        # название операции: «Сторно продаж» — это документ возврата.
        if doc_n.startswith("возврат"):
            return v.RETURN
        if doc_n.startswith("продажа"):
            return v.SALE
        return v.RETURN if oper_n in _WB_RETURN_OPS else v.SALE
    # Порядок проверок важен: «Компенсация скидки по программе лояльности» —
    # доход, а «Стоимость участия в программе лояльности» — удержание.
    # «Возмещение издержек по перемещению и операционной обработке товара» —
    # перевозка, а не приёмка, хоть в названии и есть «обработк». Сумма у этих
    # строк в колонке, которую мы намеренно не читаем (см. выше): на доход
    # продавца они не влияют.
    if ("логистик" in oper_n or "доставк" in oper_n or "перевозк" in oper_n
            or "перемещени" in oper_n or "выдачу и возврат" in oper_n):
        return v.LOGISTICS
    if "хранени" in oper_n:
        return v.STORAGE
    if "штраф" in oper_n:
        return v.PENALTY
    if "приемк" in oper_n or "обработк" in oper_n:
        return v.ACCEPTANCE
    if any(w in oper_n for w in ("компенсац", "доплат", "авансов", "брак", "потерянн",
                                 "инвентаризац")):
        return v.COMPENSATION
    if "удержан" in oper_n or "лояльност" in oper_n or "срока перечисления" in oper_n:
        return v.DEDUCTION
    return v.OTHER


def build_wb(row: Row) -> dict:
    line = v.empty_line()
    oper = row.text("oper")
    doc_type = row.text("doc_type")
    line["source_operation"] = oper or doc_type
    line["operation"] = classify_wb(oper, doc_type)

    # Суммы продаж и возвратов в детализации WB положительные — направление
    # задаёт «Тип документа» (справка WB: Σ[Продажа] − Σ[Возврат]).
    sign = -1 if _norm(doc_type).startswith("возврат") else 1
    for_pay = sign * row.money("for_pay")
    line["quantity"] = abs(row.quantity("quantity") or 0)

    if line["operation"] in v.GOODS_OPERATIONS:
        line["revenue"] = sign * row.money("retail_amount")
        line["payout"] = for_pay
        # Всё, что площадка взяла с продажи (вознаграждение, платёжные услуги,
        # ПВЗ), — разница между реализацией и выплатой за товар. При скидках
        # WB бывает отрицательной: площадка доплачивает скидку из своей комиссии.
        line["commission"] = line["revenue"] - line["payout"]
    else:
        # Выплата без продажи (компенсация ущерба, оплата брака): документ
        # «Продажа» — платят продавцу, «Возврат» — забирают обратно.
        line["compensation"] += for_pay

    line["logistics"] = row.money("delivery")
    line["storage"] = row.money("storage")
    line["penalty"] = row.money("penalty")
    line["acceptance"] = row.money("acceptance")
    line["deduction"] = (row.money("deduction") + row.money("wb_correction")
                         + row.money("payment_schedule")
                         + sign * (row.money("loyalty_fee") + row.money("loyalty_points")))
    line["compensation"] += row.money("loyalty_comp") + row.money("additional_old")

    line["operation_date"] = row.date("sale_date") or row.date("rr_date")
    line["order_date"] = row.date("order_date")
    assembly = row.text("assembly_id")
    # Номер сборочного задания — это id заказа FBS, под которым его принял наш
    # опрос заказов; «0» у FBO-продаж означает «задания не было».
    line["order_id"] = assembly if assembly not in ("", "0") else ""
    line["srid"] = row.text("srid")
    line["barcode"] = row.text("barcode")
    line["article"] = row.text("article")
    line["platform_sku"] = row.text("nm_id") if row.text("nm_id") != "0" else ""
    line["name"] = row.text("name")
    line["size"] = row.text("size") if row.text("size") != "0" else ""
    return line


WB_FORMAT = ReportFormat(
    code="wb_detail",
    label="Wildberries — детализация еженедельного отчёта реализации",
    platforms=("wb",),
    aliases=WB_ALIASES,
    build=build_wb,
    required=("oper", "for_pay", "retail_amount"),
    note="Колонки сверены со справкой WB (октябрь 2026) и схемой finance-api; итог строки — "
         "по формуле «Итого к оплате» WB. Сверьте первый отчёт с суммой в ЛК WB.",
)


# ======================================================================
# Общий разбор для Lamoda и Яндекс KIT. Точная раскладка их отчётов на живых
# файлах не проверена, поэтому заголовки собраны с запасом синонимов, а суммы
# расходов берутся по модулю (одни отчёты пишут удержания со знаком минус,
# другие — положительными в колонке «Удержания»).
# ======================================================================

_GENERIC_COMMON = {
    "operation": ("Тип операции", "Операция", "Тип транзакции", "Тип документа", "Вид операции",
                  "Тип начисления", "Статус", "Статус заказа", "Статус товара", "Статус позиции",
                  "status"),
    "operation_date": ("Дата операции", "Дата продажи", "Дата реализации", "Дата транзакции",
                       "Дата выкупа", "Дата доставки", "Дата начисления", "Дата", "Дата документа"),
    "order_date": ("Дата заказа", "Дата создания заказа", "Дата оформления", "created_at"),
    "order_id": ("Номер заказа", "№ заказа", "Заказ", "ID заказа", "Номер отправления",
                 "Order number", "order_id", "order_nr"),
    "barcode": ("Баркод", "Штрихкод", "Штрих-код", "Штрих код", "EAN", "Barcode", "barcode"),
    "article": ("Артикул продавца", "Артикул партнера", "Артикул партнёра", "Артикул поставщика",
                "Артикул", "Артикул товара", "SKU продавца", "Seller SKU", "offer_id",
                "Код товара продавца"),
    "platform_sku": ("SKU", "SKU Lamoda", "Lamoda SKU", "Артикул Lamoda", "ID варианта",
                     "ID товара", "variant_id", "product_variant_id"),
    "name": ("Наименование", "Наименование товара", "Название товара", "Товар", "Название",
             "Номенклатура"),
    "size": ("Размер", "Размер товара"),
    "quantity": ("Количество", "Кол-во", "Кол-во, шт", "Количество, шт", "Количество товаров",
                 "quantity", "count"),
    "price": ("Цена", "Цена продажи", "Цена реализации", "Цена за единицу", "Цена за шт",
              "Цена со скидкой", "price"),
    "revenue": ("Сумма продажи", "Сумма реализации", "Сумма продаж", "Выручка", "Стоимость",
                "Стоимость товаров", "Сумма", "Сумма заказа", "Сумма по позиции",
                "Реализовано на сумму", "total"),
    "commission": ("Комиссия", "Комиссия площадки", "Комиссия Lamoda", "Комиссия KIT",
                   "Вознаграждение комиссионера", "Вознаграждение Lamoda",
                   "Комиссионное вознаграждение", "Вознаграждение", "commission"),
    "acquiring": ("Эквайринг", "Комиссия за эквайринг", "Комиссия эквайринга",
                  "Оплата услуг эквайринга", "acquiring"),
    "logistics": ("Логистика", "Доставка", "Стоимость доставки", "Услуги доставки",
                  "Услуги логистики", "Обратная логистика", "delivery"),
    "storage": ("Хранение", "Услуги хранения"),
    "penalty": ("Штраф", "Штрафы"),
    "acceptance": ("Приемка", "Приёмка", "Платная приемка"),
    "deduction": ("Удержания", "Прочие удержания", "Удержание", "Прочие услуги"),
    "compensation": ("Компенсация", "Компенсации", "Доплаты", "Бонусы"),
    "payout": ("К выплате", "К перечислению", "Сумма к перечислению", "Сумма к выплате",
               "Итого к выплате", "Итого к перечислению", "К перечислению продавцу",
               "Перечислено", "payout"),
}

# Статусы/операции: что считать продажей, возвратом, а что не учитывать.
_SALE_WORDS = ("продаж", "реализац", "выкуп", "доставлен", "получен", "выполнен", "завершен",
               "delivered", "completed", "sale", "sold")
_RETURN_WORDS = ("возврат", "refund", "return")
_PARTIAL_WORDS = ("частичн", "partial")
_SKIP_WORDS = ("отмен", "cancel", "не выкуп", "невыкуп", "ожида", "wait", "setup_delivery",
               "creating", "в доставке", "в пути", "собира", "processing")


def classify_generic(text: str, status_column: bool = False) -> str | None:
    """Операция по слову из колонки «Тип операции»/«Статус». None — строку не
    учитываем (отмена, ещё не доставлен). Пустая колонка → продажа: отчёт без
    колонки статуса — это отчёт о продажах.

    `status_column` — колонка хранит статус ЗАКАЗА: тогда всё, что не продажа
    и не возврат («Новый», «Оплачен», «Заморожен»), пропускается, а не идёт в
    «Прочее» — иначе неоплаченный заказ стал бы доходом."""
    t = _norm(text)
    if not t:
        return v.SALE
    if any(w in t for w in _RETURN_WORDS):
        return v.RETURN
    if any(w in t for w in _SKIP_WORDS):
        return None
    if any(w in t for w in _SALE_WORDS):
        return v.SALE
    if status_column:
        return None
    if "логистик" in t or "доставк" in t:
        return v.LOGISTICS
    if "хранени" in t:
        return v.STORAGE
    if "штраф" in t:
        return v.PENALTY
    if "приемк" in t:
        return v.ACCEPTANCE
    if "компенсац" in t or "доплат" in t or "бонус" in t:
        return v.COMPENSATION
    if "удержан" in t or "услуг" in t:
        return v.DEDUCTION
    return v.OTHER


def build_generic(row: Row) -> dict:
    line = v.empty_line()
    op_text = row.text("operation")
    op_header = _norm(row.headers.get("operation", ""))
    status_column = op_header.startswith("статус") or op_header == "status"
    if status_column and any(w in _norm(op_text) for w in _PARTIAL_WORDS):
        # Статус заказа «частичный возврат» не говорит, КАКИЕ позиции вернули;
        # считать заказ целиком продажей или целиком возвратом — одинаково неверно.
        raise SkipRow(f"статус «{op_text}»: какие позиции вернули, из выгрузки не видно — "
                      f"внесите их через единую форму")
    operation = classify_generic(op_text, status_column)
    if operation is None:
        raise SkipRow(f"статус «{op_text}» — не продажа и не возврат")

    quantity = row.quantity("quantity")
    revenue = row.money_or_none("revenue")
    if revenue is None and row.has("price"):
        price = row.money("price")
        revenue = price * (abs(quantity) if quantity is not None else 1)
    revenue = revenue or 0
    payout = row.money_or_none("payout")

    # Отчёт без колонки статуса помечает возврат минусом в количестве/сумме.
    if not op_text and ((quantity or 0) < 0 or revenue < 0):
        operation = v.RETURN

    line["operation"] = operation
    line["source_operation"] = op_text

    for name in v.EXPENSE_FIELDS:
        line[name] = abs(row.money(name))
    line["compensation"] = abs(row.money("compensation"))
    expenses = sum(line[name] for name in v.EXPENSE_FIELDS) - line["compensation"]
    commission = abs(row.money("commission")) + abs(row.money("acquiring"))

    if operation in v.GOODS_OPERATIONS:
        sign = -1 if operation == v.RETURN else 1
        line["quantity"] = abs(quantity) if quantity is not None else 1
        line["revenue"] = sign * abs(revenue)
        if payout is not None:
            # «К выплате» в строке отчёта — уже ИТОГ строки (за вычетом
            # комиссии и услуг). Он и должен остаться итогом: это деньги,
            # которые придут на счёт. Поэтому выплата «за товар» = итог +
            # расходы строки, а комиссия — всё, что площадка взяла сверх них.
            line["payout"] = sign * abs(payout) + expenses
            line["commission"] = line["revenue"] - line["payout"]
        else:
            line["commission"] = sign * commission
            line["payout"] = line["revenue"] - line["commission"]
    else:
        line["quantity"] = abs(quantity or 0)
        # Начисление без товара («Хранение: 1 200»): если сумма не разнесена
        # по колонкам расходов, берём её из колонки суммы/выплаты.
        raw = payout if payout is not None else revenue
        if expenses == 0 and raw:
            amount = abs(raw)
            if operation in v.EXPENSE_FIELDS:
                line[operation] += amount
            elif operation == v.COMPENSATION or raw > 0:
                line["compensation"] += amount
            else:
                line["deduction"] += amount

    line["operation_date"] = row.date("operation_date")
    line["order_date"] = row.date("order_date")
    line["order_id"] = row.text("order_id")
    line["barcode"] = row.text("barcode")
    line["article"] = row.text("article")
    line["platform_sku"] = row.text("platform_sku")
    line["name"] = row.text("name")
    line["size"] = row.text("size")
    return line


_GENERIC_REQUIRED_ANY = (("revenue", "price", "payout"), ("barcode", "article", "platform_sku"))

# Lamoda: «Отчёт комиссионера» (ООО «Купишуз»), .xlsx из кабинета, раз в месяц.
# Сверено с настоящим отчётом № CR000179664 за 01–30.09.2026: шапка из шести
# строк («Отчет комиссионера № … от …», «Период: С … по …», стороны, договор),
# затем таблица, в конце строка «Итого:». Возврат — строка с минусом в
# количестве и во всех суммах. Колонки:
#
#   Заказ | Дата заказа | Артикул (код Lamoda MP…) | SellerSKU (артикул и название
#   продавца) | Штрих-код партнера (EAN) — НАШ баркод | Штрих-код (код Lamoda SL…) |
#   Метод доставки | Количество | Цена реализации | Согласованная с комитентом цена |
#   К перечислению комитенту | Комиссия Купишуз, % | Комиссия Купишуз | НДС с ин.
#   компаний | Расходы по программе лояльности | Дата продажи | …
#
# Деньги продавца считаются от СОГЛАСОВАННОЙ цены, а не от цены реализации:
# скидку покупателю по программе лояльности оплачивает Lamoda («Расходы по
# программе лояльности» = согласованная − реализации; справочная колонка «…
# предоставлено Lamoda» её повторяет). Поэтому выручка — согласованная цена,
# выплата — «К перечислению комитенту», комиссия — разница между ними: это
# «Комиссия Купишуз» и, у заказов из-за рубежа (KZ…), «НДС с ин. компаний».
# Итог строк сходится со строкой «Итого:» отчёта до копейки.
LAMODA_ALIASES = {
    "order_id": ("Заказ", "Номер заказа", "№ заказа"),
    "order_date": ("Дата заказа",),
    "platform_sku": ("Артикул", "Артикул Lamoda", "SKU Lamoda"),
    "seller_sku": ("SellerSKU", "Seller SKU", "Артикул продавца", "Артикул партнера"),
    "barcode": ("Штрих-код партнера (EAN)", "Штрихкод партнера (EAN)", "Штрих-код партнёра (EAN)",
                "EAN", "Баркод"),
    "lamoda_code": ("Штрих-код", "Штрихкод"),
    "delivery_method": ("Метод доставки",),
    "quantity": ("Количество", "Кол-во"),
    "price": ("Цена реализации",),
    "agreed": ("Согласованная с комитентом цена",),
    "payout": ("К перечислению комитенту", "К перечислению"),
    "commission": ("Комиссия Купишуз", "Комиссия Lamoda"),
    "foreign_vat": ("НДС с ин. компаний",),
    "operation_date": ("Дата продажи", "Дата реализации"),
}


def build_lamoda(row: Row) -> dict:
    line = v.empty_line()
    quantity = row.quantity("quantity") or 0
    agreed = row.money_or_none("agreed")
    revenue = agreed if agreed is not None else row.money("price")
    payout = row.money("payout")
    if not quantity and not revenue and not payout:
        raise SkipRow("строка без количества и сумм")
    is_return = quantity < 0 or (quantity == 0 and (revenue < 0 or payout < 0))
    sign = -1 if is_return else 1
    method = row.text("delivery_method")
    line["operation"] = v.RETURN if is_return else v.SALE
    line["source_operation"] = ("Возврат" if is_return else "Продажа") + (f" ({method})" if method else "")
    line["quantity"] = abs(quantity)
    line["revenue"] = sign * abs(revenue)
    line["payout"] = sign * abs(payout)
    line["commission"] = line["revenue"] - line["payout"]
    line["operation_date"] = row.date("operation_date")
    line["order_date"] = row.date("order_date")
    line["order_id"] = row.text("order_id")
    line["barcode"] = row.text("barcode")
    line["srid"] = row.text("lamoda_code")
    line["platform_sku"] = row.text("platform_sku")
    seller = row.text("seller_sku")
    # SellerSKU у Lamoda — «артикул название размер цвет» одной строкой
    # («C23-2251C Авер Куртка 48 черный»). Артикул — первое слово; в
    # юнит-экономике его всё равно перекрывает артикул товара 1С по баркоду.
    line["article"] = seller.split()[0] if seller else ""
    line["name"] = seller
    return line


LAMODA_FORMAT = ReportFormat(
    code="lamoda",
    label="Lamoda — отчёт комиссионера",
    platforms=("lamoda",),
    aliases=LAMODA_ALIASES,
    build=build_lamoda,
    required=("payout", "quantity"),
    required_any=(("agreed", "price"), ("barcode", "seller_sku", "platform_sku")),
    min_fields=5,
    note="Сверено с отчётом комиссионера Lamoda за сентябрь 2026: выручка — согласованная "
         "цена, выплата — «К перечислению комитенту», итог сходится со строкой «Итого:».",
)

# Яндекс KIT: финансового отчёта у площадки нет (своей комиссии KIT не берёт).
# Продажи — выгрузка «Заказы» из кабинета с данными о товарах (Статус заказа,
# Артикул, Штрихкод, Наименование товара, Цена до скидки, Цена…). «Итого к
# оплате» там — сумма ЗАКАЗА покупателя, поэтому выручка считается по позиции:
# цена × количество (нет колонки количества — одна штука на строку).
KIT_ALIASES = {
    "operation": ("Статус заказа", "Статус", "status"),
    "operation_date": ("Дата доставки", "Дата", "Дата заказа", "created_at"),
    "order_id": ("№", "Номер заказа", "order_number", "Номер"),
    "srid": ("№ дочернего заказа",),
    "barcode": ("Штрихкод", "Штрих-код", "Баркод"),
    "article": ("Артикул", "Артикул товара"),
    "platform_sku": ("ID варианта", "product_variant_id"),
    "name": ("Наименование товара", "Наименование", "Товар", "name"),
    "size": ("Размер",),
    "quantity": ("Количество", "Кол-во", "quantity"),
    "price": ("Цена", "Цена со скидкой", "final_price"),
    "revenue": ("Сумма по позиции", "Стоимость позиции"),
    "commission": ("Комиссия", "Эквайринг"),
    "logistics": ("Стоимость доставки для продавца", "seller_costs"),
}

KIT_FORMAT = ReportFormat(
    code="kit",
    label="Яндекс KIT — выгрузка заказов с товарами",
    platforms=("kit",),
    aliases=KIT_ALIASES,
    build=build_generic,
    required_any=(("revenue", "price"), ("barcode", "article", "platform_sku")),
    min_fields=4,
    note="Выгрузка «Заказы» из кабинета KIT с галочкой «Добавить данные о товарах». В продажи "
         "идут доставленные и выполненные заказы, отменённые и незавершённые пропускаются. "
         "Точный список колонок выгрузки KIT не документирован — сверьте первый файл.",
)


# AWER CRM (своя CRM заказов сайта на KIT, база kitcrm.db) — «Продажи»,
# файл sales-items-<с>_<по>-<когда выгружен>.xlsx: Дата, Номер заказа,
# Артикул, Баркод, Сумма; строка «Итого:» под таблицей. Сверено с выгрузкой за
# 01–30.09.2026 (49 позиций, 309 950 ₽). Строка — одна штука (колонки
# количества нет, две штуки одного товара идут двумя строками). «Номер заказа»
# — номер для покупателя (`order_number` KIT, 100608), а не внутренний id
# заказа, по которому принимает заказы Sync Admin, поэтому он кладётся в
# `srid`, как и у загрузки KIT по API, — сверки по номеру с Sync Admin нет.
# «Сумма» — ДО комиссии (так сказал владелец 18.10.2026), и «Итого:» — итог
# сумм, а не выплаты. Комиссию разбор не знает: ставка — настройка, её
# применяет импорт (`apply_commission`), а до того выплата равна сумме.
AWER_CRM_ALIASES = {
    "operation_date": ("Дата",),
    "srid": ("Номер заказа",),
    "article": ("Артикул",),
    "barcode": ("Баркод",),
    "revenue": ("Сумма",),
}


def build_awer_crm(row: Row) -> dict:
    line = v.empty_line()
    amount = row.money("revenue")
    # Минус в сумме — возврат (в выгрузке продаж его не было, но молча считать
    # его продажей с отрицательной выручкой было бы хуже).
    line["operation"] = v.RETURN if amount < 0 else v.SALE
    line["source_operation"] = "Возврат (AWER CRM)" if amount < 0 else "Продажа (AWER CRM)"
    line["quantity"] = 1
    line["revenue"] = line["payout"] = amount
    line["operation_date"] = row.date("operation_date")
    line["srid"] = row.text("srid")
    line["barcode"] = row.text("barcode")
    line["article"] = row.text("article")
    return line


def apply_commission(lines: list[dict], percent: Decimal) -> int:
    """Комиссия `percent` % от выручки строки, до копейки (половина — вверх);
    выплата = выручка − комиссия. У возврата комиссия со знаком минус:
    удержанная при продаже возвращается. Возвращает сумму комиссии."""
    total = 0
    for line in lines:
        if line["operation"] not in v.GOODS_OPERATIONS:
            continue
        commission = int((Decimal(line["revenue"]) * percent / 100).quantize(Decimal(1), ROUND_HALF_UP))
        line["commission"] = commission
        line["payout"] = line["revenue"] - commission
        total += commission
    return total


AWER_CRM_FORMAT = ReportFormat(
    code="kit_awer_crm",
    label="AWER CRM — продажи сайта на KIT",
    platforms=("kit",),
    aliases=AWER_CRM_ALIASES,
    build=build_awer_crm,
    required=("operation_date", "srid", "barcode", "revenue"),
    min_fields=5,
    total_field="revenue",
    note="Выгрузка «Продажи» из AWER CRM (sales-items-…xlsx). Строка — одна штука; «Сумма» — до "
         "комиссии, она вычитается по ставке из настроек расчёта. Итог сверяется со строкой "
         "«Итого:», период — из имени файла.",
)


# Яндекс Пэй — «Детализация к платёжному поручению» (и «к акту»): комиссии
# эквайринга по заказам сайта на KIT. Выручки в этом отчёте нет — она уже
# пришла из выгрузки заказов, — поэтому строка несёт только комиссию.
KIT_PAY_ALIASES = {
    "operation_date": ("Дата транзакции",),
    "order_id": ("Номер заказа",),
    "srid": ("ID транзакции",),
    "source": ("Источник транзакции",),
    "amount": ("Сумма транзакции",),
    "fee_service": ("Комиссия за сервис", "Комиссия сервисная"),
    "fee_maintenance": ("Комиссия за обслуживание",),
    "transferred": ("Сумма транзакции попавшая в перечисление", "Сумма попавшая в акт"),
}


def build_kit_pay(row: Row) -> dict:
    line = v.empty_line()
    # В отчёте Яндекс Пэй удержанная комиссия — со знаком «−» (−57,6 у оплаты),
    # а при возврате покупателю она возвращается со знаком «+» (36 у
    # user_payment_refund). Расход для нас — та же сумма с обратным знаком.
    fee = -(row.money("fee_service") + row.money("fee_maintenance"))
    if not fee:
        raise SkipRow("транзакция без комиссии")
    line["operation"] = v.DEDUCTION
    line["source_operation"] = "Комиссия Яндекс Пэй" + (f" ({row.text('source')})"
                                                        if row.text("source") else "")
    # Комиссия без товара: «выплата» −комиссия, чтобы итог к перечислению
    # уменьшился, а в разбивке «куда уходит выручка» она встала в комиссию.
    line["commission"] = fee
    line["payout"] = -fee
    line["operation_date"] = row.date("operation_date")
    line["order_id"] = row.text("order_id")
    line["srid"] = row.text("srid")
    return line


KIT_PAY_FORMAT = ReportFormat(
    code="kit_yandex_pay",
    label="Яндекс Пэй — детализация к платёжному поручению (комиссии для KIT)",
    platforms=("kit",),
    aliases=KIT_PAY_ALIASES,
    build=build_kit_pay,
    required=("amount", "fee_service", "order_id"),
    min_fields=4,
    note="Сверено с образцом CSV из документации Яндекс Пэй. Даёт только комиссии "
         "эквайринга (возврат комиссии при возврате покупателю уменьшает расход); продажи "
         "KIT загружайте выгрузкой заказов.",
)


# ======================================================================
# Единая форма (шаблон) — подходит для любой площадки, в том числе Ozon.
# ======================================================================

_OPERATION_BY_LABEL = {_norm(label): code for code, label in v.OPERATION_LABELS.items()}
_OPERATION_BY_LABEL.update({code: code for code in v.OPERATION_LABELS})


def build_unified(row: Row) -> dict:
    line = v.empty_line()
    op_text = row.text("operation")
    operation = _OPERATION_BY_LABEL.get(_norm(op_text))
    if operation is None:
        allowed = ", ".join(v.OPERATION_LABELS.values())
        raise v.ValueParseError(f"тип операции «{op_text}» не из списка ({allowed})")
    line["operation"] = operation
    line["source_operation"] = row.text("source_operation") or v.OPERATION_LABELS[operation]
    line["quantity"] = abs(row.quantity("quantity") or 0)
    for name in v.MONEY_FIELDS:
        line[name] = row.money(name)
    if operation == v.RETURN:
        # Возврат уменьшает выручку и выплату, как бы ни был записан знак.
        line["revenue"] = -abs(line["revenue"])
        line["payout"] = -abs(line["payout"])
        line["commission"] = -abs(line["commission"])
    if operation in v.GOODS_OPERATIONS and not row.has("commission"):
        line["commission"] = line["revenue"] - line["payout"]
    for text_field in ("order_id", "srid", "barcode", "article", "platform_sku", "name", "size"):
        line[text_field] = row.text(text_field)
    line["operation_date"] = row.date("operation_date")
    line["order_date"] = row.date("order_date")
    return line


UNIFIED_FORMAT = ReportFormat(
    code="unified",
    label="Единая форма (шаблон)",
    platforms=v.REPORT_PLATFORMS,
    aliases={c.field: (c.header,) for c in v.UNIFIED_COLUMNS},
    build=build_unified,
    required=("operation", "operation_date", "payout"),
    min_fields=5,
    note="Наш собственный шаблон: подходит для любой площадки.",
)

FORMATS = (WB_FORMAT, LAMODA_FORMAT, KIT_FORMAT, AWER_CRM_FORMAT, KIT_PAY_FORMAT, UNIFIED_FORMAT)


def formats_for(platform: str) -> list[ReportFormat]:
    return [f for f in FORMATS if platform in f.platforms]


def _is_total_row(cells: list) -> bool:
    for cell in cells:
        if cell in (None, ""):
            continue
        text = _norm(str(cell))
        return text.startswith("итого") or text.startswith("всего")
    return False


def _period_from_title(rows: list[list]):
    """«Период: С 01.09.2017 по 30.09.2017» в шапке над таблицей."""
    for row in rows:
        text = " ".join(str(c) for c in row if c not in (None, ""))
        match = _PERIOD_RE.search(text)
        if match:
            try:
                start, end = (v.parse_date(match.group(1)), v.parse_date(match.group(2)))
            except v.ValueParseError:
                continue
            if start and end and start <= end:
                return start, end
    return None


_TITLE_NUMBER_RE = re.compile(r"отч[её]т[^№\n]{0,80}№\s*([0-9A-Za-zА-Яа-я_\-/]+)", re.IGNORECASE)


def _number_from_title(rows: list[list]) -> str | None:
    """«Отчет комиссионера № CR000179664 от 30.09.2026 …» в шапке над таблицей."""
    for row in rows:
        text = " ".join(str(c) for c in row if c not in (None, ""))
        match = _TITLE_NUMBER_RE.search(text)
        if match:
            return match.group(1)
    return None


def parse_sheets(sheets: list[tuple[str, list[list]]], platform: str) -> ParseResult:
    """Опознаёт формат (по листам и строке заголовков) и разбирает строки."""
    candidates = formats_for(platform)
    best = None   # (приоритет, найдено полей, формат, лист, строки, заголовок)
    for fmt in candidates:
        for sheet_name, rows in sheets:
            found = find_header(rows, fmt.aliases, fmt.required, fmt.required_any, fmt.min_fields)
            if found is None:
                continue
            header_row, mapping, matched = found
            # Единая форма опознаётся по своим уникальным заголовкам и важнее
            # общего разбора: иначе заполненный шаблон прочитался бы «как
            # Lamoda» по совпавшим «Баркод» и «Кол-во».
            priority = 1 if fmt is UNIFIED_FORMAT else 0
            key = (priority, matched)
            if best is None or key > best[0]:
                best = (key, fmt, sheet_name, rows, header_row, mapping)

    result = ParseResult()
    if best is None:
        preview = header_preview(sheets[0][1]) if sheets else []
        expected = "; ".join(
            f"{f.label}: " + ", ".join(f"«{f.aliases[name][0]}»" for name in f.required
                                       or [g[0] for g in f.required_any])
            for f in candidates)
        result.errors.append(
            "Отчёт не опознан. Колонки в файле: "
            + (", ".join(f"«{h}»" for h in preview[:25]) or "не найдены")
            + f". Ожидались, например: {expected}. Можно привести отчёт к единой форме "
              "(шаблон на этой странице) и загрузить его.")
        return result

    _, fmt, sheet_name, rows, header_row, mapping = best
    result.format = fmt
    result.sheet = sheet_name
    headers = {name: str(rows[header_row][idx]).strip() for name, idx in mapping.items()}

    skipped: dict[str, int] = {}
    dates_from, dates_to = set(), set()
    # Под строкой «Итого» у отчётов-актов идут подписи сторон («ИСПОЛНИТЕЛЬ …
    # ЗАКАЗЧИК» у Lamoda) — это не данные. Строку после итога, которая не
    # разбирается, пропускаем с предупреждением, а не роняем отчёт.
    after_total = False
    footer_skipped = 0
    body = len(rows) - header_row - 1
    for offset, cells in enumerate(rows[header_row + 1:], start=header_row + 2):
        progress.every(500, offset - header_row - 1, body, f"разбор строк ({fmt.label})")
        if not cells or all(c in (None, "") for c in cells):
            continue
        if _is_total_row(cells):
            after_total = True
            target = "declared_payout" if fmt.total_field == "payout" else "declared_revenue"
            if fmt.total_field in mapping and getattr(result, target) is None:
                try:
                    setattr(result, target, Row(cells, mapping, headers).money_or_none(fmt.total_field))
                except v.ValueParseError:
                    pass
            continue
        row = Row(cells, mapping, headers)
        try:
            line = fmt.build(row)
            if fmt is WB_FORMAT:
                number = row.text("report_number")
                if number:
                    result.report_numbers.add(number)
                if row.has("date_from") and row.has("date_to"):
                    d_from, d_to = row.date("date_from"), row.date("date_to")
                    if d_from and d_to:
                        dates_from.add(d_from)
                        dates_to.add(d_to)
        except SkipRow as e:
            skipped[str(e)] = skipped.get(str(e), 0) + 1
            continue
        except v.ValueParseError as e:
            if after_total:
                footer_skipped += 1
                continue
            result.errors_total += 1
            if len(result.errors) < MAX_ERRORS_SHOWN:
                result.errors.append(f"строка {offset}: {e}")
            continue
        line["row_no"] = offset
        result.lines.append(line)

    for reason, count in skipped.items():
        result.warnings.append(f"Пропущено строк: {count} ({reason}).")
    if footer_skipped:
        result.warnings.append(f"Под строкой «Итого» пропущено строк без данных: {footer_skipped} "
                               f"(подписи и реквизиты).")
    if dates_from and dates_to:
        result.period = (min(dates_from), max(dates_to))
    else:
        result.period = _period_from_title(rows[:header_row])
    if not result.report_numbers:
        number = _number_from_title(rows[:header_row])
        if number:
            result.report_numbers.add(number)
    if not result.lines and not result.errors:
        result.errors.append("В отчёте нет ни одной строки с данными.")
    return result


def parse_records(records: list[dict], platform: str) -> ParseResult:
    """Записи из API (список словарей) — тем же разбором, что и файлы: ключи
    становятся строкой заголовков. Поэтому для API нет отдельной копии правил
    знаков и статей — у WB поля API и колонки детализации описаны одним
    `WB_ALIASES`."""
    headers: list[str] = []
    for record in records:
        for key in record:
            if key not in headers:
                headers.append(key)
    rows = [headers] + [[record.get(key) for key in headers] for record in records]
    return parse_sheets([("API", rows)], platform)

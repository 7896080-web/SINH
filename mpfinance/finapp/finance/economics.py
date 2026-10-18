"""Юнит-экономика и сводки по площадкам из строк отчётов в единой форме.

Формула (всё в копейках, по строкам отчётов за период):

    Выручка            = продажи − возвраты (сумма реализации площадки)
    К перечислению     = выплата за товар − логистика − хранение − штрафы
                         − приёмка − прочие удержания + компенсации
    Себестоимость      = (продано − возвращено) × себестоимость на дату операции
                         (себестоимость 1С в долларах — по курсу ЦБ на ту же дату)
    Доп. расходы       = (продано − возвращено) × расход на единицу (упаковка…)
    Налог              = ставка × выручка            (УСН «Доходы»)
                       | ставка × (к перечислению − себестоимость − доп. расходы)
                                                     (УСН «Доходы − расходы»)
    Прибыль            = к перечислению − себестоимость − доп. расходы − налог
    Маржинальность     = прибыль / выручка;  ROI = прибыль / себестоимость

Расходы площадки, не привязанные к товару (хранение, удержания без баркода),
показываются отдельной строкой или, по желанию оператора, распределяются по
товарам пропорционально выручке — до копейки, без потери сумм на округлении.

Налог по «Доходы − расходы» считается линейно по каждой позиции: убыточная
позиция уменьшает базу, поэтому сумма по строкам равна итогу.
"""
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from finapp.finance import values as v
from finapp.finance.rates import RateBook, to_kop
from finapp.finance.settings import FinanceSettings
from finapp.models import Product, ProductCost, SalesReport, SalesReportLine

_CHUNK = 500


@dataclass
class Filters:
    date_from: date
    date_to: date
    platform: str | None = None
    account_id: int | None = None


@dataclass
class Totals:
    sold_qty: int = 0
    returned_qty: int = 0
    revenue: int = 0
    commission: int = 0
    payout: int = 0
    logistics: int = 0
    storage: int = 0
    penalty: int = 0
    acceptance: int = 0
    deduction: int = 0
    compensation: int = 0
    cogs: int = 0
    # Продано штук, для которых себестоимость не задана: прибыль по ним
    # завышена, и интерфейс должен это показать, а не молча считать ноль.
    cost_missing_qty: int = 0
    unmatched_lines: int = 0
    tax: int = 0
    extra: int = 0

    MONEY = ("revenue", "commission", "payout", "logistics", "storage", "penalty", "acceptance",
             "deduction", "compensation", "cogs", "tax", "extra")

    def add(self, other: "Totals"):
        for name in ("sold_qty", "returned_qty", "cost_missing_qty", "unmatched_lines") + self.MONEY:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    @property
    def net_qty(self) -> int:
        return self.sold_qty - self.returned_qty

    @property
    def total_payout(self) -> int:
        return (self.payout - self.logistics - self.storage - self.penalty - self.acceptance
                - self.deduction + self.compensation)

    @property
    def platform_costs(self) -> int:
        """Всё, что забрала площадка: выручка − к перечислению."""
        return self.revenue - self.total_payout

    @property
    def other_costs(self) -> int:
        return self.penalty + self.acceptance + self.deduction - self.compensation

    @property
    def profit(self) -> int:
        return self.total_payout - self.cogs - self.extra - self.tax

    @staticmethod
    def _ratio(num: int, den: int) -> float | None:
        return num / den if den else None

    @property
    def margin(self) -> float | None:
        return self._ratio(self.profit, self.revenue) if self.revenue > 0 else None

    @property
    def roi(self) -> float | None:
        return self._ratio(self.profit, self.cogs) if self.cogs > 0 else None

    @property
    def return_rate(self) -> float | None:
        return self._ratio(self.returned_qty, self.sold_qty)

    @property
    def platform_cost_share(self) -> float | None:
        return self._ratio(self.platform_costs, self.revenue) if self.revenue > 0 else None

    @property
    def profit_per_unit(self) -> int | None:
        return round(self.profit / self.net_qty) if self.net_qty > 0 else None

    @property
    def payout_per_unit(self) -> int | None:
        return round(self.total_payout / self.net_qty) if self.net_qty > 0 else None

    @property
    def avg_price(self) -> int | None:
        return round(self.revenue / self.net_qty) if self.net_qty > 0 else None


@dataclass
class Group:
    key: str | None
    title: str = ""
    article: str = ""
    size: str = ""
    color: str = ""
    uid_1c: str | None = None
    matched: bool = True
    platforms: set = field(default_factory=set)
    totals: Totals = field(default_factory=Totals)


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def apply_tax_and_extra(totals: Totals, settings: FinanceSettings):
    totals.extra = totals.net_qty * settings.extra_cost_kop
    rate = settings.tax_rate / 100
    if settings.tax_mode == "revenue":
        totals.tax = _round(Decimal(totals.revenue) * rate)
    elif settings.tax_mode == "profit":
        totals.tax = _round(Decimal(totals.total_payout - totals.cogs - totals.extra) * rate)
    else:
        totals.tax = 0


# ---------------------------------------------------------------- данные

_LINE_COLUMNS = (
    SalesReport.platform, SalesReport.account_id, SalesReportLine.operation,
    SalesReportLine.operation_date, SalesReportLine.uid_1c, SalesReportLine.barcode,
    SalesReportLine.article, SalesReportLine.name, SalesReportLine.size,
    SalesReportLine.quantity, SalesReportLine.revenue_kop, SalesReportLine.commission_kop,
    SalesReportLine.payout_kop, SalesReportLine.logistics_kop, SalesReportLine.storage_kop,
    SalesReportLine.penalty_kop, SalesReportLine.acceptance_kop, SalesReportLine.deduction_kop,
    SalesReportLine.compensation_kop,
)


def load_lines(db: Session, filters: Filters) -> list:
    query = db.query(*_LINE_COLUMNS).join(SalesReport, SalesReport.id == SalesReportLine.report_id) \
        .filter(SalesReportLine.operation_date >= filters.date_from,
                SalesReportLine.operation_date <= filters.date_to)
    if filters.platform:
        query = query.filter(SalesReport.platform == filters.platform)
    if filters.account_id:
        query = query.filter(SalesReport.account_id == filters.account_id)
    return query.all()


class CostBook:
    """Себестоимость на дату, в копейках. Для продаж раньше первой известной
    даты берётся самая ранняя цена: себестоимость обычно заводят «с сегодняшнего
    дня», и без этого все прошлые месяцы выглядели бы как продажи без
    себестоимости. Доллары (себестоимость из 1С) переводятся курсом ЦБ на дату
    операции; нет курса — нет и себестоимости (`None`), а не ноль."""

    def __init__(self, db: Session, uids: set[str], settings: FinanceSettings | None = None):
        self._dates: dict[str, list[date]] = {}
        self._costs: dict[str, list[tuple[int, str]]] = {}
        self.rates = RateBook(db, manual=settings.usd_rate_manual if settings else None)
        rows = []
        ordered = sorted(uids)
        for start in range(0, len(ordered), _CHUNK):
            chunk = ordered[start:start + _CHUNK]
            rows += db.query(ProductCost.uid_1c, ProductCost.valid_from, ProductCost.cost_minor,
                             ProductCost.currency) \
                .filter(ProductCost.uid_1c.in_(chunk)).all()
        by_uid = defaultdict(list)
        for uid, valid_from, cost, currency in rows:
            by_uid[uid].append((valid_from, cost, currency or "RUB"))
        for uid, items in by_uid.items():
            items.sort()
            self._dates[uid] = [d for d, _, _ in items]
            self._costs[uid] = [(c, cur) for _, c, cur in items]

    def cost_on(self, uid: str | None, day: date) -> int | None:
        if not uid or uid not in self._dates:
            return None
        idx = bisect_right(self._dates[uid], day) - 1
        minor, currency = self._costs[uid][max(idx, 0)]
        return to_kop(minor, currency, day, self.rates)


def _products(db: Session, uids: set[str]) -> dict[str, Product]:
    result = {}
    ordered = sorted(uids)
    for start in range(0, len(ordered), _CHUNK):
        for p in db.query(Product).filter(Product.uid_1c.in_(ordered[start:start + _CHUNK])):
            result[p.uid_1c] = p
    return result


def add_line(totals: Totals, line, cost: int | None):
    (platform, _account, operation, _day, uid, _bc, _art, _name, _size, qty, revenue, commission,
     payout, logistics, storage, penalty, acceptance, deduction, compensation) = line
    totals.revenue += revenue
    totals.commission += commission
    totals.payout += payout
    totals.logistics += logistics
    totals.storage += storage
    totals.penalty += penalty
    totals.acceptance += acceptance
    totals.deduction += deduction
    totals.compensation += compensation
    if operation in v.GOODS_OPERATIONS:
        sign = 1 if operation == v.SALE else -1
        if operation == v.SALE:
            totals.sold_qty += qty
        else:
            totals.returned_qty += qty
        if uid is None:
            totals.unmatched_lines += 1
        if cost is None:
            totals.cost_missing_qty += sign * qty
        else:
            totals.cogs += sign * qty * cost


# ---------------------------------------------------------------- расчёты

def _group_key(line, group_by: str, products: dict[str, Product]) -> str | None:
    uid, barcode, article = line[4], line[5], line[6]
    if group_by == "platform":
        return line[0]
    if group_by == "sku":
        if uid:
            return f"uid:{uid}"
        if barcode:
            return f"bc:{barcode}"
        return f"art:{article}" if article else None
    product = products.get(uid) if uid else None
    if product is not None and product.article:
        return f"art:{product.article}"
    return f"art:{article}" if article else (f"bc:{barcode}" if barcode else None)


def _allocate(groups: list[Group], unassigned: Totals):
    """Делит не привязанные к товару расходы по позициям пропорционально
    выручке методом наибольшего остатка — сумма долей равна исходной копейка
    в копейку."""
    weights = [max(g.totals.revenue, 0) for g in groups]
    total_weight = sum(weights)
    if not total_weight:
        return False
    for name in ("commission", "payout", "logistics", "storage", "penalty", "acceptance",
                 "deduction", "compensation", "revenue"):
        amount = getattr(unassigned, name)
        if not amount:
            continue
        shares = [amount * w // total_weight for w in weights]
        remainders = sorted(range(len(groups)),
                            key=lambda i: (amount * weights[i]) % total_weight, reverse=True)
        for i in remainders[:amount - sum(shares)]:
            shares[i] += 1
        for group, share in zip(groups, shares):
            setattr(group.totals, name, getattr(group.totals, name) + share)
        setattr(unassigned, name, 0)
    return True


def unit_economics(db: Session, filters: Filters, settings: FinanceSettings,
                   group_by: str = "article", allocate: bool = False, lines: list | None = None):
    """Позиции (артикул / SKU / площадка), строка «не привязано к товару» и итог.
    `lines` — уже загруженные строки (`load_lines`), чтобы дашборд, которому нужны
    три среза одного периода, читал их из базы один раз."""
    if lines is None:
        lines = load_lines(db, filters)
    uids = {line[4] for line in lines if line[4]}
    products = _products(db, uids)
    costs = CostBook(db, uids, settings)

    groups: dict[str | None, Group] = {}
    for line in lines:
        key = _group_key(line, group_by, products)
        group = groups.get(key)
        if group is None:
            group = groups[key] = Group(key=key)
        uid = line[4]
        product = products.get(uid) if uid else None
        if not group.title:
            group.title = (product.name if product else None) or line[7] or ""
            group.article = (product.article if product else None) or line[6] or ""
            if group_by == "sku":
                group.uid_1c = uid
                group.size = (product.size if product else None) or line[8] or ""
                group.color = (product.color if product else None) or ""
        if line[2] in v.GOODS_OPERATIONS and uid is None:
            group.matched = False
        group.platforms.add(line[0])
        cost = costs.cost_on(uid, line[3]) if line[2] in v.GOODS_OPERATIONS else None
        add_line(group.totals, line, cost)

    unassigned_group = groups.pop(None, None)
    unassigned = unassigned_group.totals if unassigned_group else Totals()
    rows = list(groups.values())
    allocated = allocate and _allocate(rows, unassigned)

    total = Totals()
    for group in rows:
        apply_tax_and_extra(group.totals, settings)
        total.add(group.totals)
    apply_tax_and_extra(unassigned, settings)
    total.add(unassigned)

    rows.sort(key=lambda g: (g.totals.profit, g.totals.revenue), reverse=True)
    return rows, unassigned, total, bool(allocated)


def platform_summary(db: Session, filters: Filters, settings: FinanceSettings,
                     lines: list | None = None) -> dict[str, Totals]:
    """Итоги по площадкам (у каждой строки отчёта площадка есть, поэтому
    «непривязанной» строки здесь не бывает)."""
    rows, _unassigned, _total, _ = unit_economics(db, filters, settings, group_by="platform",
                                                  lines=lines)
    return {g.key: g.totals for g in rows}


def bucket_start(day: date, bucket: str) -> date:
    if bucket == "month":
        return day.replace(day=1)
    return day - timedelta(days=day.weekday())


def trend(db: Session, filters: Filters, settings: FinanceSettings, bucket: str = "week",
          lines: list | None = None):
    """[(начало периода, площадка, Totals)] — для графика динамики."""
    if lines is None:
        lines = load_lines(db, filters)
    uids = {line[4] for line in lines if line[4]}
    costs = CostBook(db, uids, settings)
    buckets: dict[tuple[date, str], Totals] = defaultdict(Totals)
    for line in lines:
        cost = costs.cost_on(line[4], line[3]) if line[2] in v.GOODS_OPERATIONS else None
        add_line(buckets[(bucket_start(line[3], bucket), line[0])], line, cost)
    for totals in buckets.values():
        apply_tax_and_extra(totals, settings)
    return sorted(((start, platform, totals) for (start, platform), totals in buckets.items()),
                  key=lambda item: (item[0], item[1]))

"""Сверка загруженного отчёта площадки.

Четыре вопроса, на которые отвечает страница отчёта:

1. **Деньги.** Сходится ли итог строк с «Итого к оплате» площадки и с тем, что
   реально пришло на счёт (обе суммы вводит оператор).
2. **Заказы.** Каждая ли продажа отчёта есть среди заказов, принятых sync_admin
   (его база, только чтение — `syncdb.py`), не продан ли заказ, который sync_admin
   считает отменённым, и какие принятые заказы не встретились ни в одном отчёте.
3. **Товары.** У всех ли продаж/возвратов баркод сопоставлен с номенклатурой 1С —
   без этого отчёт нельзя загрузить в 1С.
4. **1С.** Сколько штук и на какую сумму к перечислению уйдёт в документы и что
   ответила 1С (`sales_1c.build_plan`, статус на шапке отчёта).
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from finapp import syncdb
from finapp.finance import values as v
from finapp.finance.economics import Totals, add_line
from finapp.models import SalesReport, SalesReportLine
from finapp.timeutils import local_day_start_utc

# Сколько расхождений показываем списком (остальные — числом).
LIST_LIMIT = 200


@dataclass
class OperationRow:
    operation: str
    label: str
    lines: int = 0
    totals: Totals = field(default_factory=Totals)
    quantity: int = 0


def _line_tuple(report: SalesReport, line: SalesReportLine):
    return (report.platform, report.account_id, line.operation, line.operation_date, line.uid_1c,
            line.barcode, line.article, line.name, line.size, line.quantity, line.revenue_kop,
            line.commission_kop, line.payout_kop, line.logistics_kop, line.storage_kop,
            line.penalty_kop, line.acceptance_kop, line.deduction_kop, line.compensation_kop)


def breakdown(db: Session, report: SalesReport) -> tuple[list[OperationRow], Totals]:
    """Итоги отчёта по видам операций и общий итог (без себестоимости)."""
    rows: dict[str, OperationRow] = {}
    total = Totals()
    lines = db.query(SalesReportLine).filter(SalesReportLine.report_id == report.id).all()
    for line in lines:
        row = rows.get(line.operation)
        if row is None:
            row = rows[line.operation] = OperationRow(
                line.operation, v.OPERATION_LABELS.get(line.operation, line.operation))
        row.lines += 1
        row.quantity += line.quantity
        item = _line_tuple(report, line)
        add_line(row.totals, item, 0)
        add_line(total, item, 0)
    order = list(v.OPERATION_LABELS)
    result = sorted(rows.values(), key=lambda r: order.index(r.operation)
                    if r.operation in order else len(order))
    return result, total


@dataclass
class MoneyCheck:
    computed: int
    declared: int | None
    bank: int | None

    @property
    def declared_diff(self) -> int | None:
        return None if self.declared is None else self.declared - self.computed

    @property
    def bank_diff(self) -> int | None:
        if self.bank is None:
            return None
        return self.bank - (self.declared if self.declared is not None else self.computed)


def money_check(report: SalesReport, total: Totals) -> MoneyCheck:
    return MoneyCheck(total.total_payout, report.declared_payout_kop, report.bank_payout_kop)


@dataclass
class OrderCheck:
    applicable: bool
    reason: str = ""
    report_orders: int = 0
    matched: int = 0
    without_order_id: int = 0
    not_in_system: list = field(default_factory=list)
    not_in_system_total: int = 0
    cancelled_in_system: list = field(default_factory=list)
    missing_in_reports: list = field(default_factory=list)
    missing_in_reports_total: int = 0


def order_check(db: Session, report: SalesReport) -> OrderCheck:
    account = report.account
    if account is None:
        return OrderCheck(False, "Кабинет у отчёта не указан — сравнивать не с чем: заказы "
                                 "sync_admin принимает по кабинетам.")
    if account.sync_account_id is None:
        return OrderCheck(False, f"У кабинета «{account.name}» не выбран тот же кабинет в Sync Admin "
                                 f"(страница «API-ключи») — сравнивать не с чем.")

    goods = db.query(SalesReportLine).filter(
        SalesReportLine.report_id == report.id,
        SalesReportLine.operation.in_(v.GOODS_OPERATIONS),
    ).all()
    by_order: dict[str, list[SalesReportLine]] = defaultdict(list)
    check = OrderCheck(True)
    for line in goods:
        if line.order_id:
            by_order[line.order_id].append(line)
        else:
            check.without_order_id += 1
    check.report_orders = len(by_order)
    if goods and not by_order:
        # Без номеров заказов, по которым принимает Sync Admin, сравнивать не по
        # чему, а «принятые заказы, которых нет в отчётах» назвали бы ВСЕ заказы
        # периода — при том что они в этом отчёте есть, только под другими номерами.
        shown = "номера заказов для покупателя" if any(line.srid for line in goods) else "номеров заказов нет"
        return OrderCheck(False, f"В отчёте {shown}, а Sync Admin хранит заказы по внутренним "
                                 f"номерам площадки — сверить по заказам нельзя.")

    # Границы периода — МЕСТНЫЕ сутки (отчёт площадки по московским датам), а
    # sync_admin хранит момент приёма в UTC.
    start = local_day_start_utc(report.period_from)
    end = local_day_start_utc(report.period_to + timedelta(days=1))
    try:
        system = syncdb.orders_by_ids(account.sync_account_id, sorted(by_order))
        accepted = syncdb.accepted_between(account.sync_account_id, start, end)
    except syncdb.SyncDbError as e:
        return OrderCheck(False, f"База Sync Admin недоступна: {e}")

    for order_id, lines in sorted(by_order.items()):
        order = system.get(order_id)
        sold = any(line.operation == v.SALE for line in lines)
        if order is None:
            check.not_in_system_total += 1
            if len(check.not_in_system) < LIST_LIMIT:
                check.not_in_system.append(lines[0])
            continue
        check.matched += 1
        # sync_admin вернул товар на ЦС (отмена), а площадка его продала: в 1С
        # товар лежит на ЦС и одновременно продан — прямое расхождение остатка.
        if sold and order.status == syncdb.CANCELLED:
            check.cancelled_in_system.append((order, lines[0]))

    # Принятые заказы периода, которых нет НИ В ОДНОМ отчёте этого кабинета (не
    # только в этом: продажа могла попасть в соседнюю неделю).
    reported: set[str] = set()
    ids = [o.order_id for o in accepted]
    same_account = db.query(SalesReport.id).filter(SalesReport.account_id == report.account_id)
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start:chunk_start + 500]
        reported |= {oid for (oid,) in db.query(SalesReportLine.order_id).filter(
            SalesReportLine.report_id.in_(same_account), SalesReportLine.order_id.in_(chunk))}
    missing = [o for o in accepted if o.order_id not in reported]
    check.missing_in_reports_total = len(missing)
    check.missing_in_reports = missing[:LIST_LIMIT]
    return check


def unmatched_barcodes(db: Session, report: SalesReport) -> list[dict]:
    """Продажи/возвраты без товара 1С, свёрнутые по баркоду."""
    rows: dict[str, dict] = {}
    for line in db.query(SalesReportLine).filter(
            SalesReportLine.report_id == report.id,
            SalesReportLine.operation.in_(v.GOODS_OPERATIONS),
            SalesReportLine.uid_1c.is_(None)):
        key = line.barcode or ""
        row = rows.setdefault(key, {"barcode": line.barcode or "", "article": line.article or "",
                                    "name": line.name or "", "size": line.size or "",
                                    "lines": 0, "quantity": 0, "revenue": 0})
        row["lines"] += 1
        row["quantity"] += line.quantity if line.operation == v.SALE else -line.quantity
        row["revenue"] += line.revenue_kop
    return sorted(rows.values(), key=lambda r: -abs(r["revenue"]))

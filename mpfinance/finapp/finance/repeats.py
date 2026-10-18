"""Одни и те же продажи в двух отчётах — отказ, а не предупреждение.

Тот же файл второй раз не ляжет (SHA256), тот же отчёт площадки — тоже (ключ по
номеру или периоду). Но одни и те же ПРОДАЖИ приходят разными отчётами: сентябрь
выгрузкой AWER CRM файлом и те же недели из базы CRM; месячный файл и недельный
за тот же период; продажи сайта из API KIT поверх продаж из CRM. Предупреждение
о пересечении периодов это видело, но не останавливало: обе копии ложились в
дашборд (выручка вдвое), и каждая уходила в 1С своим документом.

Продажа узнаётся по номеру заказа (`srid`), баркоду, виду операции и сумме за
штуку; штук — сколько их. Дата в признак НЕ входит намеренно: AWER CRM датирует
заказ без вручения временем своей пересинхронизации, и одна продажа в её файле от
30.09 и в базе стоит разными месяцами — совпадение по дате пропустило бы ровно
самый опасный повтор. Сумма — за штуку: KIT по API пишет позицию одной строкой
на все штуки, CRM — строкой на каждую.

Сравниваются отчёты одной площадки и одного кабинета (отчёт без кабинета — с
любым). Отчёты, у которых ОБА есть номер площадки (официальные отчёты WB, отчёт
комиссионера Lamoda), между собой не сравниваются: их единица — номер, повтор
номера ловит ключ отчёта, а у WB одна продажа законно встречается в двух отчётах
(сторно и корректная продажа). Предварительные недели Lamoda не в счёт: их
заменяет отчёт комиссионера (`lamoda.apply_correction`). Строки без номера
заказа не узнать — они не сравниваются.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, or_, true
from sqlalchemy.orm import Session

from finapp.finance import lamoda, values as v
from finapp.models import SalesReport, SalesReportLine

_CHUNK = 500


def sale_key(srid, barcode, operation, revenue, quantity) -> tuple | None:
    """Признак продажи и сколько в ней штук; None — строку не узнать."""
    srid = (srid or "").strip()
    if not srid or operation not in v.GOODS_OPERATIONS:
        return None
    pieces = abs(int(quantity or 0)) or 1
    return (srid, (barcode or "").strip(), operation, int(revenue or 0) // pieces)


@dataclass
class Repeats:
    pieces: int = 0                                   # сколько штук уже лежит в других отчётах
    total: int = 0                                    # сколько штук продаж/возвратов проверено
    reports: list[SalesReport] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.pieces > 0

    def text(self) -> str:
        where = ", ".join(f"#{r.id} ({r.period_from:%d.%m}–{r.period_to:%d.%m.%Y}, "
                          f"{'файл' if r.source == 'file' else r.source})" for r in self.reports[:5])
        return (f"эти продажи уже загружены: {self.pieces} шт из {self.total} совпадают (номер заказа, "
                f"баркод, сумма) с отчётами {where}. Второй раз они задвоили бы выручку и ушли бы в 1С "
                f"вторым документом. Если новый отчёт точнее — удалите прежний и загрузите снова.")


def _counts(lines) -> Counter:
    counts: Counter = Counter()
    for srid, barcode, operation, revenue, quantity in lines:
        key = sale_key(srid, barcode, operation, revenue, quantity)
        if key is not None:
            counts[key] += abs(int(quantity or 0)) or 1
    return counts


def find(db: Session, *, platform: str, account_id: int | None, numbered: bool, lines,
         exclude_ids=(), skip=lambda report: False) -> Repeats:
    """Какие из `lines` (srid, barcode, operation, revenue, quantity) уже лежат в
    других отчётах. `skip(report)` — отчёт, который сейчас будет заменён и потому
    не в счёт (неотправленные продажи прежнего источника за те же недели)."""
    mine = _counts(lines)
    result = Repeats(total=sum(mine.values()))
    if not mine:
        return result
    srids = sorted({key[0] for key in mine})
    theirs: dict[tuple, Counter] = defaultdict(Counter)
    reports: dict[int, SalesReport] = {}
    for start in range(0, len(srids), _CHUNK):
        chunk = srids[start:start + _CHUNK]
        # id строки в выборке обязателен: запрос с сущностью схлопывает одинаковые
        # строки, а две штуки одного заказа по одной цене — ровно одинаковые строки.
        query = db.query(SalesReportLine.id, SalesReportLine.srid, SalesReportLine.barcode,
                         SalesReportLine.operation, SalesReportLine.revenue_kop, SalesReportLine.quantity,
                         SalesReport) \
            .join(SalesReport, SalesReport.id == SalesReportLine.report_id) \
            .filter(SalesReport.platform == platform,
                    SalesReportLine.srid.in_(chunk),
                    SalesReportLine.operation.in_(v.GOODS_OPERATIONS),
                    func.coalesce(SalesReport.report_kind, "") != lamoda.PROVISIONAL,
                    true() if account_id is None
                    else or_(SalesReport.account_id == account_id, SalesReport.account_id.is_(None)))
        if exclude_ids:
            query = query.filter(SalesReport.id.notin_(list(exclude_ids)))
        if numbered:
            query = query.filter(func.coalesce(SalesReport.report_number, "") == "")
        for _line_id, srid, barcode, operation, revenue, quantity, report in query:
            if skip(report):
                continue
            key = sale_key(srid, barcode, operation, revenue, quantity)
            if key in mine:
                theirs[key][report.id] += abs(int(quantity or 0)) or 1
                reports[report.id] = report
    involved: set[int] = set()
    for key, by_report in theirs.items():
        result.pieces += min(mine[key], sum(by_report.values()))
        involved |= set(by_report)
    result.reports = sorted((reports[rid] for rid in involved), key=lambda r: (r.period_from, r.id))
    return result


def of_report(db: Session, report: SalesReport) -> Repeats:
    """Повторы уже лежащего отчёта — для страницы отчёта и запрета отправки в 1С.
    Нужны для отчётов, загруженных до этой проверки."""
    if report.report_kind == lamoda.PROVISIONAL:
        return Repeats()
    lines = db.query(SalesReportLine.srid, SalesReportLine.barcode, SalesReportLine.operation,
                     SalesReportLine.revenue_kop, SalesReportLine.quantity) \
        .filter(SalesReportLine.report_id == report.id,
                SalesReportLine.operation.in_(v.GOODS_OPERATIONS)).all()
    return find(db, platform=report.platform, account_id=report.account_id,
                numbered=bool(report.report_number), lines=lines, exclude_ids=[report.id])


def in_period(db: Session, date_from: date, date_to: date) -> list[list[SalesReport]]:
    """Группы отчётов, в которых лежат одни и те же продажи периода, — для
    дашборда: задвоенная выручка видна там, где на неё смотрят."""
    rows = db.query(SalesReportLine.srid, SalesReportLine.barcode, SalesReportLine.operation,
                    SalesReportLine.revenue_kop, SalesReportLine.quantity, SalesReport) \
        .join(SalesReport, SalesReport.id == SalesReportLine.report_id) \
        .filter(SalesReportLine.operation_date >= date_from, SalesReportLine.operation_date <= date_to,
                SalesReportLine.operation.in_(v.GOODS_OPERATIONS),
                SalesReportLine.srid.isnot(None), SalesReportLine.srid != "",
                func.coalesce(SalesReport.report_kind, "") != lamoda.PROVISIONAL).all()
    by_key: dict[tuple, dict[int, SalesReport]] = defaultdict(dict)
    for srid, barcode, operation, revenue, quantity, report in rows:
        key = sale_key(srid, barcode, operation, revenue, quantity)
        if key is not None:
            by_key[(report.platform,) + key][report.id] = report
    groups: dict[tuple[int, ...], list[SalesReport]] = {}
    for found in by_key.values():
        if len(found) < 2:
            continue
        pair = [r for r in found.values()]
        clash = [r for r in pair if any(
            o.id != r.id and not (r.report_number and o.report_number)
            and (r.account_id is None or o.account_id is None or r.account_id == o.account_id)
            for o in pair)]
        if len(clash) >= 2:
            ids = tuple(sorted(r.id for r in clash))
            groups.setdefault(ids, sorted(clash, key=lambda r: (r.period_from, r.id)))
    return list(groups.values())

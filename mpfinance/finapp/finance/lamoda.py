"""Lamoda: еженедельные отчёты — предварительно, отчёт комиссионера — итог.

Lamoda присылает на почту (b2b@lamoda.ru) «AWER - Weekly Report - ГГГГ-ММ-ДД.xlsx»:
- лист «Еженедельный отчет» — сводка по брендам за 8 недель (Выручка, Комиссия,
  Выручка за вычетом комиссии…, с оговоркой «*возможны расхождения»);
- лист «Продажи по размерам» — построчно продажи тех же 8 недель: Дата продажи,
  Неделя, Артикул Партнера, Размер Партнера, Короткий артикул Lamoda, Размер
  Lamoda, …, Продано товаров, …, Цена, согласованная с партнером, Цена продажи.
  Цены — за всю строку (у строки на 2 шт «Цена без скидки» 33000 = 2 × 16500), и
  сумма «Цены, согласованной с партнером» за неделю равна «Выручке» сводки.

Сверено с отчётом за 2026-10-05 (недели 33–40). Баркода (EAN) и возвратов в нём
нет. Поэтому:

1. Недельные данные — ПРЕДВАРИТЕЛЬНЫЕ: по отчёту на неделю (понедельник —
   воскресенье), `report_kind = "предварительный"`. Каждое письмо повторяет 8
   недель, и свежее письмо заменяет данные тех же недель. Выручка — согласованная
   цена (как в отчёте комиссионера), комиссия — по ставке из сводки (Комиссия /
   Выручка; сейчас 42%). Баркод берётся из прошлых отчётов комиссионера по тому
   же «Артикулу Партнера» (= SellerSKU) — иначе строка без товара 1С.
2. Отчёт комиссионера — официальный итоговый документ. При его загрузке
   предварительные строки за его период УДАЛЯЮТСЯ (`apply_correction`): дашборд и
   юнит-экономика за месяц дальше считаются только по нему. Строки недельных
   писем, пришедших позже, за этот период не загружаются.
3. В 1С уходит только отчёт комиссионера: предварительный — без баркодов и
   возвратов, и `sales_1c.upload_problems` его не пускает.

С 14.09.2026 на ту же почту приходит ещё и ПАРТНЁРСКИЙ отчёт («Lamoda Sales
Report - AWER ГГГГ-ММ-ДД.xlsx», дважды в неделю): лист «Артикулы» — по строке на
артикул-размер с полным и коротким артикулом Lamoda, размером партнёра и EAN,
плюс продажи за 7/14/28/180 дней, сток, оборачиваемость. Это аналитика, а не
отчёт о продажах: дат продаж и заказов в нём нет, а продажи за «последние 7
дней» задвоили бы недельный отчёт за те же дни. Поэтому продажи из него НЕ
берутся — он ложится справочником `LamodaArticle` (`import_catalog`), и строка
недельного отчёта находит по нему EAN (`ArticleBook`), а по EAN — товар 1С.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func
from sqlalchemy.orm import Session

from finapp.finance import values as v
from finapp.finance.formats import ParseResult, ReportFormat
from finapp.finance.readers import find_header
from finapp.models import LamodaArticle, SalesReport, SalesReportLine
from finapp.timeutils import now_utc, today_local

PROVISIONAL = "предварительный"
OFFICIAL_FORMAT = "lamoda"
DEFAULT_COMMISSION = Decimal("0.42")

WEEKLY_ALIASES = {
    "sale_date": ("Дата продажи",),
    "week": ("Неделя",),
    "partner_article": ("Артикул Партнера", "Артикул партнёра"),
    "partner_size": ("Размер Партнера", "Размер партнёра"),
    "lamoda_article": ("Короткий артикул Lamoda",),
    "lamoda_size": ("Размер Lamoda",),
    "name": ("Наименование товара",),
    "quantity": ("Продано товаров",),
    "agreed": ("Цена, согласованная с партнером", "Цена, согласованная с партнёром"),
    "sale_price": ("Цена продажи",),
    "brand": ("Brand name",),
}
WEEKLY_REQUIRED = ("sale_date", "partner_article", "quantity", "agreed")
WEEKLY_FORMAT = ReportFormat(code="lamoda_weekly", label="Lamoda — еженедельный отчёт (предварительно)",
                             platforms=("lamoda",), aliases={}, build=lambda row: {})


class WeeklyError(ValueError):
    pass


@dataclass
class Weekly:
    lines: list[dict] = field(default_factory=list)
    commission_rate: Decimal = DEFAULT_COMMISSION
    rate_from_summary: bool = False


def _normalize_sku(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def is_weekly(sheets: list[tuple[str, list[list]]]) -> bool:
    return any(find_header(rows, WEEKLY_ALIASES, WEEKLY_REQUIRED, min_fields=4) for _n, rows in sheets)


def _commission_rate(sheets) -> Decimal | None:
    """Комиссия / Выручка по сводному листу (все бренды, все недели)."""
    revenue = commission = Decimal(0)
    for _name, rows in sheets:
        for row in rows:
            label = next((str(c).strip() for c in row if c not in (None, "")), "")
            numbers = [Decimal(str(c)) for c in row if isinstance(c, (int, float)) and not isinstance(c, bool)]
            # Первая числовая колонка — «Изменения за неделю, %», её не берём.
            values = numbers[1:]
            if label == "Выручка":
                revenue += sum(values)
            elif label == "Комиссия":
                commission += sum(values)
    if revenue <= 0 or commission <= 0:
        return None
    rate = (commission / revenue).quantize(Decimal("0.0001"))
    return rate if Decimal("0") < rate < Decimal("1") else None


def parse_weekly(sheets: list[tuple[str, list[list]]]) -> Weekly:
    result = Weekly()
    rate = _commission_rate(sheets)
    if rate is not None:
        result.commission_rate, result.rate_from_summary = rate, True
    for _name, rows in sheets:
        found = find_header(rows, WEEKLY_ALIASES, WEEKLY_REQUIRED, min_fields=4)
        if found is None:
            continue
        header_row, mapping, _ = found

        def cell(cells, key):
            idx = mapping.get(key)
            return cells[idx] if idx is not None and idx < len(cells) else None

        for offset, cells in enumerate(rows[header_row + 1:], start=header_row + 2):
            if not cells or all(c in (None, "") for c in cells):
                continue
            try:
                day = v.parse_date(cell(cells, "sale_date"))
                quantity = v.parse_quantity(cell(cells, "quantity")) or 0
                revenue = v.parse_money(cell(cells, "agreed")) or 0
            except v.ValueParseError as e:
                raise WeeklyError(f"строка {offset}: {e}") from None
            if day is None or not quantity:
                continue
            article = v.parse_text(cell(cells, "partner_article"))
            commission = int((Decimal(revenue) * result.commission_rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            line = v.empty_line()
            line.update(
                operation=v.SALE, source_operation="Продажа (еженедельный отчёт)", operation_date=day,
                quantity=abs(quantity), revenue=revenue, commission=commission, payout=revenue - commission,
                article=article.split()[0] if article else "", name=article[:255],
                size=v.parse_text(cell(cells, "partner_size")),
                platform_sku=v.parse_text(cell(cells, "lamoda_article")),
            )
            line["row_no"] = offset
            result.lines.append(line)
        return result
    raise WeeklyError("не найден лист «Продажи по размерам» (Дата продажи, Артикул Партнера, "
                      "Продано товаров, Цена, согласованная с партнером)")


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _accounts_match(query, account_id):
    """Кабинет не указан с одной из сторон — считаем, что тот же (обычно у Lamoda
    кабинет один)."""
    if account_id is None:
        return query
    return query.filter((SalesReport.account_id == account_id) | SalesReport.account_id.is_(None))


def official_periods(db: Session, account_id: int | None) -> list[tuple[date, date]]:
    query = db.query(SalesReport.period_from, SalesReport.period_to).filter(
        SalesReport.platform == "lamoda", SalesReport.source_format == OFFICIAL_FORMAT)
    return [(a, b) for a, b in _accounts_match(query, account_id)]


def _barcodes_by_partner_article(db: Session) -> dict[str, str]:
    """«Артикул Партнера» → EAN по прошлым отчётам комиссионера (там SellerSKU и
    штрих-код партнёра в одной строке)."""
    out = {}
    rows = db.query(SalesReportLine.name, SalesReportLine.barcode).join(
        SalesReport, SalesReport.id == SalesReportLine.report_id).filter(
        SalesReport.platform == "lamoda", SalesReport.source_format == OFFICIAL_FORMAT,
        SalesReportLine.barcode.isnot(None), SalesReportLine.name.isnot(None))
    for name, barcode in rows:
        out[_normalize_sku(name)] = barcode
    return out


def import_weekly(db: Session, sheets, *, account_id: int | None, user: str, file_name: str,
                  source: str = "file"):
    """Недельный отчёт → по предварительному отчёту на каждую неделю. Возвращает
    (список ImportOutcome по неделям, сводка для человека)."""
    from finapp.finance.importer import store_api_report

    weekly = parse_weekly(sheets)
    periods = official_periods(db, account_id)
    barcodes = _barcodes_by_partner_article(db)
    by_week: dict[date, list[dict]] = defaultdict(list)
    covered = 0
    for line in weekly.lines:
        day = line["operation_date"]
        if any(a <= day <= b for a, b in periods):
            covered += 1          # месяц уже закрыт отчётом комиссионера
            continue
        line["barcode"] = barcodes.get(_normalize_sku(line["name"]), "")
        by_week[_monday(day)].append(line)

    outcomes = []
    for monday in sorted(by_week):
        iso = monday.isocalendar()
        # Та же неделя, загруженная с другим кабинетом или без него (письмо —
        # «кабинет не указан», файл руками — с кабинетом): номер недели включает
        # кабинет, и раньше неделя ложилась ДВАЖДЫ до прихода отчёта
        # комиссионера. Копии, чей кабинет «совпадает» по правилу
        # `_accounts_match`, заменяются свежей, а кабинет берётся указанный.
        twins = _accounts_match(db.query(SalesReport).filter(
            SalesReport.platform == "lamoda", SalesReport.report_kind == PROVISIONAL,
            SalesReport.period_from == monday), account_id).all()
        week_account = account_id or next((r.account_id for r in twins if r.account_id), None)
        number = f"W{iso[0]}{iso[1]:02d}-{week_account or 0}"
        for twin in twins:
            if twin.report_number != number and (week_account is None or twin.account_id in (None, week_account)):
                db.query(SalesReportLine).filter(SalesReportLine.report_id == twin.id) \
                    .delete(synchronize_session=False)
                db.delete(twin)
        db.flush()
        parsed = ParseResult(format=WEEKLY_FORMAT, lines=by_week[monday])
        outcome = store_api_report(
            db, platform="lamoda", account_id=week_account, parsed=parsed, period_from=monday,
            period_to=monday + timedelta(days=6), user=user, report_number=number,
            report_kind=PROVISIONAL, file_name=file_name, replace_unsent=True, source=source)
        outcomes.append(outcome)
    summary = (f"еженедельный отчёт Lamoda: недель {len(outcomes)}, строк "
               f"{sum(len(l) for l in by_week.values())}, комиссия "
               f"{weekly.commission_rate * 100:.2f}%" + (" (по сводке отчёта)" if weekly.rate_from_summary
                                                         else " (по умолчанию)"))
    if covered:
        summary += f"; строк за месяцы, закрытые отчётом комиссионера, пропущено: {covered}"
    return outcomes, summary


def apply_correction(db: Session, official: SalesReport) -> dict:
    """Отчёт комиссионера пришёл — предварительные строки за его период удаляются.
    Не коммитит."""
    query = db.query(SalesReport).filter(SalesReport.platform == "lamoda",
                                         SalesReport.report_kind == PROVISIONAL,
                                         SalesReport.period_from <= official.period_to,
                                         SalesReport.period_to >= official.period_from)
    if official.account_id is not None:
        query = query.filter((SalesReport.account_id == official.account_id) | SalesReport.account_id.is_(None))
    stats = {"lines": 0, "reports": 0, "deleted": 0}
    for report in query.all():
        removed = db.query(SalesReportLine).filter(
            SalesReportLine.report_id == report.id,
            SalesReportLine.operation_date >= official.period_from,
            SalesReportLine.operation_date <= official.period_to,
        ).delete(synchronize_session=False)
        if not removed:
            continue
        stats["lines"] += removed
        stats["reports"] += 1
        left = db.query(SalesReportLine).filter(SalesReportLine.report_id == report.id).count()
        if left == 0:
            db.delete(report)
            stats["deleted"] += 1
        else:
            report.lines_count = left
            report.note = ((report.note or "") + f" Часть строк заменена отчётом комиссионера "
                           f"№{official.report_number or official.id}.").strip()
    db.flush()
    return stats


# ---------------------------------------------------------- партнёрский отчёт

CATALOG_ALIASES = {
    "lamoda_sku": ("Артикул Lamoda",),
    "short_sku": ("Короткий артикул Lamoda",),
    "partner_size": ("Размер Партнера", "Размер партнёра"),
    "lamoda_size": ("Размер Lamoda",),
    "ean": ("EAN (штрихкод)", "EAN"),
    "partner_article": ("Артикул Партнера", "Артикул партнёра"),
}
CATALOG_REQUIRED = ("lamoda_sku", "short_sku", "partner_size", "ean")
_REPORT_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


class CatalogError(ValueError):
    pass


def norm_sku(text) -> str:
    return "".join(v.parse_text(text).split()).upper()


def norm_size(text) -> str:
    """Размер партнёра для сравнения: «2xl » и «2XL» — один размер."""
    return "".join(v.parse_text(text).split()).upper()


def is_catalog(sheets: list[tuple[str, list[list]]]) -> bool:
    """Партнёрский отчёт: полный и короткий артикул Lamoda, размер партнёра и EAN
    в одной строке. У недельного отчёта нет ни полного артикула, ни EAN."""
    return any(find_header(rows, CATALOG_ALIASES, CATALOG_REQUIRED, min_fields=4) for _n, rows in sheets)


def _report_date(rows: list[list], header_row: int) -> date | None:
    """«Дата отчета:» над таблицей — значение в соседней непустой ячейке."""
    for row in rows[:header_row]:
        cells = [c for c in row if c not in (None, "")]
        for i, cell in enumerate(cells[:-1]):
            if isinstance(cell, str) and cell.strip().lower().startswith("дата отчета"):
                try:
                    return v.parse_date(cells[i + 1])
                except v.ValueParseError:
                    return None
    return None


def parse_catalog(sheets: list[tuple[str, list[list]]], filename: str = "") -> tuple[date, list[dict]]:
    for _name, rows in sheets:
        found = find_header(rows, CATALOG_ALIASES, CATALOG_REQUIRED, min_fields=4)
        if found is None:
            continue
        header_row, mapping, _ = found

        def cell(cells, key):
            idx = mapping.get(key)
            return cells[idx] if idx is not None and idx < len(cells) else None

        day = _report_date(rows, header_row)
        if day is None:
            match = _REPORT_DATE_RE.search(filename or "")
            day = date.fromisoformat(match.group(1)) if match else today_local()
        items: dict[str, dict] = {}
        for cells in rows[header_row + 1:]:
            if not cells or all(c in (None, "") for c in cells):
                continue
            sku, short = norm_sku(cell(cells, "lamoda_sku")), norm_sku(cell(cells, "short_sku"))
            if not sku or not short:
                continue
            items[sku] = {
                "lamoda_sku": sku[:64], "short_sku": short[:32],
                "partner_size": norm_size(cell(cells, "partner_size"))[:32],
                "lamoda_size": v.parse_text(cell(cells, "lamoda_size"))[:32] or None,
                "ean": "".join(v.parse_text(cell(cells, "ean")).split())[:64] or None,
                "partner_article": v.parse_text(cell(cells, "partner_article"))[:255] or None,
            }
        return day, list(items.values())
    raise CatalogError("не найден лист «Артикулы» (Артикул Lamoda, Короткий артикул Lamoda, "
                       "Размер Партнера, EAN (штрихкод))")


def import_catalog(db: Session, sheets, filename: str = "") -> dict:
    """Партнёрский отчёт → справочник артикулов. Не коммитит.

    Файл постарше (письма перечитываются в любом порядке) не перетирает данные
    свежего, а пустой EAN не стирает известный: артикул EAN не теряет, пустая
    ячейка значит, что Lamoda его не прислала."""
    day, items = parse_catalog(sheets, filename)
    if not items:
        raise CatalogError("в партнёрском отчёте нет ни одного артикула")
    stats = {"date": day, "rows": len(items), "with_ean": sum(1 for i in items if i["ean"]),
             "added": 0, "updated": 0, "older": 0}
    skus = [i["lamoda_sku"] for i in items]
    known: dict[str, LamodaArticle] = {}
    for start in range(0, len(skus), 500):
        for row in db.query(LamodaArticle).filter(LamodaArticle.lamoda_sku.in_(skus[start:start + 500])):
            known[row.lamoda_sku] = row
    for item in items:
        row = known.get(item["lamoda_sku"])
        if row is None:
            row = LamodaArticle(lamoda_sku=item["lamoda_sku"])
            db.add(row)
            stats["added"] += 1
        elif row.report_date and row.report_date > day:
            stats["older"] += 1
            continue
        else:
            stats["updated"] += 1
        row.short_sku, row.partner_size = item["short_sku"], item["partner_size"]
        row.lamoda_size, row.partner_article = item["lamoda_size"], item["partner_article"]
        if item["ean"]:
            row.ean = item["ean"]
        row.report_date, row.updated_at = day, now_utc()
    db.flush()
    return stats


class ArticleBook:
    """Справочник артикулов для строк Lamoda без товара: по полному артикулу, а
    у недельного отчёта — по короткому артикулу и размеру партнёра.

    Полный артикул НЕ собирается из короткого и размера: у Lamoda это не
    всегда «короткий + IN + размер» («2.5XL» → «…IN25XL», джинсы — «…JE3234»), и
    собранный ключ промахнулся бы. Два разных артикула на одну пару «модель +
    размер» — не угадываем."""

    def __init__(self, db: Session, lines):
        wanted = sorted({norm_sku(line.platform_sku) for line in lines} - {""})
        self.by_sku: dict[str, LamodaArticle] = {}
        self.by_model: dict[tuple[str, str], LamodaArticle | None] = {}
        for start in range(0, len(wanted), 250):
            chunk = wanted[start:start + 250]
            for row in db.query(LamodaArticle).filter(
                    (LamodaArticle.lamoda_sku.in_(chunk)) | (LamodaArticle.short_sku.in_(chunk))):
                self.by_sku[row.lamoda_sku] = row
                key = (row.short_sku, row.partner_size)
                self.by_model[key] = row if key not in self.by_model else None

    def find(self, platform_sku, size) -> LamodaArticle | None:
        sku = norm_sku(platform_sku)
        if not sku:
            return None
        return self.by_sku.get(sku) or self.by_model.get((sku, norm_size(size)))


def catalog_status(db: Session) -> str:
    """Строка для «Диагностики»: есть ли справочник и насколько он свежий."""
    total, with_ean, last = db.query(func.count(LamodaArticle.lamoda_sku), func.count(LamodaArticle.ean),
                                     func.max(LamodaArticle.report_date)).one()
    if not total:
        return ("пуст — строки недельного отчёта Lamoda без EAN не найдут товар 1С; загрузите партнёрский "
                "отчёт Lamoda («Lamoda Sales Report …xlsx») или настройте почту")
    return (f"артикулов {total}, с EAN {with_ean}"
            + (f", последний партнёрский отчёт от {last:%d.%m.%Y}" if last else ""))

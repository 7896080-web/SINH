"""Загрузка отчёта площадки в базу: разбор файла, проверки, сопоставление
баркодов с номенклатурой 1С.

Импорт строгий: если хоть одна строка не разобралась (сумма текстом в денежной
колонке и т.п.), отчёт не сохраняется целиком. Половина отчёта — это неверная
выручка, прибыль и сверка, которые выглядят правдоподобно; отказ с номером
строки — честнее.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import or_, true
from sqlalchemy.orm import Session

from finapp.audit import log_action
from finapp import progress
from finapp import mapping
from finapp.finance import lamoda, repeats, statements, values as v
from finapp.finance.formats import AWER_CRM_FORMAT, KIT_PAY_FORMAT, ParseResult, apply_commission, parse_sheets
from finapp.finance.settings import load_settings
from finapp.finance.readers import ReportReadError, read_sheets
from finapp.models import OneCUploadStatus, PlatformAccount, SalesReport, SalesReportLine

# Строк в одной пачке IN (...) при сопоставлении баркодов — с запасом под лимит
# параметров SQLite.
_CHUNK = 500


@dataclass
class ImportOutcome:
    report: SalesReport | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Справка к итогу загрузки (не предупреждение): чем посчитана комиссия и т.п.
    notes: list[str] = field(default_factory=list)
    skipped: bool = False        # такой отчёт уже есть — ничего не записано
    replaced: bool = False       # прежний (ещё не отправленный в 1С) отчёт заменён свежим
    repeat: bool = False         # отказ: эти продажи уже лежат в другом отчёте (`repeats.py`)
    # Еженедельный отчёт Lamoda раскладывается на несколько отчётов (по неделе).
    reports: list[SalesReport] = field(default_factory=list)
    # Не отчёт о продажах, а справочник (партнёрский отчёт Lamoda) — принят, итоги
    # в `notes`; `report` при этом пуст, а `errors` нет.
    catalog: dict | None = None


def make_onec_key(platform: str, account_id: int | None, report_number: str | None,
                  period_from: date, period_to: date, kind: str = "") -> str:
    """Ключ документа в 1С. Один и тот же отчёт → один и тот же ключ, поэтому
    перезагрузка исправленного отчёта заменяет документ в 1С, а не дублирует.
    Только латиница, цифры и «-»: ключ едет в строке файла с разделителем «|»
    и в комментарий документа 1С."""
    number = re.sub(r"[^0-9A-Za-z_-]", "", report_number or "")[:40]
    if number:
        return f"SR-{platform}-{number}"
    # Без номера отчёт узнаётся по периоду — и по РОДУ (`kind`: формат файла,
    # продажи или доставка по API). Без рода файл Яндекс Пэй или AWER CRM за тот
    # же период занимал ключ, и отчёт KIT по API молча не ложился со словами
    # «уже отправлен в 1С» — про чужой отчёт, который в 1С не уходил вовсе.
    tag = re.sub(r"[^0-9A-Za-z]", "", kind or "")[:16]
    return f"SR-{platform}-{tag + '-' if tag else ''}{account_id or 0}-{period_from:%Y%m%d}-{period_to:%Y%m%d}"


_FILENAME_NUMBER_RE = re.compile(r"№\s*(\d+)")


def number_from_filename(filename: str) -> str:
    """Номер отчёта из имени файла: WB называет детализацию «Еженедельный
    детализированный отчет №848966494_531247 - 1.xlsx», номер до «_» — номер
    отчёта в кабинете. finance-api отдаёт номер отчёта как reportId; если это он
    же (сверить на первом отчёте из API), файл и тот же отчёт из API получат
    один ключ, и второй раз отчёт не загрузится."""
    match = _FILENAME_NUMBER_RE.search(filename or "")
    return match.group(1) if match else ""


_FILENAME_PERIOD_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})")


def period_from_filename(filename: str) -> tuple[date, date] | None:
    """Период из имени файла «…-2026-09-01_2026-09-30-…» (AWER CRM). По датам
    строк месяц, в последний день которого не продали ничего, кончился бы
    раньше, — а имя файла называет тот период, за который выгружали."""
    match = _FILENAME_PERIOD_RE.search(filename or "")
    if not match:
        return None
    try:
        start, end = (date.fromisoformat(g) for g in match.groups())
    except ValueError:
        return None
    return (start, end) if start <= end else None


_LAMODA_SHORT_RE = re.compile(r"^MP[0-9A-Z]{10}$")


def lamoda_sku_keys(platform_sku: str | None, size: str | None) -> list[str]:
    """Чем ещё искать строку Lamoda в «Мэппинге» Sync Admin, когда EAN нет и
    справочник артикулов (`lamoda.ArticleBook`) строку не знает.

    В отчёте комиссионера «Артикул» — полный артикул Lamoda с размером
    («MP002XM0WFWSIN3XL»): ищется как есть. В еженедельном отчёте — только
    короткий артикул модели («MP002XM22QRZ») и «Размер Партнера» («4XL»): полный
    собирается по тому же правилу, что в отчёте комиссионера, — короткий + «IN» +
    размер. Короткий артикул сам по себе не ищется: он один на весь размерный
    ряд, и продажа легла бы не на тот размер."""
    sku = (platform_sku or "").strip().upper()
    if not sku:
        return []
    if _LAMODA_SHORT_RE.match(sku):
        size = "".join((size or "").split()).upper()
        return [f"{sku}IN{size}"] if size else []
    return [sku]


def resolve_uids(db: Session, report: SalesReport, notes: list[str] | None = None) -> int:
    """Проставляет строкам uid_1c (`mapping.resolve`: «Мэппинг» sync_admin, затем
    справочник 1С) и пересчитывает, сколько строк продаж/возвратов осталось без
    товара 1С. Вызывается при импорте и повторно — кнопкой «Пересопоставить»,
    перед отправкой в 1С и раз в час фоном (`remap_unmatched`): мэппинг в
    sync_admin могли дополнить.

    У Lamoda строка без EAN (или с EAN, которого нет в мэппинге) ищется ещё по
    справочнику артикулов из партнёрского отчёта Lamoda (`lamoda.ArticleBook`:
    артикул → EAN) и по самому артикулу Lamoda (`lamoda_sku_keys`), если его
    завели в «Мэппинг». 18.10.2026 на бою артикулов Lamoda в «Мэппинге» не было
    ни одного — без справочника недельные строки товар 1С не находили вовсе."""
    lines = db.query(SalesReportLine).filter(SalesReportLine.report_id == report.id).all()
    found = mapping.resolve(db, (line.barcode for line in lines))
    if notes is not None and found.note:
        notes.append(found.note)
    if notes is not None and found.ambiguous:
        notes.append("Баркоды у нескольких товаров в справочнике 1С (не сопоставлены): "
                     + ", ".join(sorted(found.ambiguous)[:5]) + ".")
    for line in lines:
        line.uid_1c = found.uid_by_barcode.get(line.barcode) if line.barcode else None
    if report.platform == "lamoda":
        rest = [line for line in lines if line.uid_1c is None and line.operation in v.GOODS_OPERATIONS]
        book = lamoda.ArticleBook(db, rest) if rest else None
        wanted = []
        for line in rest:
            article = book.find(line.platform_sku, line.size)
            keys = []
            if article is not None:
                if article.ean:
                    keys.append(article.ean)
                    if not line.barcode:
                        # EAN из партнёрского отчёта — дальше строка ищется по нему
                        # первым же проходом и показывает его в отчёте.
                        line.barcode = article.ean
                keys.append(article.lamoda_sku)
            keys = list(dict.fromkeys(keys + lamoda_sku_keys(line.platform_sku, line.size)))
            if keys:
                wanted.append((line, keys))
        if wanted:
            by_key = mapping.resolve(db, (key for _line, keys in wanted for key in keys))
            matched = 0
            for line, keys in wanted:
                uid = next((by_key.uid_by_barcode[k] for k in keys if k in by_key.uid_by_barcode), None)
                if uid is not None:
                    line.uid_1c = uid
                    matched += 1
            if notes is not None and matched:
                notes.append(f"По артикулу Lamoda (EAN нет или его нет в «Мэппинге»; EAN — из партнёрского "
                             f"отчёта Lamoda) сопоставлено строк: {matched}.")
    unmatched = sum(1 for line in lines if line.operation in v.GOODS_OPERATIONS and line.uid_1c is None)
    report.unmatched_lines = unmatched
    return unmatched


def remap_unmatched(db: Session, limit: int = 200, platform: str | None = None) -> tuple[int, int]:
    """Пересопоставить неотправленные отчёты, где остались строки без товара 1С:
    «Мэппинг» Sync Admin дополняют, а нажимать «Пересопоставить» в каждом отчёте
    руками никто не станет. Отправленные в 1С не трогаем — документ построен.
    Возвращает (отчётов просмотрено, строк сопоставлено). Не коммитит."""
    query = db.query(SalesReport).filter(
        SalesReport.unmatched_lines > 0,
        SalesReport.onec_status.in_((OneCUploadStatus.none, OneCUploadStatus.failed)))
    if platform:
        query = query.filter(SalesReport.platform == platform)
    reports = query.order_by(SalesReport.id.desc()).limit(limit).all()
    fixed = 0
    for report in reports:
        before = report.unmatched_lines or 0
        fixed += max(before - resolve_uids(db, report), 0)
    return len(reports), fixed


def _with_goods(db: Session, report_ids: list[int]) -> set[int]:
    """Из отчётов — те, в которых есть продажи или возвраты."""
    return {rid for (rid,) in db.query(SalesReportLine.report_id).filter(
        SalesReportLine.report_id.in_(report_ids),
        SalesReportLine.operation.in_(v.GOODS_OPERATIONS)).distinct()}


def overlap_warning(overlaps: list[SalesReport]) -> str:
    return ("Период пересекается с уже загруженными отчётами этой площадки: "
            + ", ".join(f"#{r.id} ({r.period_from:%d.%m}–{r.period_to:%d.%m.%Y})" for r in overlaps[:5])
            + ". Если это те же продажи — они посчитаются дважды (у WB «основной» и «по выкупам» "
              "за одну неделю — разные отчёты).")


def sales_overlaps(db: Session, report: SalesReport) -> list[SalesReport]:
    """Отчёты с продажами, пересекающиеся с этим по периоду и кабинету, —
    для карточки на странице отчёта: в сообщении при загрузке предупреждение
    могло потеряться."""
    if report.report_kind == lamoda.PROVISIONAL:
        return []
    others = [r for r in overlapping_reports(db, report) if r.report_kind != lamoda.PROVISIONAL]
    if not others:
        return []
    with_goods = _with_goods(db, [report.id] + [r.id for r in others])
    return [r for r in others if r.id in with_goods] if report.id in with_goods else []


def overlapping_reports(db: Session, report: SalesReport) -> list[SalesReport]:
    """Другие отчёты той же площадки и кабинета с пересекающимся периодом —
    риск посчитать одни и те же продажи дважды (недельный + месячный отчёт)."""
    # Кабинет на форме загрузки необязателен: отчёт без кабинета может быть
    # отчётом любого кабинета, и сравнивать его надо со всеми (и наоборот).
    return db.query(SalesReport).filter(
        SalesReport.id != report.id,
        SalesReport.platform == report.platform,
        true() if report.account_id is None
        else or_(SalesReport.account_id == report.account_id, SalesReport.account_id.is_(None)),
        SalesReport.period_from <= report.period_to,
        SalesReport.period_to >= report.period_from,
    ).order_by(SalesReport.period_from).all()


def import_report(db: Session, *, platform: str, file_bytes: bytes, filename: str, user: str,
                  account_id: int | None = None, report_number: str = "",
                  period_from: date | None = None, period_to: date | None = None,
                  source: str = "file") -> ImportOutcome:
    outcome = ImportOutcome()

    if platform not in v.REPORT_PLATFORMS:
        outcome.errors.append(f"Неизвестная площадка «{platform}».")
        return outcome

    if account_id is not None:
        account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
        if account is None:
            outcome.errors.append(f"Кабинет #{account_id} не найден.")
            return outcome
        if account.platform != platform:
            outcome.errors.append(
                f"Кабинет «{account.name}» — {v.PLATFORM_LABELS[account.platform]}, "
                f"а отчёт загружается как {v.PLATFORM_LABELS[platform]}.")
            return outcome

    if (period_from is None) != (period_to is None):
        outcome.errors.append("Укажите обе даты периода или ни одной (тогда период возьмём из отчёта).")
        return outcome
    if period_from and period_to and period_from > period_to:
        outcome.errors.append("Начало периода позже конца.")
        return outcome

    sha256 = hashlib.sha256(file_bytes or b"").hexdigest()
    duplicate = db.query(SalesReport).filter(SalesReport.file_sha256 == sha256).first()
    if duplicate is not None:
        outcome.errors.append(f"Этот файл уже загружен — отчёт #{duplicate.id} "
                              f"({duplicate.period_from:%d.%m.%Y}–{duplicate.period_to:%d.%m.%Y}).")
        return outcome

    try:
        sheets = read_sheets(file_bytes, filename)
    except ReportReadError as e:
        outcome.errors.append(str(e))
        return outcome

    if platform == "lamoda" and lamoda.is_weekly(sheets):
        return _import_lamoda_weekly(db, sheets, account_id=account_id, user=user, filename=filename,
                                     source=source)
    if platform == "lamoda" and lamoda.is_catalog(sheets):
        return _import_lamoda_catalog(db, sheets, filename=filename)

    parsed = parse_sheets(sheets, platform)
    if parsed.errors:
        outcome.errors.extend(parsed.errors)
        if parsed.errors_total > len(parsed.errors):
            outcome.errors.append(f"… и ещё ошибок: {parsed.errors_total - len(parsed.errors)}.")
        if parsed.format is not None and parsed.errors_total:
            outcome.errors.append("Отчёт не сохранён: строки с ошибками изменили бы итоги. "
                                  "Исправьте файл и загрузите снова.")
        return outcome

    if parsed.format is KIT_PAY_FORMAT and load_settings(db).kit_sales_source == "crm":
        # 3,5% AWER CRM — это и есть эквайринг Яндекс Пэй (владелец, 18.10.2026):
        # продажи из CRM уже за его вычетом, и детализация Пэй вычла бы его второй раз.
        outcome.errors.append(
            "Детализация Яндекс Пэй не загружена: продажи сайта берутся из AWER CRM, и комиссия "
            "эквайринга уже вычтена из них (ставка — «Себестоимость» → «Настройки»). Загрузка Пэй "
            "вычла бы её второй раз.")
        return outcome
    if parsed.format is AWER_CRM_FORMAT:
        _awer_crm(db, parsed, outcome)

    # Период без явного указания — по датам ПРОДАЖ И ВОЗВРАТОВ, если они есть. В
    # детализации WB у строк логистики «Дата продажи» бывает на месяц раньше
    # недели отчёта (отчёт №848966494 за 14–20.09.2026: доставки от 22.08), и
    # период по всем датам вышел бы месячным.
    goods_dates = [line["operation_date"] for line in parsed.lines
                   if line["operation_date"] and line["operation"] in v.GOODS_OPERATIONS]
    dates = goods_dates or [line["operation_date"] for line in parsed.lines if line["operation_date"]]
    if period_from is None:
        if parsed.period:
            period_from, period_to = parsed.period
        elif period_from_filename(filename):
            period_from, period_to = period_from_filename(filename)
        elif dates:
            period_from, period_to = min(dates), max(dates)
        else:
            outcome.errors.append("В отчёте нет дат операций — укажите период вручную.")
            return outcome

    number = (report_number or "").strip()
    if not number and len(parsed.report_numbers) == 1:
        number = next(iter(parsed.report_numbers))
    elif not number and len(parsed.report_numbers) > 1:
        outcome.warnings.append(
            f"В файле несколько номеров отчёта ({', '.join(sorted(parsed.report_numbers)[:5])}) — "
            f"отчёт сохранён по периоду.")
    if not number and not parsed.report_numbers:
        number = number_from_filename(filename)

    onec_key = make_onec_key(platform, account_id, number, period_from, period_to, parsed.format.code)
    same_key = db.query(SalesReport).filter(SalesReport.onec_key == onec_key).first()
    if same_key is None and not number:
        # Отчёт, загруженный до появления рода в ключе: тот же формат и период —
        # тот же отчёт.
        same_key = db.query(SalesReport).filter(
            SalesReport.onec_key == make_onec_key(platform, account_id, "", period_from, period_to),
            SalesReport.source_format == parsed.format.code).first()
    if same_key is not None:
        outcome.errors.append(
            f"Отчёт с тем же {'номером' if number else 'периодом и кабинетом'} уже загружен — "
            f"#{same_key.id}. Чтобы заменить, удалите старый"
            + ("" if number else " или укажите номер отчёта площадки, если это другой отчёт")
            + ".")
        return outcome
    found = repeats.find(db, platform=platform, account_id=account_id, numbered=bool(number),
                         lines=goods_of(parsed.lines))
    if found:
        outcome.errors.append("Отчёт не загружен: " + found.text())
        outcome.repeat = True
        return outcome

    _store(db, outcome, platform=platform, account_id=account_id, parsed=parsed, user=user,
           number=number, period_from=period_from, period_to=period_to, source=source,
           file_name=filename, file_sha256=sha256,
           declared_payout=parsed.declared_payout if parsed.declared_payout is not None
           else statements.declared_for(db, platform, number),
           report_kind=None, onec_key=onec_key)
    if outcome.report is not None and parsed.format is AWER_CRM_FORMAT:
        pay = [r for r in overlapping_reports(db, outcome.report) if r.source_format == KIT_PAY_FORMAT.code]
        if pay:
            outcome.warnings.insert(0, "За этот период загружена детализация Яндекс Пэй: "
                                    + ", ".join(f"#{r.id}" for r in pay[:5])
                                    + " — комиссия эквайринга посчитается дважды (она уже вычтена из "
                                      "продаж AWER CRM). Удалите детализацию Пэй.")
    if outcome.report is not None and parsed.format.code == lamoda.OFFICIAL_FORMAT:
        # Отчёт комиссионера — итоговый документ: предварительные данные
        # еженедельных отчётов за его период заменяются им.
        fixed = lamoda.apply_correction(db, outcome.report)
        db.commit()
        if fixed["lines"]:
            outcome.warnings.append(
                f"Коррекция: предварительные данные еженедельных отчётов за период заменены этим "
                f"отчётом — удалено строк {fixed['lines']} из {fixed['reports']} недельных отчётов.")
    return outcome


def _import_lamoda_weekly(db: Session, sheets, *, account_id, user: str, filename: str,
                          source: str = "file") -> ImportOutcome:
    outcome = ImportOutcome()
    try:
        weeks, summary = lamoda.import_weekly(db, sheets, account_id=account_id, user=user,
                                              file_name=filename, source=source)
    except lamoda.WeeklyError as e:
        outcome.errors.append(f"Еженедельный отчёт Lamoda не разобран: {e}.")
        return outcome
    for week in weeks:
        if week.errors:
            outcome.errors.extend(week.errors)
        elif week.report is not None:
            outcome.reports.append(week.report)
    outcome.report = outcome.reports[-1] if outcome.reports else None
    outcome.warnings.append(summary[:1].upper() + summary[1:] + ".")
    if not weeks:
        outcome.errors.append("В еженедельном отчёте нет продаж за незакрытые недели.")
    return outcome

def _import_lamoda_catalog(db: Session, sheets, *, filename: str) -> ImportOutcome:
    """Партнёрский отчёт Lamoda — справочник артикулов, не продажи
    (`lamoda.import_catalog`). Принят — сразу пересопоставляются неотправленные
    отчёты Lamoda: ждать часового прохода незачем. Коммитит."""
    outcome = ImportOutcome()
    try:
        stats = lamoda.import_catalog(db, sheets, filename)
    except lamoda.CatalogError as e:
        outcome.errors.append(f"Партнёрский отчёт Lamoda не разобран: {e}.")
        return outcome
    seen, fixed = remap_unmatched(db, platform="lamoda")
    db.commit()
    outcome.catalog = stats
    outcome.notes.append(
        f"Партнёрский отчёт Lamoda от {stats['date']:%d.%m.%Y} принят как справочник артикулов: "
        f"{stats['rows']} артикулов, с EAN {stats['with_ean']}, новых {stats['added']}. Продажи из него "
        f"не берутся: дат и заказов в нём нет, а продажи «за последние 7 дней» задвоили бы недельный "
        f"отчёт. Строк продаж Lamoda нашли товар 1С: {fixed}"
        + (f" (в {seen} неотправленных отчётах)" if seen else "") + ".")
    if stats["older"]:
        outcome.notes.append(f"Не обновлено артикулов — справочник уже свежее этого файла: {stats['older']}.")
    return outcome


def _awer_crm(db: Session, parsed: ParseResult, outcome: ImportOutcome) -> None:
    """«Сумма» AWER CRM — до комиссии: вычесть её по ставке из настроек и
    сверить итог сумм со строкой «Итого:» самого файла."""
    revenue = sum(line["revenue"] for line in parsed.lines if line["operation"] in v.GOODS_OPERATIONS)
    if parsed.declared_revenue is not None and parsed.declared_revenue != revenue:
        outcome.warnings.append(
            f"Сумма строк ({v.format_rub(revenue)}) не совпала со строкой «Итого:» файла "
            f"({v.format_rub(parsed.declared_revenue)}) — файл выгружен не целиком?")
    percent = load_settings(db).kit_crm_commission
    commission = apply_commission(parsed.lines, percent)
    outcome.notes.append(
        f"Комиссия {str(percent).replace('.', ',')}% от «Суммы» AWER CRM: {v.format_rub(commission)} ₽, "
        f"к перечислению {v.format_rub(revenue - commission)} ₽ (ставка — «Себестоимость» → «Настройки»).")


def _store(db: Session, outcome: ImportOutcome, *, platform: str, account_id: int | None,
           parsed: ParseResult, user: str, number: str, period_from: date, period_to: date,
           source: str, file_name: str, file_sha256: str | None, declared_payout: int | None,
           report_kind: str | None, onec_key: str) -> None:
    """Запись разобранного отчёта — общая для файла и для API: те же строки,
    то же сопоставление с 1С и те же предупреждения."""
    lines = parsed.lines
    report = SalesReport(
        platform=platform, account_id=account_id, report_number=number or None,
        period_from=period_from, period_to=period_to, source_format=parsed.format.code,
        source=source, report_kind=report_kind,
        file_name=(file_name or "")[:255], file_sha256=file_sha256, uploaded_by=user,
        lines_count=len(lines), onec_key=onec_key, declared_payout_kop=declared_payout,
    )
    db.add(report)
    db.flush()

    outside = 0
    mappings = []
    for line in lines:
        op_date = line["operation_date"] or period_to
        if not (period_from <= op_date <= period_to):
            outside += 1
        mapping = {
            "report_id": report.id, "row_no": line.get("row_no"),
            "operation": line["operation"], "source_operation": line["source_operation"][:128],
            "operation_date": op_date, "order_date": line["order_date"],
            "order_id": line["order_id"][:128] or None, "srid": line["srid"][:128] or None,
            "barcode": line["barcode"][:64] or None, "article": line["article"][:128] or None,
            "platform_sku": line["platform_sku"][:64] or None, "name": line["name"][:255] or None,
            "size": line["size"][:64] or None, "quantity": line["quantity"],
        }
        for name in v.MONEY_FIELDS:
            mapping[f"{name}_kop"] = line[name]
        mappings.append(mapping)
    progress.report(None, None, f"запись строк в базу: {len(mappings)}")
    db.bulk_insert_mappings(SalesReportLine, mappings)
    db.flush()

    progress.report(None, None, "сопоставление баркодов с 1С")
    mapping_notes: list[str] = []
    unmatched = resolve_uids(db, report, mapping_notes)

    if parsed.warnings:
        outcome.warnings.extend(parsed.warnings)
    if declared_payout is not None:
        computed = sum(v.line_total(line) for line in lines)
        if computed != declared_payout:
            outcome.warnings.insert(0,
                f"Итог строк ({v.format_rub(computed)}) не совпал с «Итого к оплате» площадки "
                f"({v.format_rub(declared_payout)}) — проверьте раздел «Сверка денег».")
    others = sorted({line["source_operation"] for line in lines
                     if line["operation"] == v.OTHER and line["source_operation"]})
    if others:
        outcome.warnings.append("Операции, не отнесённые ни к одной статье (учтены как «Прочее»): "
                                + ", ".join(f"«{o}»" for o in others[:10]) + ".")
    if outside:
        outcome.warnings.append(f"Строк с датой операции вне периода отчёта: {outside} — "
                                f"на дашборде они попадут в свои даты.")
    outcome.warnings.extend(mapping_notes)
    if unmatched:
        outcome.warnings.append(
            f"Строк продаж/возвратов без товара 1С: {unmatched} (баркода нет ни в «Мэппинге» "
            f"Sync Admin, ни в справочнике 1С). Пока они есть, отчёт нельзя отправить в 1С.")
    # Для отчётов из API общее предупреждение не к месту: у WB за одну неделю
    # бывает два официальных отчёта («основной» и «по выкупам») с одним
    # периодом, и это разные продажи. Задвоение там грозит только от отчёта,
    # загруженного файлом, — про него предупреждает `store_api_report`.
    # Предварительные недели Lamoda не в счёт: отчёт комиссионера их заменяет
    # (`lamoda.apply_correction`), задвоения не будет.
    # Отчёт без продаж (только доставка KIT по API, комиссии Яндекс Пэй) с
    # отчётом о продажах не задваивается — про него предупреждать не о чем.
    overlaps = [r for r in overlapping_reports(db, report) if r.report_kind != lamoda.PROVISIONAL] \
        if source in ("file", "email") and report_kind != lamoda.PROVISIONAL else []
    if overlaps:
        with_goods = _with_goods(db, [report.id] + [r.id for r in overlaps])
        overlaps = [r for r in overlaps if r.id in with_goods] if report.id in with_goods else []
    if overlaps:
        # ПЕРВЫМ: сообщение режется по MAX_FLASH_CHARS, а задвоение продаж —
        # самое дорогое из всего, что тут можно сказать.
        outcome.warnings.insert(0, overlap_warning(overlaps))

    log_action(db, user, "sales_report_imported",
               f"#{report.id} {platform} {period_from:%Y-%m-%d}..{period_to:%Y-%m-%d} "
               f"источник={source} формат={parsed.format.code} строк={len(lines)} "
               f"без_товара={unmatched}")
    db.commit()
    db.refresh(report)
    outcome.report = report
    return outcome


def goods_of(lines: list[dict]) -> list[tuple]:
    """Продажи и возвраты строк единой формы — в виде, который сверяет `repeats.find`."""
    return [(line["srid"], line["barcode"], line["operation"], line["revenue"], line["quantity"])
            for line in lines if line["operation"] in v.GOODS_OPERATIONS]


def store_api_report(db: Session, *, platform: str, account_id: int | None, parsed: ParseResult,
                     period_from: date, period_to: date, user: str, report_number: str = "",
                     report_kind: str | None = None, declared_payout: int | None = None,
                     file_name: str = "", replace_unsent: bool = False,
                     source: str = "api", kind: str = "", superseded=None) -> ImportOutcome:
    """Отчёт, собранный не из файла отчёта: официальный из API или неделя
    еженедельного отчёта Lamoda (`lamoda.py`).

    Тот же отчёт второй раз не загружается (ключ по номеру или периоду). Отчёт,
    который площадка может дополнить задним числом (KIT: отказы при получении
    доезжают позже; Lamoda: каждое письмо повторяет 8 недель), с
    `replace_unsent=True` ЗАМЕНЯЕТСЯ свежим — но только пока он не ушёл в 1С:
    документ в 1С уже построен по прежним цифрам.

    Продажи, которые уже лежат в ДРУГОМ отчёте (`repeats.py`), — отказ
    (`outcome.repeat`), кроме отчётов, которые вызывающий сейчас заменит сам
    (`superseded(report)` — неотправленные продажи прежнего источника за те же
    недели)."""
    outcome = ImportOutcome()
    if parsed.errors:
        outcome.errors.extend(parsed.errors)
        return outcome
    onec_key = make_onec_key(platform, account_id, report_number, period_from, period_to, kind)
    existing = db.query(SalesReport).filter(SalesReport.onec_key == onec_key).first()
    if report_kind != lamoda.PROVISIONAL:
        found = repeats.find(db, platform=platform, account_id=account_id, numbered=bool(report_number),
                             lines=goods_of(parsed.lines),
                             exclude_ids=[existing.id] if existing is not None else [],
                             skip=superseded or (lambda report: False))
        if found:
            outcome.errors.append(found.text())
            outcome.repeat = True
            return outcome
    if existing is not None:
        same_kind = existing.source == source or (report_kind is not None
                                                  and existing.report_kind == report_kind)
        # «1С не ответила» (timeout) — документ в 1С МОГ создаться: заменив отчёт,
        # мы потеряли бы поздний OK, а следующая отправка под другим ключом
        # завела бы второй документ. Заменяем только то, что в 1С точно нет.
        replaceable = (replace_unsent and same_kind
                       and existing.onec_status in (OneCUploadStatus.none, OneCUploadStatus.failed))
        if not replaceable:
            outcome.skipped = True
            outcome.report = existing
            return outcome
        # Ввод оператора на странице отчёта (поступление на счёт, примечание)
        # переживает замену: ежедневный перезабор иначе стирал бы его молча.
        kept = {"bank_payout_kop": existing.bank_payout_kop, "note": existing.note}
        db.query(SalesReportLine).filter(SalesReportLine.report_id == existing.id) \
            .delete(synchronize_session=False)
        db.delete(existing)
        db.flush()
        outcome.replaced = True
    _store(db, outcome, platform=platform, account_id=account_id, parsed=parsed, user=user,
           number=report_number, period_from=period_from, period_to=period_to, source=source,
           file_name=file_name, file_sha256=None, declared_payout=declared_payout,
           report_kind=report_kind, onec_key=onec_key)
    if outcome.replaced and outcome.report is not None:
        for name, value in kept.items():
            if value is not None:
                setattr(outcome.report, name, value)
        db.commit()
    overlapping_files = [r for r in overlapping_reports(db, outcome.report) if r.source in ("file", "email")] \
        if report_kind != lamoda.PROVISIONAL else []
    if overlapping_files:
        with_goods = _with_goods(db, [outcome.report.id] + [r.id for r in overlapping_files])
        overlapping_files = [r for r in overlapping_files if r.id in with_goods] \
            if outcome.report.id in with_goods else []
    if overlapping_files:
        outcome.warnings.append(
            "За тот же период есть отчёты, загруженные файлом: "
            + ", ".join(f"#{r.id}" for r in overlapping_files[:5])
            + " — удалите их, иначе продажи посчитаются дважды.")
    return outcome

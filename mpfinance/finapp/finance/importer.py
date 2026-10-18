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

from sqlalchemy.orm import Session

from finapp.audit import log_action
from finapp import mapping
from finapp.finance import lamoda, statements, values as v
from finapp.finance.formats import ParseResult, parse_sheets
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
    skipped: bool = False        # такой отчёт уже есть — ничего не записано
    replaced: bool = False       # прежний (ещё не отправленный в 1С) отчёт заменён свежим
    # Еженедельный отчёт Lamoda раскладывается на несколько отчётов (по неделе).
    reports: list[SalesReport] = field(default_factory=list)


def make_onec_key(platform: str, account_id: int | None, report_number: str | None,
                  period_from: date, period_to: date) -> str:
    """Ключ документа в 1С. Один и тот же отчёт → один и тот же ключ, поэтому
    перезагрузка исправленного отчёта заменяет документ в 1С, а не дублирует.
    Только латиница, цифры и «-»: ключ едет в строке файла с разделителем «|»
    и в комментарий документа 1С."""
    number = re.sub(r"[^0-9A-Za-z_-]", "", report_number or "")[:40]
    if number:
        return f"SR-{platform}-{number}"
    return f"SR-{platform}-{account_id or 0}-{period_from:%Y%m%d}-{period_to:%Y%m%d}"


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


def resolve_uids(db: Session, report: SalesReport, notes: list[str] | None = None) -> int:
    """Проставляет строкам uid_1c (`mapping.resolve`: «Мэппинг» sync_admin, затем
    справочник 1С) и пересчитывает, сколько строк продаж/возвратов осталось без
    товара 1С. Вызывается при импорте и повторно — кнопкой «Пересопоставить» и
    перед отправкой в 1С: мэппинг в sync_admin могли дополнить."""
    lines = db.query(SalesReportLine).filter(SalesReportLine.report_id == report.id).all()
    found = mapping.resolve(db, (line.barcode for line in lines))
    if notes is not None and found.note:
        notes.append(found.note)
    if notes is not None and found.ambiguous:
        notes.append("Баркоды у нескольких товаров в справочнике 1С (не сопоставлены): "
                     + ", ".join(sorted(found.ambiguous)[:5]) + ".")
    unmatched = 0
    for line in lines:
        line.uid_1c = found.uid_by_barcode.get(line.barcode) if line.barcode else None
        if line.operation in v.GOODS_OPERATIONS and line.uid_1c is None:
            unmatched += 1
    report.unmatched_lines = unmatched
    return unmatched


def overlapping_reports(db: Session, report: SalesReport) -> list[SalesReport]:
    """Другие отчёты той же площадки и кабинета с пересекающимся периодом —
    риск посчитать одни и те же продажи дважды (недельный + месячный отчёт)."""
    return db.query(SalesReport).filter(
        SalesReport.id != report.id,
        SalesReport.platform == report.platform,
        SalesReport.account_id.is_(None) if report.account_id is None
        else SalesReport.account_id == report.account_id,
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

    parsed = parse_sheets(sheets, platform)
    if parsed.errors:
        outcome.errors.extend(parsed.errors)
        if parsed.errors_total > len(parsed.errors):
            outcome.errors.append(f"… и ещё ошибок: {parsed.errors_total - len(parsed.errors)}.")
        if parsed.format is not None and parsed.errors_total:
            outcome.errors.append("Отчёт не сохранён: строки с ошибками изменили бы итоги. "
                                  "Исправьте файл и загрузите снова.")
        return outcome

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

    onec_key = make_onec_key(platform, account_id, number, period_from, period_to)
    same_key = db.query(SalesReport).filter(SalesReport.onec_key == onec_key).first()
    if same_key is not None:
        outcome.errors.append(
            f"Отчёт с тем же {'номером' if number else 'периодом и кабинетом'} уже загружен — "
            f"#{same_key.id}. Чтобы заменить, удалите старый"
            + ("" if number else " или укажите номер отчёта площадки, если это другой отчёт")
            + ".")
        return outcome

    _store(db, outcome, platform=platform, account_id=account_id, parsed=parsed, user=user,
           number=number, period_from=period_from, period_to=period_to, source=source,
           file_name=filename, file_sha256=sha256,
           declared_payout=parsed.declared_payout if parsed.declared_payout is not None
           else statements.declared_for(db, platform, number),
           report_kind=None, onec_key=onec_key)
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
    db.bulk_insert_mappings(SalesReportLine, mappings)
    db.flush()

    mapping_notes: list[str] = []
    unmatched = resolve_uids(db, report, mapping_notes)

    if parsed.warnings:
        outcome.warnings.extend(parsed.warnings)
    if declared_payout is not None:
        computed = sum(v.line_total(line) for line in lines)
        if computed != declared_payout:
            outcome.warnings.append(
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
    overlaps = [r for r in overlapping_reports(db, report) if r.report_kind != lamoda.PROVISIONAL] \
        if source in ("file", "email") and report_kind != lamoda.PROVISIONAL else []
    if overlaps:
        outcome.warnings.append(
            "Период пересекается с уже загруженными отчётами этой площадки: "
            + ", ".join(f"#{r.id} ({r.period_from:%d.%m}–{r.period_to:%d.%m.%Y})" for r in overlaps[:5])
            + ". Если это один и тот же период — продажи посчитаются дважды.")

    log_action(db, user, "sales_report_imported",
               f"#{report.id} {platform} {period_from:%Y-%m-%d}..{period_to:%Y-%m-%d} "
               f"источник={source} формат={parsed.format.code} строк={len(lines)} "
               f"без_товара={unmatched}")
    db.commit()
    db.refresh(report)
    outcome.report = report
    return outcome


def store_api_report(db: Session, *, platform: str, account_id: int | None, parsed: ParseResult,
                     period_from: date, period_to: date, user: str, report_number: str = "",
                     report_kind: str | None = None, declared_payout: int | None = None,
                     file_name: str = "", replace_unsent: bool = False,
                     source: str = "api") -> ImportOutcome:
    """Отчёт, собранный не из файла отчёта: официальный из API или неделя
    еженедельного отчёта Lamoda (`lamoda.py`).

    Тот же отчёт второй раз не загружается (ключ по номеру или периоду). Отчёт,
    который площадка может дополнить задним числом (KIT: отказы при получении
    доезжают позже; Lamoda: каждое письмо повторяет 8 недель), с
    `replace_unsent=True` ЗАМЕНЯЕТСЯ свежим — но только пока он не ушёл в 1С:
    документ в 1С уже построен по прежним цифрам."""
    outcome = ImportOutcome()
    if parsed.errors:
        outcome.errors.extend(parsed.errors)
        return outcome
    onec_key = make_onec_key(platform, account_id, report_number, period_from, period_to)
    existing = db.query(SalesReport).filter(SalesReport.onec_key == onec_key).first()
    if existing is not None:
        same_kind = existing.source == source or (report_kind is not None
                                                  and existing.report_kind == report_kind)
        replaceable = (replace_unsent and same_kind
                       and existing.onec_status in (OneCUploadStatus.none, OneCUploadStatus.failed,
                                                    OneCUploadStatus.timeout))
        if not replaceable:
            outcome.skipped = True
            outcome.report = existing
            return outcome
        db.query(SalesReportLine).filter(SalesReportLine.report_id == existing.id) \
            .delete(synchronize_session=False)
        db.delete(existing)
        db.flush()
        outcome.replaced = True
    _store(db, outcome, platform=platform, account_id=account_id, parsed=parsed, user=user,
           number=report_number, period_from=period_from, period_to=period_to, source=source,
           file_name=file_name, file_sha256=None, declared_payout=declared_payout,
           report_kind=report_kind, onec_key=onec_key)
    overlapping_files = [r for r in overlapping_reports(db, outcome.report) if r.source in ("file", "email")] \
        if report_kind != lamoda.PROVISIONAL else []
    if overlapping_files:
        outcome.warnings.append(
            "За тот же период есть отчёты, загруженные файлом: "
            + ", ".join(f"#{r.id}" for r in overlapping_files[:5])
            + " — удалите их, иначе продажи посчитаются дважды.")
    return outcome

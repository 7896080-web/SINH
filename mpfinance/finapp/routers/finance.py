"""Страницы программы: дашборд по площадкам, отчёты площадок (загрузка, сверка,
выгрузка в 1С), юнит-экономика, себестоимость и настройки расчёта.

Вся логика — в `finapp/finance/`; здесь только разбор форм и шаблоны.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from finapp import onec
from finapp.audit import log_action
from finapp.database import get_db
from finapp.deps import get_current_user
from finapp.excel_utils import ExcelReadError, build_xlsx_response, read_upload, read_xlsx_rows
from finapp.finance import charts, fetch, lamoda, statements, values as v
from finapp.finance.economics import (Filters, Totals, load_lines, platform_summary, trend,
                                   unit_economics)
from finapp.finance.formats import FORMATS
from finapp.finance.readers import ReportReadError, read_sheets
from finapp.finance.importer import import_report, resolve_uids, sales_overlaps
from finapp.finance.sales_1c import IN_FLIGHT, build_plan, request_upload, upload_problems
from finapp.finance.rates import RateBook
from finapp.finance.reconcile import breakdown, money_check, order_check, unmatched_barcodes
from finapp.finance.settings import (DEFAULT_WAREHOUSES, KIT_SALES_SOURCES, TAX_MODES, WAREHOUSE_KEYS,
                                  effective_rate, load_settings, raw_settings, save_setting, tax_text)
from finapp.flash import pop_flash, set_flash
from finapp.models import (Barcode, OnecTask, OneCUploadStatus, PlatformAccount, PlatformStatement, Product,
                           ProductCost, ReportFetch, SalesReport, SalesReportLine, User)
from finapp.templating import ONEC_TASK_LABELS, templates
from finapp.timeutils import today_local

router = APIRouter()

ONEC_LABELS = {
    OneCUploadStatus.none: "не отправлялся",
    OneCUploadStatus.pending: "ждёт отправки в 1С",
    OneCUploadStatus.sent: "отправлен, ждём ответа 1С",
    OneCUploadStatus.done: "загружен в 1С",
    OneCUploadStatus.failed: "ошибка 1С",
    OneCUploadStatus.timeout: "1С не ответила",
}
COSTS_LIMIT = 300
REPORTS_LIMIT = 200
FETCH_LOG_LIMIT = 20
# Самая ранняя дата себестоимости, если в файле она не указана: «действует
# всегда», иначе продажи до даты импорта остались бы без себестоимости.
COST_DEFAULT_FROM = date(2000, 1, 1)


def _ctx(request: Request, user: User, active: str, **extra) -> dict:
    return {"request": request, "current_user": user, "active_page": active,
            "platform_labels": v.PLATFORM_LABELS, "platform_colors": charts.PLATFORM_COLORS,
            "operation_labels": v.OPERATION_LABELS, "flash": pop_flash(request), **extra}


def _parse_day(text: str) -> date | None:
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


PRESETS = (("30d", "30 дней"), ("month", "Этот месяц"), ("prev_month", "Прошлый месяц"),
           ("quarter", "90 дней"), ("year", "Год"))


def _period(preset: str, date_from: str, date_to: str) -> tuple[date, date, str]:
    """Период фильтра: явные даты важнее пресета; по умолчанию — 30 дней."""
    today = today_local()
    d_from, d_to = _parse_day(date_from), _parse_day(date_to)
    if d_from and d_to:
        if d_from > d_to:
            d_from, d_to = d_to, d_from
        return d_from, d_to, "custom"
    if preset == "month":
        return today.replace(day=1), today, preset
    if preset == "prev_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last, preset
    if preset == "quarter":
        return today - timedelta(days=89), today, preset
    if preset == "year":
        return today - timedelta(days=364), today, preset
    return today - timedelta(days=29), today, "30d"


def _accounts(db: Session) -> list[PlatformAccount]:
    return db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name).all()


def _int_or_none(text: str) -> int | None:
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


# ======================================================================
# Дашборд
# ======================================================================

@router.get("/finance", response_class=HTMLResponse)
def finance_dashboard(
    request: Request, preset: str = Query(""), date_from: str = Query(""), date_to: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    d_from, d_to, preset = _period(preset, date_from, date_to)
    settings = load_settings(db)
    filters = Filters(d_from, d_to)

    lines = load_lines(db, filters)
    summary = platform_summary(db, filters, settings, lines=lines)
    platforms = [p for p in v.REPORT_PLATFORMS if p in summary]
    total = None
    if summary:
        total = Totals()
        for item in summary.values():
            total.add(item)

    bucket = "week" if (d_to - d_from).days <= 120 else "month"
    trend_rows = trend(db, filters, settings, bucket, lines=lines)
    chart = charts.stacked_columns([(s, p, t.revenue) for s, p, t in trend_rows], platforms, bucket) \
        if trend_rows else None

    articles, _unassigned, _total, _ = unit_economics(db, filters, settings, group_by="article",
                                                      lines=lines)
    top = [g for g in articles if g.totals.profit > 0][:10]
    losers = sorted([g for g in articles if g.totals.profit < 0], key=lambda g: g.totals.profit)[:5]

    # «Не загружено в 1С» — только отчёты, которые в 1С уходят: предварительные
    # недели Lamoda, доставка KIT по API и детализация Яндекс Пэй не уходят
    # никогда, и сигнал горел бы всегда.
    # coalesce обязателен: у большинства отчётов вид пуст (NULL), а NOT (NULL IN …)
    # в SQL — NULL, и такие отчёты выпали бы из счёта молча.
    never_to_1c = or_(func.coalesce(SalesReport.report_kind, "").in_([lamoda.PROVISIONAL, fetch.KIT_DELIVERY_KIND]),
                      SalesReport.source_format == "kit_yandex_pay")
    not_in_1c = db.query(SalesReport).filter(
        SalesReport.period_from <= d_to, SalesReport.period_to >= d_from, ~never_to_1c,
        SalesReport.onec_status.in_([OneCUploadStatus.none, OneCUploadStatus.failed,
                                     OneCUploadStatus.timeout]),
    ).count()
    stale_fetches = [f for f in fetch.account_freshness(db) if f["stale"]]
    reports_in_period = db.query(SalesReport).filter(
        SalesReport.period_from <= d_to, SalesReport.period_to >= d_from).count()

    return templates.TemplateResponse(request, "finance_dashboard.html", _ctx(
        request, user, "finance",
        d_from=d_from, d_to=d_to, preset=preset, presets=PRESETS,
        summary=summary, platforms=platforms, total=total, chart=chart, bucket=bucket,
        trend_rows=trend_rows, top=top, losers=losers, settings=settings,
        tax_modes=TAX_MODES, not_in_1c=not_in_1c, reports_in_period=reports_in_period,
        tax_text=tax_text(settings), tax_effective=effective_rate(settings),
        meter_width=charts.meter_width, stale_fetches=stale_fetches,
    ))


# ======================================================================
# Отчёты площадок
# ======================================================================

@router.get("/finance/reports", response_class=HTMLResponse)
def reports_page(
    request: Request, platform: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(SalesReport)
    if platform in v.REPORT_PLATFORMS:
        query = query.filter(SalesReport.platform == platform)
    reports = query.order_by(SalesReport.period_to.desc(), SalesReport.id.desc()) \
        .limit(REPORTS_LIMIT).all()

    totals = {}
    if reports:
        ids = [r.id for r in reports]
        rows = db.query(
            SalesReportLine.report_id,
            func.sum(SalesReportLine.revenue_kop),
            func.sum(SalesReportLine.payout_kop - SalesReportLine.logistics_kop
                     - SalesReportLine.storage_kop - SalesReportLine.penalty_kop
                     - SalesReportLine.acceptance_kop - SalesReportLine.deduction_kop
                     + SalesReportLine.compensation_kop),
        ).filter(SalesReportLine.report_id.in_(ids)).group_by(SalesReportLine.report_id).all()
        totals = {rid: (int(rev or 0), int(pay or 0)) for rid, rev, pay in rows}

    accounts = _accounts(db)
    return templates.TemplateResponse(request, "finance_reports.html", _ctx(
        request, user, "finance-reports",
        reports=reports, totals=totals, accounts=accounts, platform=platform,
        platforms=v.REPORT_PLATFORMS, onec_labels=ONEC_LABELS, formats=FORMATS,
        api_accounts=[a for a in accounts if a.platform in ("wb", "kit")],
        fetches=db.query(ReportFetch).order_by(ReportFetch.id.desc()).limit(FETCH_LOG_LIMIT).all(),
        fetch_kinds=fetch.KINDS, auto_fetch=fetch.auto_enabled(db),
        default_from=(today_local() - timedelta(days=28)).isoformat(), today=today_local().isoformat(),
    ))


@router.post("/finance/fetch")
def request_api_fetch(
    request: Request, kind: str = Form(""), account_id: str = Form(""),
    date_from: str = Form(""), date_to: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Заявка на загрузку официальных отчётов по API. Сама загрузка — в фоновом
    задании: WB отдаёт один запрос в минуту, держать на этом страницу нельзя."""
    back = "/finance/reports#api"
    job, error = fetch.request_fetch(db, kind, user.username, _int_or_none(account_id),
                                     _parse_day(date_from), _parse_day(date_to))
    if job is None:
        db.rollback()
        set_flash(request, error, "warn")
        return RedirectResponse(back, status_code=303)
    log_action(db, user.username, "finance_fetch_requested",
               f"#{job.id} {kind} кабинет={job.account_id or '—'} "
               f"{job.date_from or ''}..{job.date_to or ''}")
    db.commit()
    set_flash(request, f"Заявка #{job.id} поставлена: {fetch.KINDS[kind]}. Её разберёт фоновое "
                       f"задание в течение пяти минут; ход — в журнале загрузок.", "good")
    return RedirectResponse(back, status_code=303)


@router.post("/finance/reports/upload")
def reports_upload(
    request: Request, platform: str = Form(""), account_id: str = Form(""),
    report_number: str = Form(""), period_from: str = Form(""), period_to: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    d_from, d_to = _parse_day(period_from), _parse_day(period_to)
    if (period_from.strip() and d_from is None) or (period_to.strip() and d_to is None):
        set_flash(request, "Даты периода — в формате ГГГГ-ММ-ДД.", "warn")
        return RedirectResponse("/finance/reports", status_code=303)

    # Предел размера: огромный файл, выбранный по ошибке, занял бы память
    # единственного веб-процесса целиком.
    try:
        data = read_upload(file.file)
    except ExcelReadError as e:
        set_flash(request, f"Отчёт не загружен. {e}", "warn")
        return RedirectResponse("/finance/reports", status_code=303)
    outcome = import_report(
        db, platform=platform, file_bytes=data, filename=file.filename or "",
        user=user.username, account_id=_int_or_none(account_id), report_number=report_number,
        period_from=d_from, period_to=d_to,
    )
    if outcome.report is None:
        db.rollback()
        set_flash(request, "Отчёт не загружен. " + " ".join(outcome.errors), "warn")
        return RedirectResponse("/finance/reports", status_code=303)

    if outcome.reports:
        # Еженедельный отчёт Lamoda — несколько предварительных недель сразу.
        set_flash(request, " ".join(outcome.warnings), "good")
        return RedirectResponse("/finance/reports?platform=lamoda", status_code=303)
    report = outcome.report
    message = (f"Отчёт загружен: {report.lines_count} строк, период "
               f"{report.period_from:%d.%m.%Y}–{report.period_to:%d.%m.%Y}.")
    # Предупреждения — раньше справки: сообщение режется по MAX_FLASH_CHARS.
    tail = " ".join(outcome.warnings + outcome.notes)
    set_flash(request, (message + " " + tail).strip(), "warn" if outcome.warnings else "good")
    return RedirectResponse(f"/finance/reports/{report.id}", status_code=303)


@router.get("/finance/reports/template")
def reports_template(user: User = Depends(get_current_user)):
    """Пустая единая форма — для отчётов площадок, которые мы не умеем читать
    сами (Ozon, нестандартная выгрузка). Тип операции — одно из значений
    OPERATION_LABELS; знак у возвратов система проставит сама."""
    headers = [c.header for c in v.UNIFIED_COLUMNS]
    return build_xlsx_response(headers, [], "единая_форма_отчёта.xlsx")


def _get_report(db: Session, report_id: int):
    return db.query(SalesReport).filter(SalesReport.id == report_id).first()


@router.get("/finance/reports/{report_id}", response_class=HTMLResponse)
def report_detail(
    request: Request, report_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    report = _get_report(db, report_id)
    if report is None:
        set_flash(request, f"Отчёт #{report_id} не найден.", "warn")
        return RedirectResponse("/finance/reports", status_code=303)

    operations, total = breakdown(db, report)
    settings = load_settings(db)
    warehouse = settings.warehouses.get(report.platform, "")
    plan = build_plan(db, report)
    return templates.TemplateResponse(request, "finance_report.html", _ctx(
        request, user, "finance-reports",
        report=report, operations=operations, total=total, money_chk=money_check(report, total),
        orders=order_check(db, report), unmatched=unmatched_barcodes(db, report),
        plan=plan, plan_sale=plan.totals("SALE"), plan_return=plan.totals("RETURN"),
        warehouse=warehouse, problems=upload_problems(db, report, warehouse),
        onec_labels=ONEC_LABELS, in_flight=report.onec_status in IN_FLIGHT,
        overlaps=sales_overlaps(db, report),
    ))


@router.post("/finance/reports/{report_id}/payouts")
def report_payouts(
    request: Request, report_id: int, declared: str = Form(""), bank: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    report = _get_report(db, report_id)
    if report is None:
        return RedirectResponse("/finance/reports", status_code=303)
    try:
        report.declared_payout_kop = v.parse_money(declared)
        report.bank_payout_kop = v.parse_money(bank)
    except v.ValueParseError:
        db.rollback()
        set_flash(request, "Суммы — числом в рублях, например 125 430,50.", "warn")
        return RedirectResponse(f"/finance/reports/{report_id}", status_code=303)
    report.note = note.strip() or None
    log_action(db, user.username, "sales_report_payouts",
               f"#{report.id} площадка={declared or '—'} банк={bank or '—'}")
    db.commit()
    set_flash(request, "Суммы для сверки сохранены.", "good")
    return RedirectResponse(f"/finance/reports/{report_id}#money", status_code=303)


@router.post("/finance/reports/{report_id}/remap")
def report_remap(
    request: Request, report_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    report = _get_report(db, report_id)
    if report is None:
        return RedirectResponse("/finance/reports", status_code=303)
    before = report.unmatched_lines
    after = resolve_uids(db, report)
    db.commit()
    message = f"Баркоды пересопоставлены: без товара 1С было {before}, стало {after}."
    if report.onec_status in (OneCUploadStatus.done, OneCUploadStatus.sent, OneCUploadStatus.timeout):
        # В 1С уже ушло по прежнему сопоставлению: если товары сменились,
        # документ в 1С устарел, и сам он не обновится.
        message += (" Отчёт уже отправлялся в 1С по прежнему сопоставлению — если товары изменились, "
                    "отправьте его повторно: 1С перезаполнит тот же документ.")
    set_flash(request, message, "good" if after == 0 and report.onec_status == OneCUploadStatus.none else "warn")
    return RedirectResponse(f"/finance/reports/{report_id}", status_code=303)


@router.post("/finance/reports/{report_id}/send-1c")
def report_send_1c(
    request: Request, report_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    report = _get_report(db, report_id)
    if report is None:
        return RedirectResponse("/finance/reports", status_code=303)
    problems = request_upload(db, report, user.username)
    if problems:
        set_flash(request, "В 1С не отправлен: " + " ".join(problems), "warn")
    else:
        set_flash(request, "Отчёт поставлен в очередь в 1С — уйдёт с ближайшим файлом заданий "
                           "(раз в минуту), ответ появится на этой странице.", "good")
    return RedirectResponse(f"/finance/reports/{report_id}#onec", status_code=303)


@router.post("/finance/reports/{report_id}/delete")
def report_delete(
    request: Request, report_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    report = _get_report(db, report_id)
    if report is None:
        return RedirectResponse("/finance/reports", status_code=303)
    if report.onec_status in IN_FLIGHT:
        set_flash(request, "Отчёт сейчас в пути в 1С — удалить можно после ответа обработки.", "warn")
        return RedirectResponse(f"/finance/reports/{report_id}", status_code=303)
    message = f"Отчёт #{report.id} удалён."
    if report.onec_status == OneCUploadStatus.done:
        message += (" Документы в 1С остались: загрузите исправленный отчёт и отправьте его — "
                    "1С перезаполнит те же документы (если они не проведены).")
    log_action(db, user.username, "sales_report_deleted",
               f"#{report.id} {report.platform} {report.period_from:%Y-%m-%d}..{report.period_to:%Y-%m-%d} "
               f"1С={report.onec_status.value}")
    # Строки — явным запросом, а не каскадом БД: ON DELETE CASCADE работает в
    # PostgreSQL, но в SQLite (разработка, тесты) внешние ключи по умолчанию
    # выключены, и строки удалённого отчёта остались бы в юнит-экономике.
    db.query(SalesReportLine).filter(SalesReportLine.report_id == report.id) \
        .delete(synchronize_session=False)
    db.delete(report)
    db.commit()
    set_flash(request, message, "good")
    return RedirectResponse("/finance/reports", status_code=303)


@router.get("/finance/reports/{report_id}/export")
def report_export(
    report_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Отчёт в единой форме — одна раскладка для всех площадок."""
    report = _get_report(db, report_id)
    if report is None:
        return RedirectResponse("/finance/reports", status_code=303)
    account_name = report.account.name if report.account else ""
    headers = (["Площадка", "Кабинет", "Номер отчёта", "Период с", "Период по"]
               + [c.header for c in v.UNIFIED_COLUMNS] + ["ID_1С", "Итого к перечислению"])
    data = []
    lines = db.query(SalesReportLine).filter(SalesReportLine.report_id == report.id) \
        .order_by(SalesReportLine.row_no, SalesReportLine.id).all()
    for line in lines:
        row = [v.PLATFORM_LABELS[report.platform], account_name, report.report_number or "",
               report.period_from, report.period_to]
        for column in v.UNIFIED_COLUMNS:
            if column.field == "operation":
                row.append(v.OPERATION_LABELS.get(line.operation, line.operation))
            elif column.field in v.MONEY_FIELDS:
                row.append(v.kop_to_rub(getattr(line, f"{column.field}_kop")))
            else:
                row.append(getattr(line, column.field))
        row += [line.uid_1c or "", v.kop_to_rub(line.total_kop)]
        data.append(row)
    return build_xlsx_response(
        headers, data,
        f"отчёт_{report.platform}_{report.period_from:%Y-%m-%d}_{report.period_to:%Y-%m-%d}.xlsx")


# ======================================================================
# Юнит-экономика
# ======================================================================

GROUPS = {"article": "по артикулу", "sku": "по SKU (размер-цвет)"}


def _ue_params(preset, date_from, date_to, platform, account_id, group_by):
    d_from, d_to, preset = _period(preset, date_from, date_to)
    platform = platform if platform in v.REPORT_PLATFORMS else ""
    group_by = group_by if group_by in GROUPS else "article"
    return d_from, d_to, preset, platform, _int_or_none(account_id), group_by


@router.get("/finance/unit-economics", response_class=HTMLResponse)
def unit_economics_page(
    request: Request, preset: str = Query(""), date_from: str = Query(""), date_to: str = Query(""),
    platform: str = Query(""), account_id: str = Query(""), group_by: str = Query("article"),
    allocate: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    d_from, d_to, preset, platform, account, group_by = _ue_params(
        preset, date_from, date_to, platform, account_id, group_by)
    settings = load_settings(db)
    rows, unassigned, total, allocated = unit_economics(
        db, Filters(d_from, d_to, platform or None, account), settings, group_by, allocate)
    return templates.TemplateResponse(request, "finance_unit_economics.html", _ctx(
        request, user, "finance-ue",
        rows=rows, unassigned=unassigned, total=total, allocated=allocated, allocate=allocate,
        d_from=d_from, d_to=d_to, preset=preset, presets=PRESETS, platform=platform,
        account_id=account, accounts=_accounts(db), group_by=group_by, groups=GROUPS,
        platforms=v.REPORT_PLATFORMS, settings=settings, tax_modes=TAX_MODES, tax_text=tax_text(settings),
        tax_effective=effective_rate(settings),
    ))


@router.get("/finance/unit-economics/export")
def unit_economics_export(
    preset: str = Query(""), date_from: str = Query(""), date_to: str = Query(""),
    platform: str = Query(""), account_id: str = Query(""), group_by: str = Query("article"),
    allocate: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    d_from, d_to, preset, platform, account, group_by = _ue_params(
        preset, date_from, date_to, platform, account_id, group_by)
    settings = load_settings(db)
    rows, unassigned, total, _ = unit_economics(
        db, Filters(d_from, d_to, platform or None, account), settings, group_by, allocate)

    headers = ["Артикул", "Наименование", "Размер", "Цвет", "ID_1С", "Площадки", "Продано, шт",
               "Возвраты, шт", "Чистые продажи, шт", "Выручка", "Комиссия площадки", "Логистика",
               "Хранение", "Штрафы, приёмка, удержания − компенсации", "К перечислению",
               "Себестоимость", "Доп. расходы", "Налог УСН", "НДС (невозвратный)", "Прибыль",
               "Маржинальность, %", "ROI, %",
               "Прибыль на единицу", "Продано без себестоимости, шт", "Нет в 1С"]

    def pct(value):
        return round(value * 100, 2) if value is not None else None

    def line(title, article, size, color, uid, platforms_text, t, matched):
        return [article, title, size, color, uid or "", platforms_text, t.sold_qty, t.returned_qty,
                t.net_qty, v.kop_to_rub(t.revenue), v.kop_to_rub(t.commission),
                v.kop_to_rub(t.logistics), v.kop_to_rub(t.storage), v.kop_to_rub(t.other_costs),
                v.kop_to_rub(t.total_payout), v.kop_to_rub(t.cogs), v.kop_to_rub(t.extra),
                v.kop_to_rub(t.tax), v.kop_to_rub(t.vat), v.kop_to_rub(t.profit), pct(t.margin), pct(t.roi),
                v.kop_to_rub(t.profit_per_unit), t.cost_missing_qty, "" if matched else "да"]

    data = [line(g.title, g.article, g.size, g.color, g.uid_1c,
                 ", ".join(v.PLATFORM_LABELS.get(p, p) for p in sorted(g.platforms)),
                 g.totals, g.matched) for g in rows]
    data.append(line("Расходы площадок, не привязанные к товару", "", "", "", None, "",
                     unassigned, True))
    data.append(line("ИТОГО", "", "", "", None, "", total, True))
    return build_xlsx_response(headers, data,
                               f"юнит_экономика_{d_from:%Y-%m-%d}_{d_to:%Y-%m-%d}.xlsx")


# ======================================================================
# Себестоимость и настройки
# ======================================================================

def _current_costs(db: Session, uids: list[str]) -> dict[str, ProductCost]:
    result = {}
    today = today_local()
    for start in range(0, len(uids), 500):
        for cost in db.query(ProductCost).filter(ProductCost.uid_1c.in_(uids[start:start + 500])) \
                .order_by(ProductCost.valid_from):
            if cost.valid_from <= today or cost.uid_1c not in result:
                result[cost.uid_1c] = cost
    return result


@router.get("/finance/costs", response_class=HTMLResponse)
def costs_page(
    request: Request, q: str = Query(""), missing: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(Product)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(Product.article.ilike(like), Product.name.ilike(like),
                                 Product.uid_1c.ilike(like)))
    if missing:
        query = query.filter(~Product.uid_1c.in_(db.query(ProductCost.uid_1c)))
    total = query.count()
    products = query.order_by(Product.article, Product.size).limit(COSTS_LIMIT).all()
    costs = _current_costs(db, [p.uid_1c for p in products])
    with_cost = db.query(func.count(func.distinct(ProductCost.uid_1c))).scalar() or 0
    rates = RateBook(db)
    return templates.TemplateResponse(request, "finance_costs.html", _ctx(
        request, user, "finance-costs",
        products=products, costs=costs, total=total, limit=COSTS_LIMIT, q=q, missing=missing,
        with_cost=with_cost, products_total=db.query(Product).count(),
        raw=raw_settings(db), tax_modes=TAX_MODES, warehouse_keys=WAREHOUSE_KEYS,
        tax_text=tax_text(load_settings(db)), tax_effective=effective_rate(load_settings(db)),
        kit_sales_sources=KIT_SALES_SOURCES,
        latest_rate=rates.latest(),
        cost_jobs=db.query(OnecTask).filter(OnecTask.command == "EXPORT_COST_PRICES")
        .order_by(OnecTask.id.desc()).limit(5).all(),
        onec_task_labels=ONEC_TASK_LABELS,
    ))


@router.post("/finance/costs/request-1c")
def costs_request_1c(request: Request, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    try:
        task = onec.enqueue(db, "EXPORT_COST_PRICES", user.username)
    except onec.OnecError as e:
        db.rollback()
        set_flash(request, f"Не поставлено: {e}.", "warn")
        return RedirectResponse("/finance/costs", status_code=303)
    log_action(db, user.username, "onec_costs_requested", task.key)
    db.commit()
    set_flash(request, f"Запрос себестоимости поставлен ({task.key}). Ответ придёт, когда отработает "
                       f"обработка 1С.", "good")
    return RedirectResponse("/finance/costs", status_code=303)


@router.get("/finance/costs/export")
def costs_export(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Все товары с текущей себестоимостью — заполнить в Excel и загрузить обратно."""
    products = db.query(Product).order_by(Product.article, Product.size).all()
    costs = _current_costs(db, [p.uid_1c for p in products])
    first_barcode = dict(db.query(Barcode.uid_1c, func.min(Barcode.barcode))
                         .group_by(Barcode.uid_1c).all())
    # В колонку «Себестоимость» выгружаются только рубли: она же колонка
    # загрузки, и долларовая цена из 1С, вернувшись тем же файлом, стала бы
    # рублёвой. Себестоимость 1С показана отдельной колонкой для справки.
    headers = ["ID_1С", "Артикул", "Наименование", "Размер", "Цвет", "Баркод",
               "Себестоимость", "Действует с", "Себестоимость 1С, $ (справочно)"]
    data = []
    for p in products:
        cost = costs.get(p.uid_1c)
        rub = cost if cost and cost.currency == "RUB" else None
        usd = cost if cost and cost.currency == "USD" else None
        data.append([p.uid_1c, p.article, p.name, p.size, p.color, first_barcode.get(p.uid_1c, ""),
                     v.kop_to_rub(rub.cost_minor) if rub else None,
                     rub.valid_from if rub and rub.valid_from != COST_DEFAULT_FROM else None,
                     v.kop_to_rub(usd.cost_minor) if usd else None])
    return build_xlsx_response(headers, data, "себестоимость.xlsx")


@router.post("/finance/costs/import")
def costs_import(
    request: Request, file: UploadFile = File(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Себестоимость из Excel. Товар ищется по ID_1С, иначе по баркоду, иначе по
    артикулу — тогда цена ставится всем размерам артикула (у одежды себестоимость
    обычно одна на модель). Пустая себестоимость — строка пропускается."""
    try:
        rows = read_xlsx_rows(read_upload(file.file))
    except ExcelReadError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse("/finance/costs", status_code=303)

    updated, skipped, errors = 0, 0, []
    for i, row in enumerate(rows, start=2):
        raw_cost = row.get("Себестоимость")
        if raw_cost in (None, ""):
            skipped += 1
            continue
        try:
            cost_kop = v.parse_money(raw_cost)   # Excel — всегда рубли
            valid_from = v.parse_date(row.get("Действует с")) or COST_DEFAULT_FROM
        except v.ValueParseError as e:
            errors.append(f"строка {i}: {e}")
            continue
        if cost_kop is None or cost_kop < 0:
            errors.append(f"строка {i}: себестоимость должна быть не меньше нуля")
            continue

        uid = v.parse_text(row.get("ID_1С"))
        barcode = v.parse_text(row.get("Баркод"))
        article = v.parse_text(row.get("Артикул"))
        if uid:
            uids = [uid] if db.query(Product).filter(Product.uid_1c == uid).first() else []
        elif barcode:
            found = db.query(Barcode).filter(Barcode.barcode == barcode).first()
            uids = [found.uid_1c] if found else []
        elif article:
            uids = [p.uid_1c for p in db.query(Product).filter(Product.article == article)]
        else:
            errors.append(f"строка {i}: нет ни ID_1С, ни баркода, ни артикула")
            continue
        if not uids:
            errors.append(f"строка {i}: товар не найден ({uid or barcode or article})")
            continue

        for target in uids:
            existing = db.query(ProductCost).filter(ProductCost.uid_1c == target,
                                                    ProductCost.valid_from == valid_from).first()
            if existing is None:
                db.add(ProductCost(uid_1c=target, valid_from=valid_from, cost_minor=cost_kop,
                                   currency="RUB", source="excel", updated_by=user.username))
                updated += 1
            elif (existing.cost_minor, existing.currency) != (cost_kop, "RUB"):
                existing.cost_minor = cost_kop
                existing.currency = "RUB"
                existing.source = "excel"
                existing.updated_by = user.username
                updated += 1
        db.flush()

    log_action(db, user.username, "product_costs_import",
               f"обновлено {updated}, пропущено {skipped}, ошибок {len(errors)}")
    db.commit()
    message = f"Себестоимость обновлена у {updated} товаров. Пустых строк пропущено: {skipped}."
    if errors:
        more = f" и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        set_flash(request, message + f" Ошибок: {len(errors)} ({'; '.join(errors[:5])}{more}).", "warn")
    else:
        set_flash(request, message, "good")
    return RedirectResponse("/finance/costs", status_code=303)


@router.post("/finance/settings")
def finance_settings_save(
    request: Request, FIN_TAX_MODE: str = Form("revenue"), FIN_TAX_RATE: str = Form("6"),
    FIN_EXTRA_COST_PER_UNIT: str = Form("0"), FIN_USD_RATE_MANUAL: str = Form(""),
    FIN_AUTO_FETCH: str = Form("1"), FIN_KIT_CRM_COMMISSION: str = Form("3.5"), FIN_VAT_RATE: str = Form("0"),
    FIN_KIT_SALES_SOURCE: str = Form("crm"),
    FIN_WAREHOUSE_WB: str = Form(""), FIN_WAREHOUSE_OZON: str = Form(""),
    FIN_WAREHOUSE_KIT: str = Form(""), FIN_WAREHOUSE_LAMODA: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    errors = []
    if FIN_TAX_MODE not in TAX_MODES:
        errors.append("неизвестный режим налога")
    if FIN_AUTO_FETCH not in ("0", "1"):
        errors.append("автозагрузка — 0 или 1")
    if FIN_KIT_SALES_SOURCE not in KIT_SALES_SOURCES:
        errors.append("неизвестный источник продаж KIT")
    for label, text, upper, allow_empty in (
            ("ставка налога", FIN_TAX_RATE, Decimal(100), False),
            ("доп. расходы на единицу", FIN_EXTRA_COST_PER_UNIT, None, False),
            ("комиссия AWER CRM", FIN_KIT_CRM_COMMISSION, Decimal(100), False),
            ("ставка НДС", FIN_VAT_RATE, Decimal(100), False),
            ("курс доллара", FIN_USD_RATE_MANUAL, Decimal(100000), True)):
        cleaned = (text or "").replace(",", ".").replace(" ", "").strip()
        if not cleaned and allow_empty:
            continue
        try:
            number = Decimal(cleaned or "0")
        except InvalidOperation:
            errors.append(f"{label} — не число")
            continue
        if not number.is_finite() or number < 0 or (upper is not None and number > upper):
            errors.append(f"{label} — вне допустимых границ")
    warehouses = {"FIN_WAREHOUSE_WB": FIN_WAREHOUSE_WB, "FIN_WAREHOUSE_OZON": FIN_WAREHOUSE_OZON,
                  "FIN_WAREHOUSE_KIT": FIN_WAREHOUSE_KIT, "FIN_WAREHOUSE_LAMODA": FIN_WAREHOUSE_LAMODA}
    if any(any(ch in name for ch in "|\r\n") for name in warehouses.values()):
        errors.append("в имени склада 1С не может быть «|» и переводов строки")
    if errors:
        set_flash(request, "Настройки не сохранены: " + "; ".join(errors) + ".", "warn")
        return RedirectResponse("/finance/costs#settings", status_code=303)

    values = {
        "FIN_TAX_MODE": FIN_TAX_MODE,
        "FIN_TAX_RATE": (FIN_TAX_RATE or "0").replace(",", ".").strip(),
        "FIN_EXTRA_COST_PER_UNIT": (FIN_EXTRA_COST_PER_UNIT or "0").replace(",", ".").replace(" ", "").strip(),
        "FIN_USD_RATE_MANUAL": (FIN_USD_RATE_MANUAL or "").replace(",", ".").replace(" ", "").strip(),
        "FIN_AUTO_FETCH": FIN_AUTO_FETCH,
        "FIN_KIT_CRM_COMMISSION": (FIN_KIT_CRM_COMMISSION or "0").replace(",", ".").replace(" ", "").strip(),
        "FIN_VAT_RATE": (FIN_VAT_RATE or "0").replace(",", ".").replace(" ", "").strip(),
        "FIN_KIT_SALES_SOURCE": FIN_KIT_SALES_SOURCE,
        **{key: name.strip() for key, name in warehouses.items()},
    }
    changed = [key for key, value in values.items() if save_setting(db, key, value)]
    if changed:
        log_action(db, user.username, "finance_settings",
                   "; ".join(f"{key}={values[key] or '—'}" for key in changed))
    db.commit()
    set_flash(request, "Настройки расчёта сохранены." if changed else "Настройки — без изменений.",
              "good")
    return RedirectResponse("/finance/costs#settings", status_code=303)


# ======================================================================
# Сверка со сводом площадки
# ======================================================================

STATEMENT_STATUSES = {"": "все", "missing": "не загружены", "diff": "расходятся", "ok": "сходятся"}


@router.get("/finance/statements", response_class=HTMLResponse)
def statements_page(
    request: Request, status: str = Query(""), year: str = Query(""), account: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    d_from = d_to = None
    if year.isdigit():
        d_from, d_to = date(int(year), 1, 1), date(int(year), 12, 31)
    wb_accounts = [a for a in _accounts(db) if a.platform == "wb"]
    if account == statements.NO_ACCOUNT:
        scope = statements.NO_ACCOUNT
    elif account.isdigit() and any(a.id == int(account) for a in wb_accounts):
        scope = int(account)
    else:
        account, scope = "", None
    rows = statements.compare(db, "wb", d_from, d_to, scope)
    counts = {key: sum(1 for r in rows if r.status == key) for key in ("missing", "diff", "ok")}
    loaded = [r for r in rows if r.report is not None]
    totals = {"wb": sum(r.statement.total_kop for r in rows),
              "wb_loaded": sum(r.statement.total_kop for r in loaded),
              "ours": sum(r.ours.get("total_kop", 0) for r in loaded)}
    years = sorted({s.period_from.year for s in db.query(PlatformStatement.period_from)
                    .filter(PlatformStatement.platform == "wb")}, reverse=True)
    # Пусто под отбором кабинета — не «свода нет»: он есть, но у другого кабинета.
    any_summary = bool(years)
    # Кабинеты и юрлица в показанном своде: два ИП в одном списке — повод
    # выбрать кабинет, иначе итоги складывают чужие деньги.
    entities = sorted({(r.statement.account.name if r.statement.account else "кабинет не указан",
                        r.statement.legal_entity or "—") for r in rows})
    shown = [r for r in rows if not status or r.status == status]
    return templates.TemplateResponse(request, "finance_statements.html", _ctx(
        request, user, "finance-statements",
        rows=shown, total_rows=len(rows), counts=counts, totals=totals, status=status,
        statuses=STATEMENT_STATUSES, year=year, years=years, account=account,
        no_account=statements.NO_ACCOUNT, wb_accounts=wb_accounts, entities=entities,
        any_summary=any_summary,
        extra=statements.not_in_summary(db, "wb", rows, scope),
    ))


@router.post("/finance/statements/upload")
def statements_upload(
    request: Request, account_id: str = Form(""), file: UploadFile = File(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    try:
        rows = statements.parse_wb_summary(read_sheets(read_upload(file.file), file.filename or ""))
    except (ReportReadError, statements.StatementError, ExcelReadError) as e:
        set_flash(request, f"Свод не загружен: {e}", "warn")
        return RedirectResponse("/finance/statements", status_code=303)
    target = _int_or_none(account_id)
    warnings = statements.entity_warnings(db, "wb", target, rows)
    stats = statements.store(db, "wb", target, rows, "file")
    entities = sorted({r["legal_entity"] for r in rows if r.get("legal_entity")})
    log_action(db, user.username, "statements_uploaded",
               f"{file.filename}: строк {stats['rows']}, новых {stats['new']}, кабинет {target or '—'}, "
               f"юрлицо {', '.join(entities) or '—'}")
    db.commit()
    message = f"Свод загружен: отчётов {stats['rows']} (новых {stats['new']}, обновлено {stats['updated']})"
    message += f", юрлицо: {', '.join(entities)}." if entities else "."
    if stats["declared_set"]:
        message += f" «Итого к оплате» подставлено в загруженные отчёты: {stats['declared_set']}."
    if warnings:
        # Первыми: сообщение режется по MAX_FLASH_CHARS, а предупреждение о
        # чужом юрлице важнее счётчиков.
        message = " ".join(warnings) + " " + message
    set_flash(request, message, "warn" if warnings else "good")
    return RedirectResponse(f"/finance/statements?account={target}" if target else "/finance/statements",
                            status_code=303)

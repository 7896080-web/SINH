"""Отчёт о продажах → 1С своим файлом заданий (`task_fin_*.txt`).

В 1С по отчёту уходит ровно три вещи на строку: баркод, количество и СУММА К
ПЕРЕЧИСЛЕНИЮ ПРОДАВЦУ (у WB — «К перечислению Продавцу за реализованный Товар»,
у Lamoda — «К перечислению комитенту», у KIT — цена × количество). Так продажи
грузят в 1С сейчас руками. Строка задания на каждую пару «вид × баркод»:

    SALES_REPORT|ключ|площадка|склад|дата ГГГГММДД|SALE или RETURN|баркод|кол-во|сумма

1С собирает строки одного ключа в документы (продажи и возвраты со склада
площадки, датой конца периода отчёта) и отвечает ОДНОЙ строкой на ключ:

    ключ|OK|номера документов|SALES_REPORT
    ключ|ERROR|текст ошибки|SALES_REPORT

Ключ стабилен для одного и того же отчёта (`importer.make_onec_key`), и 1С по
нему находит уже созданный документ и перезаполняет его — повторная отправка
исправленного отчёта не создаёт дубль.

Граница безопасности. В 1С отчёт уходит ТОЛЬКО после явной кнопки на странице
отчёта (`request_upload`): импорт файла или отчёта из API сам по себе ничего в
1С не пишет, а фонового «досылания» нет.
"""
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from finapp import config, mapping
from finapp.audit import log_action
from finapp.finance import values as v
from finapp.finance.importer import resolve_uids
from finapp.finance.settings import load_settings
from finapp.models import OneCUploadStatus, SalesReport, SalesReportLine
from finapp.timeutils import now_utc

logger = logging.getLogger("mpfinance.onec")

SALES_REPORT_COMMAND = "SALES_REPORT"
SALES_REPORT_KEY_PREFIX = "SR-"
# Отчётов в одном файле заданий: каждый — сотни строк и документ в 1С.
MAX_REPORTS_PER_BATCH = 2

IN_FLIGHT = (OneCUploadStatus.pending, OneCUploadStatus.sent)


@dataclass
class OneCLine:
    kind: str          # SALE / RETURN
    barcode: str       # баркод, прописанный у товара в 1С (`mapping.barcode_for_1c`)
    quantity: int
    amount_kop: int    # к перечислению продавцу, всегда положительная: направление задаёт kind
    report_barcodes: list[str] = field(default_factory=list)   # как в отчёте площадки


@dataclass
class OneCPlan:
    lines: list[OneCLine]
    # Суммы без количества (WB «Корректная продажа» на 0 шт и т.п.): в товарный
    # документ их не положить, поэтому оператор видит их отдельно.
    skipped_amount_kop: int = 0
    skipped_lines: int = 0
    # Баркоды отчёта, у товара которых в 1С нет ни одного баркода.
    no_onec_barcode: set[str] = field(default_factory=set)

    def totals(self, kind: str) -> tuple[int, int]:
        rows = [l for l in self.lines if l.kind == kind]
        return sum(l.quantity for l in rows), sum(l.amount_kop for l in rows)


def build_plan(db: Session, report: SalesReport) -> OneCPlan:
    """Свод строк продаж и возвратов по баркоду 1С — то, что уйдёт в 1С."""
    agg: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0, set()])
    plan = OneCPlan(lines=[])
    rows = db.query(SalesReportLine).filter(
        SalesReportLine.report_id == report.id,
        SalesReportLine.operation.in_(v.GOODS_OPERATIONS),
    ).all()
    known = mapping.onec_barcodes(db, {line.uid_1c for line in rows if line.uid_1c})
    for line in rows:
        if not line.quantity:
            if line.payout_kop:
                plan.skipped_amount_kop += line.payout_kop
                plan.skipped_lines += 1
            continue
        kind = "SALE" if line.operation == v.SALE else "RETURN"
        barcode = mapping.barcode_for_1c(line.barcode or "", line.uid_1c, known)
        if barcode is None and line.uid_1c:
            plan.no_onec_barcode.add(line.barcode or "")
        bucket = agg[(kind, barcode or "")]
        bucket[0] += line.quantity
        # Выплата по возврату хранится со знаком «−», в 1С сумма идёт положительной.
        bucket[1] += line.payout_kop if kind == "SALE" else -line.payout_kop
        bucket[2].add(line.barcode or "")
    for (kind, barcode), (quantity, amount, sources) in sorted(agg.items()):
        plan.lines.append(OneCLine(kind, barcode, quantity, amount, sorted(sources)))
    return plan


def upload_problems(db: Session, report: SalesReport, warehouse: str,
                    check_in_flight: bool = True) -> list[str]:
    """Почему отчёт нельзя отправить в 1С прямо сейчас (пусто — можно)."""
    from finapp import onec          # здесь, а не наверху: onec сам импортирует этот модуль

    problems = []
    if report.report_kind == "предварительный":
        problems.append("Это предварительные данные еженедельного отчёта Lamoda (без баркодов и "
                        "возвратов) — в 1С уходит отчёт комиссионера, итоговый документ.")
    if not onec.enabled(db):
        problems.append("Обмен с 1С пока выключен — его включают последним этапом на «Диагностике».")
    elif not onec.epf_ready(db):
        problems.append("Обработка 1С ещё не подтвердила версию mark-4 («Диагностика» → "
                        "«Проверить обработку 1С»).")
    if check_in_flight and report.onec_status in IN_FLIGHT:
        problems.append("Отчёт уже отправлен в 1С — дождитесь ответа обработки.")
    if report.unmatched_lines:
        problems.append(f"Строк продаж/возвратов без товара 1С: {report.unmatched_lines}. "
                        f"Обновите справочник баркодов («Диагностика») и нажмите «Пересопоставить».")
    if not warehouse:
        problems.append(f"Не задан склад 1С для площадки {v.PLATFORM_LABELS[report.platform]} "
                        f"(страница «Себестоимость», блок «Настройки»).")
    if not field_is_safe(warehouse):
        problems.append("В имени склада 1С есть «|» или перевод строки — они разделяют поля и строки "
                        "в файле обмена с 1С. Исправьте имя склада в настройках.")
    plan = build_plan(db, report)
    if not plan.lines:
        problems.append("В отчёте нет продаж и возвратов с количеством — в 1С грузить нечего.")
    if plan.no_onec_barcode:
        problems.append("У товаров с баркодами " + ", ".join(sorted(plan.no_onec_barcode)[:5])
                        + " в 1С не прописано ни одного баркода — 1С не найдёт номенклатуру. "
                          "Пропишите баркод в 1С и обновите справочник («Диагностика»).")
    elif any(not line.barcode for line in plan.lines) and not report.unmatched_lines:
        problems.append("Есть продажи без баркода — 1С не найдёт по ним номенклатуру.")
    negative = [line.barcode for line in plan.lines if line.amount_kop < 0]
    if negative:
        problems.append("Отрицательная сумма к перечислению по баркодам " + ", ".join(negative[:5])
                        + " — проверьте строки отчёта (корректировки площадки).")
    return problems


def request_upload(db: Session, report: SalesReport, user: str) -> list[str]:
    """Кнопка «Отправить в 1С». Баркоды пересопоставляются заново: справочник
    мог обновиться после загрузки отчёта."""
    resolve_uids(db, report)
    warehouse = load_settings(db).warehouses.get(report.platform, "")
    problems = upload_problems(db, report, warehouse)
    if problems:
        db.commit()
        return problems
    report.onec_status = OneCUploadStatus.pending
    report.onec_requested_by = user
    report.onec_requested_at = now_utc()
    report.onec_detail = None
    report.onec_completed_at = None
    log_action(db, user, "sales_report_onec_requested", f"#{report.id} {report.onec_key}")
    db.commit()
    return []


def pending_reports(db: Session) -> list[SalesReport]:
    return db.query(SalesReport).filter(
        SalesReport.onec_status == OneCUploadStatus.pending,
    ).order_by(SalesReport.onec_requested_at.asc(), SalesReport.id.asc()) \
        .limit(MAX_REPORTS_PER_BATCH).all()


def field_is_safe(text: str) -> bool:
    """Поле строки задания 1С: без «|» (разделитель полей) и переводов строки
    (разделитель команд — `СтрРазделить(Содержимое, Символы.ПС)` в обработке)."""
    return not any(ch in (text or "") for ch in "|\r\n")


def task_lines(db: Session, reports: list[SalesReport], filename: str) -> list[str]:
    """Строки SALES_REPORT для файла заданий; отчёты помечаются отправленными.

    Проверки повторяются в момент отправки: между нажатием кнопки и ближайшим
    файлом оператор мог очистить склад в настройках. Такой отчёт в файл не идёт и
    получает статус «ошибка» с причиной."""
    settings = load_settings(db)
    lines = []
    for report in reports:
        resolve_uids(db, report)
        warehouse = settings.warehouses.get(report.platform, "")
        problems = upload_problems(db, report, warehouse, check_in_flight=False)
        if problems:
            report.onec_status = OneCUploadStatus.failed
            report.onec_detail = ("не отправлен: " + " ".join(problems))[:255]
            report.onec_completed_at = now_utc()
            logger.warning("отчёт #%s не отправлен в 1С: %s", report.id, problems)
            continue
        plan = build_plan(db, report)
        day = f"{report.period_to:%Y%m%d}"
        rows = []
        for item in plan.lines:
            amount = v.kop_to_rub(item.amount_kop)
            rows.append([SALES_REPORT_COMMAND, report.onec_key, report.platform, warehouse, day,
                         item.kind, item.barcode, str(item.quantity), f"{amount:.2f}"])
        # Последний рубеж: поле с «|» или переводом строки дало бы 1С ЛИШНЮЮ
        # строку — отдельную команду в общем канале, где ходят и перемещения
        # Sync Admin с настоящими остатками.
        bad = [f for row in rows for f in row if not field_is_safe(f)]
        if bad:
            report.onec_status = OneCUploadStatus.failed
            report.onec_detail = f"не отправлен: недопустимый символ в поле «{bad[0][:40]}»"[:255]
            report.onec_completed_at = now_utc()
            continue
        lines.extend("|".join(row) for row in rows)
        report.onec_status = OneCUploadStatus.sent
        report.onec_sent_at = now_utc()
        report.onec_batch_filename = filename
        report.onec_lines_sent = len(plan.lines)
    return lines


def is_sales_report_result(key: str, command: str) -> bool:
    return command == SALES_REPORT_COMMAND or key.startswith(SALES_REPORT_KEY_PREFIX)


def apply_result(db: Session, key: str, status: str, detail: str) -> bool:
    """Строка ответа по отчёту. Принимается и опоздавший ответ (timeout):
    документ в 1С создан, и потерять это знание хуже, чем принять поздно."""
    report = db.query(SalesReport).filter(
        SalesReport.onec_key == key,
        SalesReport.onec_status.in_([OneCUploadStatus.sent, OneCUploadStatus.timeout]),
    ).first()
    if report is None:
        logger.warning("ответ 1С по отчёту %s (%s) не сопоставлен", key, status)
        return False
    ok = status == "OK"
    report.onec_status = OneCUploadStatus.done if ok else OneCUploadStatus.failed
    report.onec_detail = detail[:255]
    report.onec_completed_at = now_utc()
    if not ok:
        logger.error("1С отказала по отчёту о продажах %s: %s", key, detail[:255])
    return True


def detect_timed_out(db: Session) -> list[SalesReport]:
    cutoff = now_utc() - timedelta(minutes=config.ONEC_TIMEOUT_MINUTES)
    stale = db.query(SalesReport).filter(
        SalesReport.onec_status == OneCUploadStatus.sent,
        SalesReport.onec_sent_at < cutoff,
    ).all()
    for report in stale:
        report.onec_status = OneCUploadStatus.timeout
        report.onec_detail = ("1С не ответила — проверьте, что обработка «ОбменССайтом» "
                              "запускается и знает команду SALES_REPORT")
    if stale:
        db.commit()
    return stale

"""Сверка со сводом площадки: каждый ли отчёт загружен и сходится ли его итог.

Свод — список финансовых отчётов WB с итогами шапки. Два источника, одна таблица
(`PlatformStatement`):
- файл из кабинета WB «Финансовые отчёты» → «Выгрузить» (колонки: № отчета,
  Юридическое лицо, Дата начала, Дата конца, Дата формирования, Тип отчета,
  Продажа, К перечислению за товар, Стоимость доставки, Стоимость хранения,
  Стоимость обработки товара, Прочие удержания/выплаты, Общая сумма штрафов, …,
  Итого к оплате) — сверено с выгрузкой за 29.01.2024–11.10.2026, 271 отчёт;
- `sales-reports/list` finance-api при каждой загрузке отчётов по API.

Итог шапки WB: «К перечислению за товар» − доставка − хранение − обработка −
прочие удержания − штрафы (и остальные удержания) = «Итого к оплате»; на своде
формула сходится до копейки. Та же формула у итога наших строк
(`SalesReportLine.total_kop`), поэтому сравниваются и итог, и статьи — по
статье видно, где разошлось.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from finapp.finance import values as v
from finapp.finance.readers import find_header
from finapp.models import PlatformStatement, SalesReport, SalesReportLine
from finapp.timeutils import now_utc

KINDS = {"основной": "основной", "по выкупам": "по выкупам", "1": "основной", "2": "по выкупам"}

# Имена колонок файла и поля списка finance-api — одним словарём, как у
# детализации: запись API ложится в тот же разбор. Из полей API взяты только
# сверенные со спецификацией (номер, период, тип, продажа, к перечислению,
# доставка, «Итого к оплате»); остальные статьи список API, если и отдаёт, то
# под неизвестными нам именами — у таких отчётов сверяется итог.
WB_SUMMARY_ALIASES = {
    "report_number": ("№ отчета", "Номер отчета", "reportId"),
    "legal_entity": ("Юридическое лицо",),
    "date_from": ("Дата начала", "dateFrom"),
    "date_to": ("Дата конца", "dateTo"),
    "formed_at": ("Дата формирования", "createDate"),
    "kind": ("Тип отчета", "reportType"),
    "sale": ("Продажа", "retailAmountSum"),
    "for_pay": ("К перечислению за товар", "forPaySum"),
    "delivery": ("Стоимость доставки", "deliveryServiceSum"),
    "storage": ("Стоимость хранения",),
    "acceptance": ("Стоимость обработки товара", "Стоимость платной приемки"),
    "other": ("Прочие удержания/выплаты",),
    "penalty": ("Общая сумма штрафов",),
    "wb_correction": ("Корректировка Вознаграждения Вайлдберриз (ВВ)",),
    "loyalty_fee": ("Стоимость участия в программе лояльности",),
    "loyalty_points": ("Сумма баллов, удержанных по программе лояльности",),
    "payment_schedule": ("Разовое изменение срока перечисления денежных средств",),
    "total": ("Итого к оплате", "bankPaymentSum"),
}
REQUIRED = ("report_number", "date_from", "date_to", "total")
DEDUCTION_PARTS = ("other", "wb_correction", "loyalty_fee", "loyalty_points", "payment_schedule")
# Статьи сверки: поле свода → как считается у нас по строкам.
BUCKETS = (
    ("for_pay_kop", "К перечислению за товар"),
    ("delivery_kop", "Доставка"),
    ("storage_kop", "Хранение"),
    ("acceptance_kop", "Обработка товара"),
    ("deduction_kop", "Прочие удержания"),
    ("penalty_kop", "Штрафы"),
)


class StatementError(ValueError):
    pass


def _money(cells: list, mapping: dict, name: str) -> int | None:
    idx = mapping.get(name)
    if idx is None or idx >= len(cells):
        return None
    return v.parse_money(cells[idx])


def parse_wb_summary(sheets: list[tuple[str, list[list]]]) -> list[dict]:
    """Свод WB (файл или записи API) → строки для `store`. Ошибка в строке —
    отказ всего файла с номером строки: полсвода сверку бы обманула."""
    for _name, rows in sheets:
        found = find_header(rows, WB_SUMMARY_ALIASES, REQUIRED, min_fields=len(REQUIRED))
        if found is None:
            continue
        header_row, mapping, _ = found
        out = []
        for offset, cells in enumerate(rows[header_row + 1:], start=header_row + 2):
            if not cells or all(c in (None, "") for c in cells):
                continue
            number = v.parse_text(cells[mapping["report_number"]])
            if not number:
                continue
            try:
                parts = {name: _money(cells, mapping, name) for name in DEDUCTION_PARTS}
                deduction = None if all(p is None for p in parts.values()) else \
                    sum(p or 0 for p in parts.values())
                kind_raw = v.parse_text(cells[mapping["kind"]]) if "kind" in mapping else ""
                row = {
                    "report_number": number[:64],
                    "legal_entity": v.parse_text(cells[mapping["legal_entity"]])[:255]
                    if "legal_entity" in mapping else "",
                    "period_from": v.parse_date(cells[mapping["date_from"]]),
                    "period_to": v.parse_date(cells[mapping["date_to"]]),
                    "formed_at": v.parse_date(cells[mapping["formed_at"]]) if "formed_at" in mapping else None,
                    "report_kind": KINDS.get(kind_raw.strip().lower(), kind_raw.strip().lower() or None),
                    "sale_kop": _money(cells, mapping, "sale"),
                    "for_pay_kop": _money(cells, mapping, "for_pay"),
                    "delivery_kop": _money(cells, mapping, "delivery"),
                    "storage_kop": _money(cells, mapping, "storage"),
                    "acceptance_kop": _money(cells, mapping, "acceptance"),
                    "penalty_kop": _money(cells, mapping, "penalty"),
                    "deduction_kop": deduction,
                    "total_kop": _money(cells, mapping, "total"),
                }
            except v.ValueParseError as e:
                raise StatementError(f"строка {offset}: {e}") from None
            if row["period_from"] is None or row["period_to"] is None or row["total_kop"] is None:
                raise StatementError(f"строка {offset}: нет периода или «Итого к оплате»")
            out.append(row)
        if not out:
            raise StatementError("в своде нет ни одной строки с номером отчёта")
        return out
    raise StatementError("Не похоже на свод отчётов WB: ждём колонки «№ отчета», «Дата начала», "
                         "«Дата конца», «Итого к оплате» (выгрузка «Финансовые отчёты» из кабинета WB).")


def parse_api_list(records: list[dict]) -> list[dict]:
    headers: list[str] = []
    for record in records:
        for key in record:
            if key not in headers:
                headers.append(key)
    return parse_wb_summary([("API", [headers] + [[r.get(k) for k in headers] for r in records])])


def store(db: Session, platform: str, account_id: int | None, rows: list[dict], source: str) -> dict:
    """Записать свод (замена по номеру отчёта). Отчёту, загруженному без «Итого
    к оплате» (файлом), сумма площадки подставляется из свода. Не коммитит."""
    stats = {"rows": len(rows), "new": 0, "updated": 0, "declared_set": 0}
    numbers = [r["report_number"] for r in rows]
    existing = {s.report_number: s for s in db.query(PlatformStatement).filter(
        PlatformStatement.platform == platform, PlatformStatement.report_number.in_(numbers))}
    for row in rows:
        statement = existing.get(row["report_number"])
        if statement is None:
            statement = PlatformStatement(platform=platform, report_number=row["report_number"])
            db.add(statement)
            existing[row["report_number"]] = statement
            stats["new"] += 1
        else:
            stats["updated"] += 1
        # API отдаёт не все колонки файла: пустое из API не затирает полное из файла.
        for name, value in row.items():
            if value is None and source == "api" and getattr(statement, name, None) is not None:
                continue
            setattr(statement, name, value)
        if account_id is not None:
            statement.account_id = account_id
        statement.source = source
        statement.loaded_at = now_utc()
    db.flush()
    for report in db.query(SalesReport).filter(SalesReport.platform == platform,
                                               SalesReport.report_number.in_(numbers),
                                               SalesReport.declared_payout_kop.is_(None)):
        report.declared_payout_kop = existing[report.report_number].total_kop
        stats["declared_set"] += 1
    return stats


def declared_for(db: Session, platform: str, number: str | None) -> int | None:
    """«Итого к оплате» отчёта по своду, если он там есть (для импорта файла)."""
    if not number:
        return None
    statement = db.query(PlatformStatement).filter(PlatformStatement.platform == platform,
                                                   PlatformStatement.report_number == number).first()
    return statement.total_kop if statement else None


@dataclass
class Row:
    statement: PlatformStatement
    report: SalesReport | None = None
    ours: dict[str, int] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.report is None:
            return "missing"
        return "ok" if self.diff == 0 else "diff"

    @property
    def diff(self) -> int | None:
        if self.report is None:
            return None
        return self.ours.get("total_kop", 0) - self.statement.total_kop

    def bucket_diffs(self) -> list[tuple[str, int, int]]:
        out = []
        for name, label in BUCKETS:
            theirs = getattr(self.statement, name)
            if theirs is None or self.report is None:
                continue
            if self.ours.get(name, 0) != theirs:
                out.append((label, theirs, self.ours.get(name, 0)))
        return out


def _ours(db: Session, report_ids: list[int]) -> dict[int, dict[str, int]]:
    L = SalesReportLine
    out: dict[int, dict[str, int]] = defaultdict(dict)
    for start in range(0, len(report_ids), 500):
        for rid, payout, comp, logi, stor, acc, ded, pen in db.query(
                L.report_id, func.sum(L.payout_kop), func.sum(L.compensation_kop),
                func.sum(L.logistics_kop), func.sum(L.storage_kop), func.sum(L.acceptance_kop),
                func.sum(L.deduction_kop), func.sum(L.penalty_kop)).filter(
                L.report_id.in_(report_ids[start:start + 500])).group_by(L.report_id):
            payout, comp, logi, stor, acc, ded, pen = (int(x or 0) for x in (payout, comp, logi, stor, acc, ded, pen))
            # «К перечислению за товар» WB включает и выплаты без продажи
            # (компенсации ущерба и т.п.), которые у нас ложатся в compensation.
            out[rid] = {"for_pay_kop": payout + comp, "delivery_kop": logi, "storage_kop": stor,
                        "acceptance_kop": acc, "deduction_kop": ded, "penalty_kop": pen,
                        "total_kop": payout + comp - logi - stor - acc - ded - pen}
    return out


# Отбор по кабинету: у двух ИП свои кабинеты WB и свои своды, а номера отчётов
# у WB сквозные — смешанный список показывал бы отчёты одного ИП «не
# загруженными» в своде другого. NO_ACCOUNT — свод, загруженный без кабинета.
NO_ACCOUNT = "none"


def _by_account(query, column, account):
    if account is None:
        return query
    if account == NO_ACCOUNT:
        return query.filter(column.is_(None))
    return query.filter(column == account)


def compare(db: Session, platform: str = "wb", date_from: date | None = None,
            date_to: date | None = None, account: int | str | None = None) -> list[Row]:
    """`account`: None — все кабинеты, id кабинета или NO_ACCOUNT."""
    query = _by_account(db.query(PlatformStatement).filter(PlatformStatement.platform == platform),
                        PlatformStatement.account_id, account)
    if date_from:
        query = query.filter(PlatformStatement.period_to >= date_from)
    if date_to:
        query = query.filter(PlatformStatement.period_from <= date_to)
    statements = query.order_by(PlatformStatement.period_from.desc(),
                                PlatformStatement.report_number.desc()).all()
    numbers = [s.report_number for s in statements]
    reports: dict[str, SalesReport] = {}
    for start in range(0, len(numbers), 500):
        for report in db.query(SalesReport).filter(
                SalesReport.platform == platform,
                SalesReport.report_number.in_(numbers[start:start + 500])):
            reports[report.report_number] = report
    ours = _ours(db, [r.id for r in reports.values()])
    rows = []
    for statement in statements:
        report = reports.get(statement.report_number)
        rows.append(Row(statement, report, ours.get(report.id, {}) if report else {}))
    return rows


def not_in_summary(db: Session, platform: str, rows: list[Row],
                   account: int | str | None = None) -> list[tuple[SalesReport, int]]:
    """Загруженные отчёты, которых нет НИ В ОДНОМ своде площадки, — в пределах
    периода, который показанный свод покрывает (за его пределами отсутствие
    ничего не значит: свод просто туда не доходит). Обычно это отчёт другого
    кабинета, чей свод не загружен, либо свод устарел."""
    if not rows:
        return []
    start = min(r.statement.period_from for r in rows)
    end = max(r.statement.period_to for r in rows)
    known = {n for (n,) in db.query(PlatformStatement.report_number).filter(
        PlatformStatement.platform == platform)}
    query = _by_account(db.query(SalesReport).filter(
        SalesReport.platform == platform, SalesReport.period_from <= end,
        SalesReport.period_to >= start), SalesReport.account_id, account)
    extra = [r for r in query.order_by(SalesReport.period_from.desc())
             if r.report_number not in known]
    ours = _ours(db, [r.id for r in extra])
    return [(r, ours.get(r.id, {}).get("total_kop", 0)) for r in extra]


def entity_warnings(db: Session, platform: str, account_id: int | None, rows: list[dict]) -> list[str]:
    """Предупреждения при загрузке свода: в файле несколько юрлиц, или в кабинете
    уже лежит свод ДРУГОГО юрлица (выбран не тот кабинет). Свод при этом
    загружается: повторная загрузка с верным кабинетом его переназначит."""
    out = []
    in_file = sorted({r["legal_entity"] for r in rows if r.get("legal_entity")})
    if len(in_file) > 1:
        out.append("В файле несколько юрлиц: " + ", ".join(in_file)
                   + ". Свод одного кабинета — одно юрлицо; разнесите по кабинетам.")
    if account_id is not None and in_file:
        numbers = {r["report_number"] for r in rows}
        before = {e for n, e in db.query(PlatformStatement.report_number, PlatformStatement.legal_entity)
                  .filter(PlatformStatement.platform == platform,
                          PlatformStatement.account_id == account_id) if n not in numbers and e}
        if before and not before & set(in_file):
            out.append(f"В этом кабинете уже лежит свод юрлица {', '.join(sorted(before))}, а в файле — "
                       f"{', '.join(in_file)}. Если кабинет выбран не тот, загрузите файл ещё раз с верным.")
    return out

"""Загрузка официальных отчётов площадок по API.

Очередь — таблица `ReportFetch`. Заявку ставит оператор кнопкой (любой период)
или само задание раз в сутки (`plan_regular`, если не выключено настройкой
FIN_AUTO_FETCH). Разбирает заявки фоновое задание `finance_fetch` раз в пять
минут (`run_pending`):

- `wb_api`  — официальные еженедельные отчёты реализации WB (finance-api):
  список за период, затем детализация каждого нового отчёта. «Итого к оплате»
  WB ложится в отчёт как сумма площадки для сверки с итогом строк;
- `kit_api` — отчёт о продажах KIT за период из официальных данных заказов.

Площадкам — только чтение. Себестоимость из 1С ходит обменом с 1С (`onec.py`).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Callable

import requests
from sqlalchemy.orm import Session

from finapp import accounts, settings as store
from finapp.finance import statements, values as v
from finapp.finance.formats import ParseResult, ReportFormat, parse_records
from finapp.finance.importer import make_onec_key, store_api_report
from finapp.finance.rates import RateError, fetch_cbr_range, missing_range
from finapp.finance.settings import load_settings
from finapp.finance.sources import kit as kit_source
from finapp.finance.sources import wb as wb_source
from finapp.models import (KitVariant, OneCUploadStatus, PlatformAccount, ReportFetch, SalesReport,
                           SalesReportLine)
from finapp.timeutils import local_day_start_utc, now_utc, today_local

logger = logging.getLogger("mpfinance.fetch")

KINDS = {
    "wb_api": "WB — официальные отчёты реализации (API)",
    "kit_api": "Яндекс KIT — продажи по заказам (API)",
}
KIND_PLATFORM = {"wb_api": "wb", "kit_api": "kit"}
ACTIVE = ("pending", "running")
# «running», у которого отметка шага не двигалась дольше этого, — проход,
# оборванный перезапуском службы: его берут заново (уже загруженные отчёты при
# этом пропускаются по ключу, двойной загрузки не будет).
STALE_MINUTES = 15
# Сколько дней назад смотрит ежедневная автозагрузка WB: отчёт за неделю WB
# формирует в понедельник, иногда позже, и иногда переформировывает.
WB_AUTO_DAYS = 28
# KIT: отказы при получении доезжают в заказы позже доставки, поэтому две
# последние полные недели перезабираются каждый день, пока не ушли в 1С.
KIT_AUTO_WEEKS = 2
AUTO_EVERY = timedelta(hours=20)

AUTO_FETCH_KEY = "FIN_AUTO_FETCH"

KIT_API_FORMAT = ReportFormat(code="kit_api", label="Яндекс KIT — заказы (API)",
                              platforms=("kit",), aliases={}, build=lambda row: {})
# Вид отчёта KIT по API, когда продажи берутся из AWER CRM: только доставка.
KIT_DELIVERY_KIND = "доставка"


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------- заявки

def credentials_of(db: Session, account: PlatformAccount) -> dict[str, str]:
    return accounts.credentials(db, account)


def wb_report_token(creds: dict[str, str]) -> str:
    """Токен для финансовых отчётов WB — с категорией «Финансы»."""
    return (creds.get("finance_token") or "").strip()


def request_fetch(db: Session, kind: str, user: str, account_id: int | None = None,
                  date_from: date | None = None, date_to: date | None = None) -> tuple[ReportFetch | None, str]:
    """Поставить заявку. Возвращает (заявка, "") или (None, причина отказа)."""
    if kind not in KINDS:
        return None, f"Неизвестный вид загрузки «{kind}»."
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first() \
        if account_id is not None else None
    if account is None or account.platform != KIND_PLATFORM[kind]:
        return None, "Выберите кабинет нужной площадки."
    if date_from is None or date_to is None or date_from > date_to:
        return None, "Укажите период: дата «с» не позже даты «по»."
    if kind == "wb_api" and date_from < wb_source.FIRST_AVAILABLE:
        date_from = wb_source.FIRST_AVAILABLE
    if date_to > today_local():
        date_to = today_local()
    busy = db.query(ReportFetch).filter(ReportFetch.kind == kind, ReportFetch.status.in_(ACTIVE),
                                        ReportFetch.account_id == account.id).first()
    if busy is not None:
        return None, f"Такая загрузка уже идёт (заявка #{busy.id}) — дождитесь её окончания."
    job = ReportFetch(kind=kind, account_id=account.id, date_from=date_from, date_to=date_to,
                      requested_by=user)
    db.add(job)
    db.flush()
    return job, ""


def auto_enabled(db: Session) -> bool:
    return store.get(db, AUTO_FETCH_KEY, "1") != "0"


def plan_regular(db: Session) -> int:
    """Ежедневные заявки: WB за последние недели, KIT за две полные недели.
    Заявка ставится, только если такой не было почти сутки."""
    if not auto_enabled(db):
        return 0
    created = 0
    since = now_utc() - AUTO_EVERY
    today = today_local()

    def recent(kind: str, account_id: int) -> bool:
        return db.query(ReportFetch).filter(ReportFetch.kind == kind, ReportFetch.created_at >= since,
                                            ReportFetch.account_id == account_id).first() is not None

    active = db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all()
    for account in active:
        creds = credentials_of(db, account)
        if account.platform == "wb" and wb_report_token(creds) and not recent("wb_api", account.id):
            job, _ = request_fetch(db, "wb_api", "system", account.id,
                                   today - timedelta(days=WB_AUTO_DAYS), today)
            created += 1 if job else 0
        if account.platform == "kit" and creds.get("token") and not recent("kit_api", account.id):
            monday = today - timedelta(days=today.weekday())
            job, _ = request_fetch(db, "kit_api", "system", account.id,
                                   monday - timedelta(weeks=KIT_AUTO_WEEKS), monday - timedelta(days=1))
            created += 1 if job else 0
    db.commit()
    return created


# ---------------------------------------------------------------- исполнение

def _next_job(db: Session) -> ReportFetch | None:
    stale = now_utc() - timedelta(minutes=STALE_MINUTES)
    return db.query(ReportFetch).filter(
        ReportFetch.kind.in_(tuple(KIND_PLATFORM)),
        (ReportFetch.status == "pending")
        | ((ReportFetch.status == "running") & (ReportFetch.progress_at < stale)),
    ).order_by(ReportFetch.id.asc()).first()


def _step(db: Session, job: ReportFetch, heartbeat: Callable[[], None]):
    job.progress_at = now_utc()
    db.commit()
    heartbeat()


def run_pending(db: Session, heartbeat: Callable[[], None] = lambda: None,
                session: requests.Session | None = None,
                sleep: Callable[[float], None] = time.sleep, max_jobs: int = 10) -> dict:
    """Разобрать заявки WB/KIT по одной. Ошибка одной заявки не останавливает
    остальные: она получает статус failed и человекочитаемую причину."""
    stats = {"jobs": 0, "imported": 0, "failed": 0}
    for _ in range(max_jobs):
        job = _next_job(db)
        if job is None:
            break
        stats["jobs"] += 1
        job.status = "running"
        job.started_at = job.started_at or now_utc()
        job.message = None
        _step(db, job, heartbeat)
        try:
            if job.kind == "wb_api":
                notes = _run_wb(db, job, heartbeat, session, sleep)
            else:
                notes = _run_kit(db, job, heartbeat, session, sleep)
            job.status = "done"
            job.message = notes
        except (FetchError, wb_source.WbFinanceError) as e:
            db.rollback()
            job.status = "failed"
            job.message = str(e)[:2000]
            stats["failed"] += 1
        except Exception as e:                   # noqa: BLE001 — сеть, неожиданный ответ
            db.rollback()
            logger.exception("finance_fetch: заявка #%s упала", job.id)
            job.status = "failed"
            job.message = f"{type(e).__name__}: {e}"[:2000]
            stats["failed"] += 1
        job.finished_at = now_utc()
        stats["imported"] += job.imported or 0
        db.commit()
        heartbeat()
    return stats


def _run_wb(db: Session, job: ReportFetch, heartbeat, session, sleep) -> str:
    account = job.account
    token = wb_report_token(credentials_of(db, account))
    if not token:
        raise FetchError(f"У кабинета «{account.name}» не задан токен WB с категорией «Финансы» "
                         f"(страница «API-ключи»).")
    api = wb_source.WbFinanceApi(token, session=session, sleep=sleep)
    reports = api.list_reports(job.date_from, job.date_to)
    job.found = len(reports)
    # Список отчётов — это и свод для сверки (`statements.py`): какие отчёты WB
    # сформировал и с каким «Итого к оплате».
    if reports:
        try:
            statements.store(db, "wb", account.id, statements.parse_api_list(reports), "api")
        except statements.StatementError as e:
            logger.warning("свод WB из API не записан: %s", e)
    _step(db, job, heartbeat)

    notes, skipped = [], 0
    for header in sorted(reports, key=lambda r: (str(r.get("dateFrom")), int(r.get("reportId") or 0))):
        report_id = header.get("reportId")
        period_from = v.parse_date(header.get("dateFrom"))
        period_to = v.parse_date(header.get("dateTo"))
        if not report_id or not period_from or not period_to:
            notes.append(f"отчёт без номера или периода пропущен: {header!r}"[:200])
            continue
        # Уже загруженный отчёт не перезапрашиваем: детализация стоит минуту.
        key = make_onec_key("wb", account.id, str(report_id), period_from, period_to)
        if db.query(SalesReport.id).filter(SalesReport.onec_key == key).first():
            skipped += 1
            continue
        records = api.report_lines(report_id, on_page=lambda _n: _step(db, job, heartbeat))
        if not records:
            notes.append(f"отчёт {report_id}: WB вернул пустую детализацию")
            continue
        parsed = parse_records(records, "wb")
        outcome = store_api_report(
            db, platform="wb", account_id=account.id, parsed=parsed,
            period_from=period_from, period_to=period_to, user=f"api:{job.requested_by or 'system'}",
            report_number=str(report_id),
            report_kind=wb_source.REPORT_TYPES.get(int(header.get("reportType") or 0)),
            declared_payout=v.parse_money(header.get("bankPaymentSum")),
            file_name=f"WB finance-api, отчёт №{report_id}")
        if outcome.errors:
            db.rollback()
            notes.append(f"отчёт {report_id} не загружен: " + " ".join(outcome.errors)[:400])
            continue
        if outcome.report is not None and not outcome.skipped:
            job.imported += 1
            mismatch = [w for w in outcome.warnings if "Итого к оплате" in w]
            if mismatch:
                notes.append(f"отчёт {report_id}: {mismatch[0]}")
        _step(db, job, heartbeat)
    summary = f"отчётов WB за период: {job.found}, загружено новых: {job.imported}"
    if skipped:
        summary += f", уже были: {skipped}"
    return "; ".join([summary] + notes)[:2000]


def _kit_variant_map(db: Session, account_id: int) -> dict[str, str | None]:
    """variant_id → баркод из своего кэша (`KitVariant`): по запросу на строку
    KIT быстро отвечает 429, а баркод варианта не меняется."""
    rows = db.query(KitVariant.variant_id, KitVariant.barcode).filter(
        KitVariant.account_id == account_id).all()
    return {vid: bc for vid, bc in rows}


def _run_kit(db: Session, job: ReportFetch, heartbeat, session, sleep) -> str:
    account = job.account
    token = (credentials_of(db, account).get("token") or "").strip()
    if not token:
        raise FetchError(f"У кабинета «{account.name}» не задан токен KIT (страница «API-ключи»).")
    api = kit_source.KitOrdersApi(token, session=session, sleep=sleep)
    orders = api.orders_updated_since(local_day_start_utc(job.date_from))
    _step(db, job, heartbeat)
    # Продажи сайта берутся из AWER CRM — тогда из заказов KIT нужна только
    # доставка: продажи в них те же, и посчитались бы дважды. Баркоды вариантов
    # тогда не нужны вовсе, и запросов к KIT за ними не делаем.
    delivery_only = load_settings(db).kit_sales_source == "crm"

    known = _kit_variant_map(db, account.id)
    asked: dict[str, str | None] = {}

    def barcode_for(variant_id: str) -> str | None:
        if delivery_only:
            return None
        if known.get(variant_id):
            return known[variant_id]
        if variant_id not in asked:
            asked[variant_id] = api.variant_barcode(variant_id)
        return asked[variant_id]

    lines, stats = kit_source.build_lines(orders, job.date_from, job.date_to, barcode_for)
    if delivery_only:
        lines = [line for line in lines if line["operation"] == v.LOGISTICS]
    for variant_id, barcode in asked.items():
        cached = db.get(KitVariant, (account.id, variant_id))
        if cached is None:
            db.add(KitVariant(account_id=account.id, variant_id=variant_id[:64], barcode=barcode))
        else:
            cached.barcode, cached.fetched_at = barcode, now_utc()
    db.flush()
    job.found = stats["orders"]
    if not lines:
        what = "стоимости доставки в заказах KIT" if delivery_only and stats["orders"] else "заказов KIT"
        return (f"{what}, доставленных за {job.date_from:%d.%m}–{job.date_to:%d.%m.%Y}, нет "
                f"(просмотрено изменённых: {len(orders)})")
    dropped = _drop_unsent_kit_sales(db, account.id, job) if delivery_only else 0
    parsed = ParseResult(format=KIT_API_FORMAT, lines=lines)
    outcome = store_api_report(
        db, platform="kit", account_id=account.id, parsed=parsed, period_from=job.date_from,
        period_to=job.date_to, user=f"api:{job.requested_by or 'system'}",
        file_name="Яндекс KIT, доставка по API" if delivery_only else "Яндекс KIT, заказы по API",
        replace_unsent=True, report_kind=KIT_DELIVERY_KIND if delivery_only else None,
        key_suffix="-dlv" if delivery_only else "")
    if outcome.errors:
        raise FetchError(" ".join(outcome.errors))
    if outcome.skipped:
        return (f"отчёт KIT за этот период уже отправлен в 1С (#{outcome.report.id}) — "
                f"не перезаписываю")
    job.imported = 1
    if delivery_only:
        cost = sum(line["logistics"] for line in lines)
        paid = sum(line["compensation"] for line in lines)
        note = (f"только доставка (продажи — из AWER CRM): заказов {stats['orders']}, доставка для "
                f"продавца {v.format_rub(cost)} ₽, оплачено покупателями {v.format_rub(paid)} ₽")
        if outcome.replaced:
            note += "; прежний неотправленный отчёт за период заменён свежим"
        if dropped:
            note += ("; прежний отчёт по API с продажами за этот период удалён — продажи теперь из "
                     "AWER CRM")
        return note
    note = (f"заказов: {stats['orders']}, продано {stats['sold_qty']} шт, отказов при получении "
            f"{stats['refused_qty']} шт")
    if stats["no_barcode"]:
        note += f", позиций без баркода: {stats['no_barcode']}"
    if outcome.replaced:
        note += "; прежний неотправленный отчёт за период заменён свежим"
    return note


def _drop_unsent_kit_sales(db: Session, account_id: int, job: ReportFetch) -> int:
    """Продажи KIT переключили на AWER CRM: отчёт по API С ПРОДАЖАМИ за тот же
    период (прежний режим) задвоил бы их с CRM. Пока он не ушёл в 1С, его можно
    забрать заново в любой момент — удаляем; ушедший в 1С не трогаем."""
    key = make_onec_key("kit", account_id, "", job.date_from, job.date_to)
    old = db.query(SalesReport).filter(
        SalesReport.onec_key == key, SalesReport.source == "api",
        SalesReport.onec_status.in_((OneCUploadStatus.none, OneCUploadStatus.failed,
                                     OneCUploadStatus.timeout))).first()
    if old is None:
        return 0
    db.query(SalesReportLine).filter(SalesReportLine.report_id == old.id).delete(synchronize_session=False)
    db.delete(old)
    db.flush()
    return 1


# ---------------------------------------------------------------- курс ЦБ

_rates_retry_after: datetime | None = None
RATES_RETRY = timedelta(hours=1)


def ensure_rates(db: Session, session: requests.Session | None = None) -> int:
    """Дозапросить у ЦБ курсы на даты продаж, по которым их ещё нет. ЦБ
    недоступен — повтор через час, а не каждые пять минут."""
    global _rates_retry_after
    if _rates_retry_after and now_utc() < _rates_retry_after:
        return 0
    first = db.query(SalesReportLine.operation_date).order_by(SalesReportLine.operation_date.asc()).first()
    if first is None:
        return 0
    gap = missing_range(db, first[0], today_local())
    if gap is None:
        return 0
    try:
        stored = fetch_cbr_range(db, gap[0], gap[1], session=session)
    except RateError as e:
        _rates_retry_after = now_utc() + RATES_RETRY
        logger.warning("finance_fetch: курс ЦБ не получен: %s", e)
        return 0
    _rates_retry_after = None
    return stored


# ---------------------------------------------------------------- проверка доступа

ACCESS_CHECK_DAYS = 14


def check_access(db: Session, account: PlatformAccount, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> tuple[bool, str]:
    """Кнопка «Проверить доступ»: ОДИН запрос к площадке тем ключом, что
    записан в кабинете. У WB это список отчётов за две недели (лимит WB — запрос
    в минуту; загрузка, идущая в это же время, подождёт), у KIT — первая
    страница заказов. Площадке — только чтение."""
    creds = credentials_of(db, account)
    today = today_local()
    try:
        if account.platform == "wb":
            token = wb_report_token(creds)
            if not token:
                return False, "не задан токен WB с категорией «Финансы»"
            api = wb_source.WbFinanceApi(token, session=session, sleep=sleep)
            reports = api.list_reports(today - timedelta(days=ACCESS_CHECK_DAYS), today)
            return True, (f"доступ к финансовым отчётам WB есть: отчётов за {ACCESS_CHECK_DAYS} дней — "
                          f"{len(reports)}")
        if account.platform == "kit":
            token = (creds.get("token") or "").strip()
            if not token:
                return False, "не задан токен KIT"
            api = kit_source.KitOrdersApi(token, session=session, sleep=sleep)
            data = api._get("/v1/orders", params={"page": 1, "per_page": 1})
            return True, f"доступ к заказам KIT есть: заказов в магазине — {data.get('total_count', '?')}"
        return False, "у этой площадки отчёты загружаются файлом — по API проверять нечего"
    except wb_source.WbFinanceError as e:
        return False, str(e)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        return False, f"площадка ответила {status}"
    except requests.RequestException as e:
        return False, f"нет связи с площадкой: {type(e).__name__}"

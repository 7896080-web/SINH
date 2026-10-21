"""Задания: опрос Lamoda, чтение 1С, пересчёт. Каждый запуск — строка в job_runs
(когда, чем кончилось, сводка), её видно на главной странице.

Ежедневный цикл (`python -m forecast daily`, запускается Планировщиком Windows):
каталог Lamoda → остатки FBO → поставки FBO → заказы → остаток ЦС → продажи 1С → пересчёт.
Шаги независимы: сбой одного не отменяет остальные, а пересчёт идёт по последним
данным — даты срезов видны на страницах, устаревшие данные не спрячутся.
"""
from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import desc, select, update
from sqlalchemy.engine import Engine

from forecast.catalog.lamoda_sync import sync_nomenclatures
from forecast.catalog.resolver import SkuResolver
from forecast.config import Settings
from forecast.db.schema import t_job_runs
from forecast.ingest.lamoda import ingest_fbo_shipments, ingest_fbo_stocks, ingest_orders
from forecast.ingest.onec import ingest_sales_files, ingest_stock_file
from forecast.integrations.lamoda.client import LamodaClient, parse_datetime
from forecast.integrations.onec.files import find_newest_stock_file, find_sales_files
from forecast.planning.recalc import recalc
from forecast.timeutil import as_aware_utc, to_naive_utc, today_msk, utcnow

logger = logging.getLogger(__name__)

STALE_RUNNING_HOURS = 3
JOB_TITLES = {
    "lamoda_catalog": "Каталог Lamoda (сверка SKU)",
    "lamoda_stocks": "Остатки FBO Lamoda",
    "lamoda_shipments": "Поставки на FBO (в пути)",
    "lamoda_orders": "Заказы Lamoda",
    "onec_stock": "Остаток ЦС из 1С",
    "onec_sales": "Продажи из 1С",
    "recalc": "Пересчёт рекомендаций",
    "catalog_import": "Импорт карты мэппинга",
    "fbo_assembly": "Сборка поставки на FBO",
}
DAILY_STEPS = ("lamoda_catalog", "lamoda_stocks", "lamoda_shipments", "lamoda_orders", "onec_stock",
               "onec_sales", "recalc")
LAMODA_JOBS = {"lamoda_catalog", "lamoda_stocks", "lamoda_shipments", "lamoda_orders"}


class JobAlreadyRunning(Exception):
    pass


@dataclass
class JobResult:
    run_id: int
    job: str
    status: str
    message: str
    details: dict | None = None


def start_job(engine: Engine, job: str, started_by: str) -> int:
    with engine.begin() as conn:
        threshold = utcnow() - timedelta(hours=STALE_RUNNING_HOURS)
        running = conn.execute(select(t_job_runs.c.run_id).where(
            t_job_runs.c.job == job, t_job_runs.c.status == "running",
            t_job_runs.c.started_at >= threshold)).first()
        if running:
            raise JobAlreadyRunning(f"«{JOB_TITLES.get(job, job)}» уже выполняется (запуск №{running[0]})")
        # Зависшие «running» старше порога (процесс убит) закрываем, чтобы не висели вечно.
        conn.execute(update(t_job_runs).where(
            t_job_runs.c.job == job, t_job_runs.c.status == "running", t_job_runs.c.started_at < threshold)
            .values(status="error", finished_at=utcnow(), message="прервано: процесс не завершил задание"))
        result = conn.execute(t_job_runs.insert().values(job=job, started_at=utcnow(), status="running",
                                                         started_by=started_by))
        return result.inserted_primary_key[0]


def finish_job(engine: Engine, run_id: int, status: str, message: str, details: dict | None = None,
               cursor: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(update(t_job_runs).where(t_job_runs.c.run_id == run_id).values(
            status=status, finished_at=utcnow(), message=message[:2000], details=details, cursor=cursor))


def last_cursor(engine: Engine, job: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(select(t_job_runs.c.cursor).where(
            t_job_runs.c.job == job, t_job_runs.c.status == "ok", t_job_runs.c.cursor.is_not(None))
            .order_by(desc(t_job_runs.c.run_id)).limit(1)).first()
    return row[0] if row else None


def make_lamoda_client(settings: Settings, http: httpx.Client | None = None,
                       sleep: Callable[[float], None] | None = None) -> LamodaClient:
    if not settings.lamoda_configured:
        raise RuntimeError("Ключи Lamoda не заданы: LAMODA_CLIENT_ID, LAMODA_CLIENT_SECRET, LAMODA_SELLER_ID в .env")
    extra = {"sleep": sleep} if sleep is not None else {}
    return LamodaClient(settings.lamoda_client_id, settings.lamoda_client_secret, settings.lamoda_seller_id,
                        base_url=settings.lamoda_base_url, http=http, **extra,
                        # пауза между запросами — вежливость к живому API; у имитации (http задан) не нужна
                        request_pause_seconds=0.0 if http is not None else 0.2)


# ---------------- шаги ----------------

def _lamoda_catalog(engine, settings, client, run_id):
    with engine.begin() as conn:
        report = sync_nomenclatures(conn, client.iter_nomenclatures(settings.lamoda_country))
    return (f"товаров Lamoda {report['total']}, сопоставлено {report['matched']}, "
            f"SKU проставлено {report['sizes_filled']}, не найдено в справочнике {report['unmatched_total']}"), report, None


def _lamoda_stocks(engine, settings, client, run_id):
    with engine.begin() as conn:
        report = ingest_fbo_stocks(conn, client.iter_fbo_stocks(), SkuResolver.load(conn), today_msk())
    return (f"позиций {report['rows']}, сопоставлено {report['matched']}, штук на FBO {report['units']}, "
            f"не сопоставлено {report['unmatched_total']}"), report, None


def _lamoda_shipments(engine, settings, client, run_id):
    with engine.begin() as conn:
        report = ingest_fbo_shipments(conn, client, SkuResolver.load(conn), today_msk())
    return (f"поставок {report['shipments']}, в пути {report['in_transit']}, "
            f"закрылось сейчас {report['closed_now']}"), report, None


def _lamoda_orders(engine, settings, client, run_id):
    now = utcnow()
    cursor = last_cursor(engine, "lamoda_orders")
    if cursor:
        updated_from = to_naive_utc(parse_datetime(cursor)) - timedelta(hours=settings.orders_overlap_hours)
    else:
        updated_from = now - timedelta(days=settings.orders_backfill_days)
    with engine.connect() as conn:
        resolver = SkuResolver.load(conn)
    report = ingest_orders(engine, client, resolver, as_aware_utc(updated_from), as_aware_utc(now))
    report["updated_from"] = updated_from.isoformat()
    report["updated_to"] = now.isoformat()
    message = (f"заказов {report['orders']}, позиций {report['items_seen']}, новых строк {report['items_inserted']}, "
               f"не сопоставлено позиций {report['unmapped_items']}")
    # Следующий опрос — от момента начала этого (с перекрытием): всё, что обновилось
    # раньше, мы уже получили; незавершённый запуск курсор не двигает.
    return message, report, as_aware_utc(now).isoformat()


def _onec_stock(engine, settings, client, run_id):
    path = find_newest_stock_file(settings.onec_exchange_dirs)
    if path is None:
        raise RuntimeError("нет файлов stock_*.txt в " + "; ".join(str(d) for d in settings.onec_exchange_dirs))
    with engine.begin() as conn:
        report = ingest_stock_file(conn, path, SkuResolver.load(conn))
    age_hours = (datetime.now() - datetime.fromisoformat(report["taken_at"])).total_seconds() / 3600
    report["age_hours"] = round(age_hours, 1)
    message = (f"{report['file']}: строк {report['rows']}, сопоставлено {report['matched']}, "
               f"размеров в наличии {report['sizes_in_stock']}")
    if age_hours > 26:
        message += f" — ВНИМАНИЕ: выгрузке {age_hours:.0f} ч, свежее в каталоге обмена нет"
    return message, report, None


def _onec_sales(engine, settings, client, run_id):
    files = find_sales_files(settings.onec_sales_dir)
    if not files:
        return "файлов продаж sales_*.txt нет (каталог ONEC_SALES_DIR)", {"files": []}, None
    with engine.begin() as conn:
        report = ingest_sales_files(conn, files)
    return f"файлов {len(report['files'])}, строк {report['rows']}, ошибок {len(report['errors'])}", report, None


def _recalc(engine, settings, client, run_id):
    with engine.begin() as conn:
        report = recalc(conn, settings.planning, today_msk(), run_id, utcnow())
    ship, buy = report["shipment"], report["purchase"]
    return (f"FBO: строк {ship['rows']}, к отгрузке {ship['to_ship']} SKU / {ship['units']} шт.; "
            f"закупка: {buy['rows']} SKU / {buy['units']} шт., без пополнения {buy['no_reorder']}"), report, None


STEPS: dict[str, Callable] = {
    "lamoda_catalog": _lamoda_catalog, "lamoda_stocks": _lamoda_stocks, "lamoda_shipments": _lamoda_shipments,
    "lamoda_orders": _lamoda_orders, "onec_stock": _onec_stock, "onec_sales": _onec_sales, "recalc": _recalc,
}


def run_step(engine: Engine, settings: Settings, job: str, *, started_by: str = "scheduler",
             http: httpx.Client | None = None, sleep: Callable[[float], None] | None = None) -> JobResult:
    if job not in STEPS:
        raise ValueError(f"нет такого задания: {job}")
    run_id = start_job(engine, job, started_by)
    try:
        client = make_lamoda_client(settings, http, sleep) if job in LAMODA_JOBS else None
        message, details, cursor = STEPS[job](engine, settings, client, run_id)
    except Exception as exc:  # отчёт о сбое — в job_runs, на главную страницу
        logger.exception("задание %s упало", job)
        tail = traceback.format_exc().splitlines()[-6:]
        finish_job(engine, run_id, "error", f"{exc.__class__.__name__}: {exc}", {"traceback": tail})
        return JobResult(run_id, job, "error", f"{exc.__class__.__name__}: {exc}")
    finish_job(engine, run_id, "ok", message, _jsonable(details), cursor)
    return JobResult(run_id, job, "ok", message, details)


def run_daily(engine: Engine, settings: Settings, *, started_by: str = "scheduler",
              http: httpx.Client | None = None, sleep: Callable[[float], None] | None = None) -> list[JobResult]:
    results = []
    for job in DAILY_STEPS:
        if job in LAMODA_JOBS and not settings.lamoda_configured:
            results.append(JobResult(0, job, "skipped", "ключи Lamoda не заданы"))
            continue
        try:
            results.append(run_step(engine, settings, job, started_by=started_by, http=http, sleep=sleep))
        except JobAlreadyRunning as exc:
            results.append(JobResult(0, job, "skipped", str(exc)))
    return results


def _jsonable(value):
    """details уходят в JSON-колонку: даты и Decimal — строками."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

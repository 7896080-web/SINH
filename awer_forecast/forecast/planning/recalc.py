"""Пересчёт: прогноз → точка заказа → рекомендации двух страниц (разделы 5.5 и 6 ТЗ).

Один пересчёт = одна «партия» (batch_id = run_id задания в job_runs). Страницы
показывают последнюю партию. Что менеджер уже успел сделать руками, пересчёт не теряет:
- подтверждённое к поставке (confirmed) переносится в новую партию вместе с автором;
- собранное в поставку (sent_to_edo) и заказанное у поставщика (ordered) не трогается;
- остальные строки прошлых партий получают статус superseded.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection

from forecast.catalog.resolver import SkuResolver
from forecast.config import PlanningSettings
from forecast.db.ops import upsert
from forecast.db.schema import (
    t_barcode_pool, t_central_stock_aggregate_demand, t_demand_forecast, t_fbo_shipments, t_job_runs,
    t_lead_time_stats, t_product_models, t_product_sizes, t_purchase_recommendations, t_reorder_points,
    t_shipment_recommendations, t_stock_1c, t_stock_fbo, t_suppliers,
)
from forecast.ingest.lamoda import rebuild_sales_daily, remap_unmapped_order_items
from forecast.integrations.lamoda.statuses import KNOWN_SHIPMENT_STATUSES, SHIPMENT_FINAL_STATUSES
from forecast.planning import smoothing
from forecast.planning.policy import Policy, order_up_to
from forecast.planning.series import DemandSeries, channel_series, lamoda_series, store_lamoda_timeseries
from forecast.timeutil import msk_date

logger = logging.getLogger(__name__)

IN_TRANSIT_MAX_AGE_DAYS = 120     # поставку старше — не считаем «в пути», даже если статус не закрыт
ASSEMBLED_PENDING_DAYS = 14       # сколько дней собранное у нас ждёт появления поставки в Lamoda
MIN_SHIPMENTS_FOR_LEAD = 3        # меньше закрытых на наших глазах поставок — срок FBO из настроек
NO_REPLENISH_STATUSES = {"fading": "затухающий", "discontinued": "снят с продажи"}
CHANNEL_NAMES = {"lamoda": "Lamoda", "wb": "WB", "ozon": "Ozon", "own_site": "сайт"}


def fmt(value: float, digits: int = 2) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:.0f}"
    return (text or "0").replace(".", ",")


def channel_name(code: str) -> str:
    if code.startswith("retail_"):
        return "розница " + code.removeprefix("retail_")
    return CHANNEL_NAMES.get(code, code.replace("_", " "))


@dataclass
class SizeInfo:
    psid: int
    model_id: int
    article: str
    color: str
    size: str
    category: str
    status: str
    collection: str
    available_to_reorder: bool
    lamoda_sku: str | None
    supplier_id: int | None
    supplier_name: str | None
    production_days: int | None
    delivery_days: int | None
    moq: int | None
    barcode: str | None


def load_catalog(conn: Connection) -> dict[int, SizeInfo]:
    first_barcode = {}
    for psid, barcode in conn.execute(
        select(t_barcode_pool.c.product_size_id, t_barcode_pool.c.barcode)
        .where(t_barcode_pool.c.valid_to.is_(None)).order_by(t_barcode_pool.c.barcode_id)):
        first_barcode.setdefault(psid, barcode)
    rows = conn.execute(
        select(t_product_sizes.c.product_size_id, t_product_sizes.c.size_label, t_product_sizes.c.lamoda_sku,
               t_product_models.c.product_model_id, t_product_models.c.article, t_product_models.c.color,
               t_product_models.c.category, t_product_models.c.status, t_product_models.c.collection_type,
               t_product_models.c.available_to_reorder, t_product_models.c.supplier_id,
               t_suppliers.c.name.label("supplier_name"), t_suppliers.c.production_days,
               t_suppliers.c.delivery_days, t_suppliers.c.moq)
        .join(t_product_models, t_product_models.c.product_model_id == t_product_sizes.c.product_model_id)
        .outerjoin(t_suppliers, t_suppliers.c.supplier_id == t_product_models.c.supplier_id))
    return {r.product_size_id: SizeInfo(
        psid=r.product_size_id, model_id=r.product_model_id, article=r.article, color=r.color or "",
        size=r.size_label, category=r.category, status=r.status or "active", collection=r.collection_type or "base",
        available_to_reorder=r.available_to_reorder is not False, lamoda_sku=r.lamoda_sku,
        supplier_id=r.supplier_id, supplier_name=r.supplier_name, production_days=r.production_days,
        delivery_days=r.delivery_days, moq=r.moq, barcode=first_barcode.get(r.product_size_id),
    ) for r in rows}


def latest_snapshot(conn: Connection, table, as_of: date) -> tuple[dict[int, int], date | None]:
    """Остатки на последнюю дату среза ≤ as_of (по FBO — сумма по складам)."""
    last = conn.execute(select(func.max(table.c.snapshot_date)).where(table.c.snapshot_date <= as_of)).scalar()
    if last is None:
        return {}, None
    rows = conn.execute(select(table.c.product_size_id, func.sum(table.c.qty))
                        .where(table.c.snapshot_date == last).group_by(table.c.product_size_id))
    return {psid: int(qty or 0) for psid, qty in rows}, last


def fbo_in_transit(conn: Connection, as_of: date,
                   on_cs_statuses: frozenset[str]) -> tuple[dict[int, int], dict[int, int]]:
    """«В пути на FBO»: незавершённые поставки, план минус уже принятое Lamoda.

    Второй словарь — часть «в пути», которая ещё числится в остатке ЦС из 1С: отгрузку
    списывают в 1С после создания поставки в Lamoda, поэтому пока поставка не доехала до
    склада Lamoda, её товар виден и в поставке, и на ЦС. Незнакомый статус считаем
    «ещё на ЦС» — так не предложим отгрузить товар, который уже обещан Lamoda."""
    out: dict[int, int] = defaultdict(int)
    on_cs: dict[int, int] = defaultdict(int)
    rows = conn.execute(select(t_fbo_shipments.c.product_size_id, t_fbo_shipments.c.qty,
                               t_fbo_shipments.c.qty_accepted, t_fbo_shipments.c.status)
                        .where(t_fbo_shipments.c.created_date >= as_of - timedelta(days=IN_TRANSIT_MAX_AGE_DAYS)))
    for psid, qty, accepted, status in rows:
        status = (status or "").upper()
        if status in SHIPMENT_FINAL_STATUSES:
            continue
        remaining = max(0, int(qty or 0) - int(accepted or 0))
        out[psid] += remaining
        if status in on_cs_statuses or status not in KNOWN_SHIPMENT_STATUSES:
            on_cs[psid] += remaining
    return dict(out), dict(on_cs)


def assembled_not_in_lamoda(conn: Connection, as_of: date) -> dict[int, int]:
    """Собрано на странице («Собрать поставку»), но поставки в Lamoda по этому размеру с даты
    сборки ещё нет. Такое количество уже распределено: в «в пути» для FBO оно входит (иначе
    завтрашний пересчёт предложил бы отгрузить то же самое второй раз), а из доступного на
    ЦС вычитается. Как только в Lamoda появится поставка с этим размером, созданная в день
    сборки или позже, считаем, что это она, — и учитываем уже её."""
    rows = conn.execute(
        select(t_shipment_recommendations.c.product_size_id, t_shipment_recommendations.c.confirmed_qty,
               t_job_runs.c.started_at)
        .join(t_job_runs, t_job_runs.c.run_id == t_shipment_recommendations.c.assembly_id)
        .where(t_shipment_recommendations.c.status == "sent_to_edo",
               t_job_runs.c.started_at >= datetime.combine(as_of - timedelta(days=ASSEMBLED_PENDING_DAYS),
                                                           datetime.min.time()))).all()
    if not rows:
        return {}
    latest_shipment = dict(conn.execute(
        select(t_fbo_shipments.c.product_size_id, func.max(t_fbo_shipments.c.created_date))
        .group_by(t_fbo_shipments.c.product_size_id)).all())
    out: dict[int, int] = defaultdict(int)
    for psid, qty, assembled_at in rows:
        created = latest_shipment.get(psid)
        if created is not None and created >= msk_date(assembled_at):
            continue
        out[psid] += int(qty or 0)
    return dict(out)


def purchase_on_order(conn: Connection, as_of: date, grace_days: int) -> dict[int, int]:
    """Заказанное у поставщика, но ещё не пришедшее: строки 'ordered' до срока готовности + запас.
    После этого считаем, что товар уже пришёл в 1С и виден в остатке ЦС."""
    out: dict[int, int] = defaultdict(int)
    rows = conn.execute(select(t_purchase_recommendations.c.product_size_id, t_purchase_recommendations.c.ordered_qty,
                               t_purchase_recommendations.c.ready_by_date)
                        .where(t_purchase_recommendations.c.status == "ordered"))
    for psid, qty, ready_by in rows:
        if ready_by is None or ready_by + timedelta(days=grace_days) >= as_of:
            out[psid] += int(qty or 0)
    return dict(out)


def fbo_lead_days(conn: Connection, default: int, computed_at: datetime) -> tuple[float, str]:
    """Медиана «создание поставки → приёмка» по поставкам, закрытие которых мы видели сами."""
    rows = conn.execute(select(t_fbo_shipments.c.shipment_id, func.min(t_fbo_shipments.c.created_date),
                               func.max(t_fbo_shipments.c.actual_arrival_date))
                        .where(t_fbo_shipments.c.actual_arrival_date.is_not(None))
                        .group_by(t_fbo_shipments.c.shipment_id)).all()
    durations = [(arrived - created).days for _, created, arrived in rows if created and arrived]
    durations = [d for d in durations if d >= 0]
    if len(durations) < MIN_SHIPMENTS_FOR_LEAD:
        return float(default), "по настройке"
    lead = float(median(durations))
    mean = sum(durations) / len(durations)
    std = (sum((d - mean) ** 2 for d in durations) / len(durations)) ** 0.5
    upsert(conn, t_lead_time_stats, [{"scope_type": "global", "scope_id": "all", "lead_type": "fbo_delivery",
                                      "median_days": lead, "std_days": std, "computed_at": computed_at}],
           keys=["scope_type", "scope_id", "lead_type"])
    return lead, f"медиана по {len(durations)} поставкам"


@dataclass
class Forecast:
    channel: str
    fit: smoothing.Fit
    stockout_days: int
    history_days: int


def forecast_series(series: DemandSeries, settings: PlanningSettings) -> Forecast:
    f = smoothing.fit(series.training, alpha=settings.smoothing_alpha, min_history=settings.min_history_days,
                      outlier_sigma=settings.outlier_sigma)
    return Forecast(series.channel, f, series.stockout_days, len(series.values))


def describe_forecast(fc: Forecast) -> str:
    f = fc.fit
    if f.method == "none":
        return "продаж не было"
    parts = [f"{fmt(f.level)} шт/день"]
    if f.method == "mean":
        parts.append(f"мало истории: {f.n} дн., взято среднее")
    else:
        parts.append(f"{f.n} дн. истории, сглаживание α={fmt(f.alpha)}")
    if fc.stockout_days:
        parts.append(f"без остатка исключено {fc.stockout_days} дн.")
    if f.outliers:
        parts.append(f"выбросов в σ отброшено {f.outliers}")
    return ", ".join(parts)


def describe_policy(policy: Policy, lead_note: str) -> str:
    return (f"L = {lead_note} = {fmt(policy.lead_days, 1)} дн.; страховой запас "
            f"{fmt(policy.z)}×{fmt(policy.sigma)}×√{fmt(policy.lead_days, 1)} = {fmt(policy.safety_stock, 1)}; "
            f"точка заказа {fmt(policy.daily_demand)}×{fmt(policy.lead_days, 1)} + {fmt(policy.safety_stock, 1)} "
            f"= {fmt(policy.reorder_point, 1)}")


def recalc(conn: Connection, settings: PlanningSettings, as_of: date, batch_id: int, computed_at: datetime) -> dict:
    report: dict = {"as_of": as_of.isoformat()}
    resolver = SkuResolver.load(conn)
    report["order_items_remapped"] = remap_unmapped_order_items(conn, resolver)
    report["sales_daily_rows"] = rebuild_sales_daily(conn)

    catalog = load_catalog(conn)
    stock_cs, stock_cs_date = latest_snapshot(conn, t_stock_1c, as_of)
    stock_fbo, stock_fbo_date = latest_snapshot(conn, t_stock_fbo, as_of)
    in_transit, transit_on_cs = fbo_in_transit(conn, as_of, settings.fbo_statuses_on_cs)
    assembled = assembled_not_in_lamoda(conn, as_of)
    on_order = purchase_on_order(conn, as_of, settings.purchase_on_order_grace_days)
    lead_fbo, lead_fbo_note = fbo_lead_days(conn, settings.fbo_lead_days_default, computed_at)
    report.update(stock_1c_date=stock_cs_date and stock_cs_date.isoformat(),
                  stock_fbo_date=stock_fbo_date and stock_fbo_date.isoformat(),
                  fbo_lead_days=lead_fbo, fbo_lead_source=lead_fbo_note)

    # --- прогноз по каналам ---
    lam_series = lamoda_series(conn, as_of, settings.history_days)
    report["demand_timeseries_rows"] = store_lamoda_timeseries(conn, lam_series, as_of, settings.history_days)
    forecasts: dict[int, dict[str, Forecast]] = defaultdict(dict)
    for psid, s in lam_series.items():
        if psid in catalog:
            forecasts[psid]["lamoda"] = forecast_series(s, settings)
    other, unmatched_channel = channel_series(conn, resolver, as_of, settings.history_days)
    report["channel_sales_unmatched"] = unmatched_channel
    for channel, by_size in other.items():
        for psid, s in by_size.items():
            if psid in catalog:
                forecasts[psid][channel] = forecast_series(s, settings)

    horizon = settings.forecast_horizon_days
    period_start, period_end = as_of, as_of + timedelta(days=horizon - 1)
    z = settings.service_level_z
    forecast_rows, aggregate_rows = [], []
    for psid, by_channel in forecasts.items():
        breakdown = {}
        for channel, fc in by_channel.items():
            qty = fc.fit.level * horizon
            spread = z * fc.fit.sigma * horizon ** 0.5
            forecast_rows.append({
                "product_size_id": psid, "channel": channel, "period_start": period_start, "period_end": period_end,
                "forecast_qty": round(qty, 2), "lower_bound": round(max(0.0, qty - spread), 2),
                "upper_bound": round(qty + spread, 2),
                "model_type": "smoothing" if fc.fit.method == "smoothing" else "mean",
                "computed_at": computed_at,
            })
            breakdown[channel] = round(qty, 2)
        aggregate_rows.append({"product_size_id": psid, "period_start": period_start, "period_end": period_end,
                               "total_forecast_qty": round(sum(breakdown.values()), 2),
                               "channel_breakdown": breakdown, "computed_at": computed_at})
    upsert(conn, t_demand_forecast, forecast_rows, keys=["product_size_id", "channel", "period_start", "period_end"])
    upsert(conn, t_central_stock_aggregate_demand, aggregate_rows,
           keys=["product_size_id", "period_start", "period_end"])
    report["forecasts"] = len(forecast_rows)

    report["shipment"] = _shipment_batch(conn, settings, catalog, forecasts, stock_cs, stock_fbo, in_transit,
                                         transit_on_cs, assembled, lead_fbo, lead_fbo_note, batch_id,
                                         computed_at, as_of)
    report["purchase"] = _purchase_batch(conn, settings, catalog, forecasts, stock_cs, stock_fbo, in_transit,
                                         transit_on_cs, on_order, batch_id, as_of)
    return report


def _shipment_batch(conn, settings, catalog, forecasts, stock_cs, stock_fbo, in_transit, transit_on_cs, assembled,
                    lead_fbo, lead_fbo_note, batch_id, computed_at, as_of) -> dict:
    """Страница 1: поставка с ЦС на FBO Lamoda. Только Lamoda-спрос (FBO есть только у неё)."""
    confirmed = {r.product_size_id: r for r in conn.execute(
        select(t_shipment_recommendations).where(t_shipment_recommendations.c.status == "confirmed"))}
    lead_days = lead_fbo + settings.review_interval_days
    lead_note = f"{fmt(lead_fbo, 1)} (поставка на FBO, {lead_fbo_note}) + {settings.review_interval_days} (пересмотр)"
    lamoda_sizes = {psid for psid, info in catalog.items()
                    if info.lamoda_sku or "lamoda" in forecasts.get(psid, {}) or psid in stock_fbo
                    or psid in in_transit or psid in assembled}
    reorder_rows, rows = [], []
    for psid in sorted(lamoda_sizes):
        info = catalog[psid]
        fc = forecasts.get(psid, {}).get("lamoda")
        d = fc.fit.level if fc else 0.0
        sigma = fc.fit.sigma if fc else 0.0
        on_fbo = stock_fbo.get(psid, 0)
        packed = assembled.get(psid, 0)
        transit = in_transit.get(psid, 0) + packed
        policy = order_up_to(d, sigma, lead_days, settings.service_level_z, max(on_fbo, 0) + transit)
        reorder_rows.append({"product_size_id": psid, "computed_at": computed_at, "lead_time_used_days": lead_days,
                             "sigma_used": round(sigma, 3), "formula_variant": "basic",
                             "safety_stock": round(policy.safety_stock, 2),
                             "reorder_point_qty": round(policy.reorder_point, 2)})
        cs = stock_cs.get(psid, 0)
        prior = confirmed.get(psid)
        if cs <= 0 and prior is None:
            continue
        # уже обещано Lamoda, но ещё в остатке ЦС: собрано у нас + поставки Lamoda, не списанные в 1С
        committed = packed + transit_on_cs.get(psid, 0)
        available = max(0, cs - settings.cs_reserve_qty - committed)
        qty = min(policy.need, available)
        transit_note = f"в пути {transit}" + (f", из них собрано у нас и ещё не создано в Lamoda {packed}"
                                              if packed else "")
        notes = [f"Спрос Lamoda: {describe_forecast(fc) if fc else 'продаж не было'}".rstrip(".") + ".",
                 describe_policy(policy, lead_note) + ".",
                 f"Есть {fmt(policy.position, 0)} (FBO {on_fbo} + {transit_note}) → не хватает {policy.need}."]
        if info.status in NO_REPLENISH_STATUSES:
            qty = 0
            notes.append(f"Модель — {NO_REPLENISH_STATUSES[info.status]}: пополнение не считаем.")
        elif info.collection == "capsule":
            qty = 0
            notes.append("Капсульная коллекция: в регулярный цикл пополнения не входит.")
        elif policy.need > available:
            notes.append(f"На ЦС {cs}" + (f", резерв ЦС {settings.cs_reserve_qty}" if settings.cs_reserve_qty else "")
                         + (f", уже собрано в поставку {packed}" if packed else "")
                         + (f", в поставках Lamoda, ещё не списанных в 1С, {transit_on_cs[psid]}"
                            if transit_on_cs.get(psid) else "")
                         + f" → к отгрузке {qty}.")
        row = {"product_size_id": psid, "stock_1c_qty_snapshot": cs, "stock_fbo_qty_snapshot": on_fbo,
               "qty_to_ship": qty, "confidence_pct": None, "confidence_explanation": " ".join(notes),
               "status": "pending", "batch_id": batch_id, "created_at": computed_at, "target_ship_date": None}
        if prior is not None:
            row.update(confirmed_qty=prior.confirmed_qty, confirmed_by=prior.confirmed_by,
                       confirmed_at=prior.confirmed_at, status="confirmed")
        rows.append(row)
    if reorder_rows:
        conn.execute(t_reorder_points.insert(), reorder_rows)
    conn.execute(update(t_shipment_recommendations)
                 .where(t_shipment_recommendations.c.status.in_(["pending", "confirmed"]))
                 .values(status="superseded"))
    if rows:
        conn.execute(t_shipment_recommendations.insert(), rows)
    return {"rows": len(rows), "to_ship": sum(1 for r in rows if r["qty_to_ship"] > 0),
            "units": sum(r["qty_to_ship"] for r in rows), "confirmed_carried": sum(1 for r in rows if r["status"] == "confirmed"),
            "reorder_points": len(reorder_rows), "lead_days": lead_days}


def _purchase_batch(conn, settings, catalog, forecasts, stock_cs, stock_fbo, in_transit, transit_on_cs, on_order,
                    batch_id, as_of) -> dict:
    """Страница 2: закупка у поставщика по СУММАРНОМУ спросу всех каналов, тянущих с ЦС."""
    z = settings.service_level_z
    rows = []
    model_totals: dict[int, int] = defaultdict(int)
    for psid, info in sorted(catalog.items()):
        by_channel = forecasts.get(psid, {})
        if info.status in NO_REPLENISH_STATUSES or info.collection == "capsule" or not by_channel:
            continue
        d = sum(fc.fit.level for fc in by_channel.values())
        if d <= 0:
            continue
        sigma = sum(fc.fit.sigma ** 2 for fc in by_channel.values()) ** 0.5   # независимые каналы
        if info.supplier_id is not None:
            supplier_lead = (info.production_days or 0) + (info.delivery_days or 0)
            supplier_note = (f"{info.production_days or 0} (производство) + {info.delivery_days or 0} (доставка, "
                             f"{info.supplier_name})")
        else:
            supplier_lead = settings.supplier_lead_days_default
            supplier_note = f"{supplier_lead} (поставщик не указан — срок по умолчанию)"
        lead_days = supplier_lead + settings.review_interval_days
        cs, fbo, ordered = max(stock_cs.get(psid, 0), 0), max(stock_fbo.get(psid, 0), 0), on_order.get(psid, 0)
        # товар поставки, ещё не списанный в 1С, уже сидит в остатке ЦС — второй раз «в пути» не считаем
        still_on_cs = min(transit_on_cs.get(psid, 0), in_transit.get(psid, 0))
        transit = in_transit.get(psid, 0) - still_on_cs
        policy = order_up_to(d, sigma, lead_days, z, cs + fbo + transit + ordered)
        if policy.need <= 0:
            continue
        channels = " + ".join(f"{channel_name(c)} {fmt(fc.fit.level)}" for c, fc in sorted(by_channel.items())
                              if fc.fit.level >= 0.005)
        notes = [f"Спрос всех каналов {fmt(d)} шт/день ({channels}).",
                 describe_policy(policy, f"{supplier_note} + {settings.review_interval_days} (пересмотр)") + ".",
                 f"Есть {fmt(policy.position, 0)} (ЦС {cs} + FBO {fbo} + в пути на FBO {transit}"
                 + (f" + заказано {ordered}" if ordered else "") + f") → к закупке {policy.need}."
                 + (f" Ещё {still_on_cs} шт. в поставках Lamoda не списаны с ЦС в 1С — учтены в остатке ЦС."
                    if still_on_cs else "")]
        status = "pending"
        if not info.available_to_reorder:
            status = "no_reorder"
            notes = [f"SKU без пополнения, идёт на исчерпание остатка (не доступен к заказу у поставщика). "
                     f"По расчёту не хватает {policy.need} шт."] + notes[:1]
        else:
            model_totals[info.model_id] += policy.need
        rows.append({
            "product_size_id": psid, "supplier_id": info.supplier_id,
            "qty_to_purchase": policy.need if status == "pending" else 0,
            "ready_by_date": as_of + timedelta(days=supplier_lead), "status": status, "batch_id": batch_id,
            "stock_1c_qty_snapshot": cs, "stock_fbo_qty_snapshot": fbo, "in_transit_qty_snapshot": transit,
            "on_order_qty_snapshot": ordered, "daily_demand": round(d, 3),
            "reorder_point_qty": round(policy.reorder_point, 2), "explanation": " ".join(notes),
            "_model_id": info.model_id, "_moq": info.moq,
        })
    for row in rows:
        model_id, moq = row.pop("_model_id"), row.pop("_moq")
        total = model_totals.get(model_id, 0)
        if row["status"] == "pending" and moq and total < moq:
            row["explanation"] += f" По модели к закупке {total} шт. — меньше MOQ поставщика ({moq})."
    conn.execute(update(t_purchase_recommendations)
                 .where(t_purchase_recommendations.c.status.in_(["pending", "no_reorder"]))
                 .values(status="superseded"))
    if rows:
        conn.execute(t_purchase_recommendations.insert(), rows)
    return {"rows": sum(1 for r in rows if r["status"] == "pending"),
            "no_reorder": sum(1 for r in rows if r["status"] == "no_reorder"),
            "units": sum(r["qty_to_purchase"] for r in rows)}

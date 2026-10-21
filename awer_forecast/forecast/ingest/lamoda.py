"""Приём данных Lamoda в БД: остатки FBO, поставки FBO («в пути»), заказы.

Всё идемпотентно: повторный запуск за тот же период ничего не задваивает.
Заказы — по ключу «id заказа + id позиции + статус» (UNIQUE в order_items): тот же
статус той же позиции второй раз не вставляется, новый статус — новая строка.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.engine import Connection, Engine

from forecast.catalog.resolver import SkuResolver, normalize_code
from forecast.db.ops import insert_ignore, upsert
from forecast.db.schema import t_fbo_shipments, t_order_items, t_sales_daily, t_stock_fbo
from forecast.integrations.lamoda.client import LamodaClient, parse_datetime
from forecast.integrations.lamoda.money import money_from_api
from forecast.integrations.lamoda.statuses import (
    KNOWN_ITEM_STATUSES, KNOWN_SHIPMENT_STATUSES, RETURN_STATUSES, SALE_STATUSES,
    SHIPMENT_ARRIVED_STATUSES, SHIPMENT_FINAL_STATUSES, normalize_status,
)
from forecast.timeutil import msk_date, to_naive_utc

logger = logging.getLogger(__name__)
MAX_LISTED = 200
ORDERS_COMMIT_EVERY = 50


def _remember(bucket: list, item, limit: int = MAX_LISTED) -> None:
    if len(bucket) < limit and item not in bucket:
        bucket.append(item)


# ---------------- остатки FBO ----------------

def ingest_fbo_stocks(conn: Connection, stocks, resolver: SkuResolver, snapshot_date: date) -> dict:
    """Снимок `GET /v2/fbo/stocks` на дату. Позиции с нулём тоже пишутся — ноль и есть
    признак stockout для цензурирования спроса (раздел 5.1)."""
    totals: dict[tuple[int, str], int] = defaultdict(int)
    report = {"rows": 0, "matched": 0, "unmatched": [], "units": 0}
    for row in stocks:
        report["rows"] += 1
        psid = resolver.resolve(lamoda_sku=row.get("sku"), external_sku=row.get("externalSku"))
        quantity = int(row.get("quantity") or 0)
        if psid is None:
            _remember(report["unmatched"], {"sku": row.get("sku"), "externalSku": row.get("externalSku"),
                                            "quantity": quantity})
            continue
        report["matched"] += 1
        warehouse = str(row.get("warehouseCode") or "FBO")
        totals[(psid, warehouse)] += quantity
        report["units"] += quantity
    rows = [{"product_size_id": psid, "warehouse_code": wh, "snapshot_date": snapshot_date, "qty": qty}
            for (psid, wh), qty in totals.items()]
    upsert(conn, t_stock_fbo, rows, keys=["product_size_id", "warehouse_code", "snapshot_date"], update=["qty"])
    report["unmatched_total"] = report["rows"] - report["matched"]
    return report


# ---------------- поставки FBO ----------------

def ingest_fbo_shipments(conn: Connection, client: LamodaClient, resolver: SkuResolver, today: date,
                         lookback_days: int = 120) -> dict:
    """Поставки на FBO за последние `lookback_days` по плановой дате.

    Незавершённые (не CLOSED/RECEIVED/CANCELLED/…) — это «в пути на FBO»: по ним
    забираются товары и плановое/принятое количество. У завершённых обновляется только
    статус; дата приёмки ставится, если мы застали переход в завершённый статус сами
    (поставка, впервые увиденная уже закрытой, даты приёмки не получает — иначе
    статистика срока поставки посчитала бы срок до сегодняшнего дня)."""
    report = {"shipments": 0, "in_transit": 0, "closed_now": 0, "items": 0, "unmatched_items": [],
              "unknown_statuses": []}
    known = {r.shipment_id: r.status for r in conn.execute(
        select(t_fbo_shipments.c.shipment_id, t_fbo_shipments.c.status).distinct())}
    for shipment in client.iter_fbo_shipments(planned_from=today - timedelta(days=lookback_days)):
        report["shipments"] += 1
        shipment_id = str(shipment.get("id") or "")
        if not shipment_id:
            continue
        status = normalize_status(shipment.get("status"))
        if status not in KNOWN_SHIPMENT_STATUSES:
            _remember(report["unknown_statuses"], status)
            logger.warning("Lamoda: незнакомый статус поставки FBO %s (%s) — считаю «в пути»", status, shipment_id)
        if status in SHIPMENT_FINAL_STATUSES:
            previous = known.get(shipment_id)
            if previous is not None and previous != status:
                values = {"status": status}
                if status in SHIPMENT_ARRIVED_STATUSES and previous not in SHIPMENT_FINAL_STATUSES:
                    values["actual_arrival_date"] = today
                    report["closed_now"] += 1
                conn.execute(update(t_fbo_shipments).where(t_fbo_shipments.c.shipment_id == shipment_id)
                             .values(**values))
            continue
        report["in_transit"] += 1
        created = parse_datetime(shipment.get("createdAt"))
        planned = shipment.get("plannedDate")
        per_size: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        for item in client.iter_fbo_shipment_items(shipment_id):
            report["items"] += 1
            psid = resolver.resolve(barcodes=[item.get("ean")] if item.get("ean") else [],
                                    lamoda_sku=item.get("sku"))
            if psid is None:
                _remember(report["unmatched_items"], {"shipment": shipment_id, "sku": item.get("sku"),
                                                      "ean": item.get("ean")})
                continue
            per_size[psid][0] += int(item.get("quantityPlanned") or 0)
            per_size[psid][1] += int(item.get("quantityAccepted") or 0)
        rows = [{
            "shipment_id": shipment_id, "product_size_id": psid, "qty": planned_qty, "qty_accepted": accepted,
            "created_date": msk_date(created) if created else today,
            "created_at": to_naive_utc(created) if created else None,
            "expected_arrival_date": date.fromisoformat(planned[:10]) if planned else None,
            "status": status, "warehouse_code": shipment.get("warehouseCode"),
        } for psid, (planned_qty, accepted) in per_size.items()]
        upsert(conn, t_fbo_shipments, rows, keys=["shipment_id", "product_size_id"],
               update=["qty", "qty_accepted", "expected_arrival_date", "status", "warehouse_code", "created_at"])
    return report


# ---------------- заказы ----------------

def parse_order_items(order: dict, resolver: SkuResolver) -> list[dict]:
    """Строки order_items из ответа `GET /v2/orders/{id}`.

    `order_id` — внутренний `id` заказа (им же адресуются все методы /v2/orders/{…});
    `tracking_id` — `orderId`, номер заказа для людей и группировки вебхуков.
    Дата события — день последнего обновления заказа по Москве: точной даты смены
    статуса позиции в ответе нет (допущение Фазы 1, см. README)."""
    order_id = str(order.get("id") or "")
    updated = parse_datetime(order.get("updatedAt")) or parse_datetime(order.get("createdAt"))
    if not order_id or updated is None:
        return []
    event_date = msk_date(updated)
    rows = []
    for item in order.get("items") or []:
        status = normalize_status(item.get("status"))
        item_id = str(item.get("id") or "")
        if not status or not item_id:
            continue
        if status not in KNOWN_ITEM_STATUSES:
            logger.warning("Lamoda: незнакомый статус позиции %s (заказ %s) — сохранён, в спрос не идёт",
                           status, order_id)
        lamoda_sku = normalize_code(item.get("sku")) or None
        external_sku = normalize_code(item.get("externalSku")) or None
        rows.append({
            "order_id": order_id,
            "tracking_id": str(order.get("orderId") or "") or None,
            "lamoda_item_id": item_id,
            "product_size_id": resolver.resolve(lamoda_sku=lamoda_sku, external_sku=external_sku),
            "status": status,
            "price_actual": money_from_api(item.get("paidPrice")),
            "price_base": money_from_api(item.get("basePrice")),
            "total_discount": money_from_api(item.get("totalDiscount")),
            "event_date": event_date,
            "lamoda_sku": lamoda_sku,
            "external_sku": external_sku,
        })
    return rows


def ingest_orders(engine: Engine, client: LamodaClient, resolver: SkuResolver,
                  updated_from: datetime, updated_to: datetime) -> dict:
    """Опрос заказов, обновлённых в [updated_from, updated_to). По каждому — детали с
    позициями. Фиксация каждые ORDERS_COMMIT_EVERY заказов: длинная транзакция на
    SQLite держала бы блокировку записи и мешала подтверждать поставки на странице."""
    report = {"orders": 0, "items_seen": 0, "items_inserted": 0, "unmapped_items": 0,
              "max_updated_at": None, "unknown_statuses": []}
    pending: list[dict] = []

    def flush():
        if pending:
            with engine.begin() as conn:
                report["items_inserted"] += insert_ignore(
                    conn, t_order_items, pending, keys=["order_id", "lamoda_item_id", "status"])
            pending.clear()

    for summary in client.iter_orders(updated_from, updated_to):
        report["orders"] += 1
        detail = client.get_order(summary["id"])
        rows = parse_order_items(detail, resolver)
        for row in rows:
            if row["status"] not in KNOWN_ITEM_STATUSES:
                _remember(report["unknown_statuses"], row["status"], 50)
            if row["product_size_id"] is None:
                report["unmapped_items"] += 1
        report["items_seen"] += len(rows)
        pending.extend(rows)
        updated = parse_datetime(summary.get("updatedAt"))
        if updated and (report["max_updated_at"] is None or updated.isoformat() > report["max_updated_at"]):
            report["max_updated_at"] = updated.isoformat()
        if report["orders"] % ORDERS_COMMIT_EVERY == 0:
            flush()
    flush()
    return report


def remap_unmapped_order_items(conn: Connection, resolver: SkuResolver) -> int:
    """Позиции, не сопоставленные при приёме, — ещё раз после пополнения справочника."""
    pairs = conn.execute(
        select(t_order_items.c.lamoda_sku, t_order_items.c.external_sku)
        .where(t_order_items.c.product_size_id.is_(None)).distinct()
    ).all()
    fixed = 0
    for lamoda_sku, external_sku in pairs:
        psid = resolver.resolve(lamoda_sku=lamoda_sku, external_sku=external_sku)
        if psid is None:
            continue
        condition = [t_order_items.c.product_size_id.is_(None)]
        condition.append(t_order_items.c.lamoda_sku == lamoda_sku if lamoda_sku is not None
                         else t_order_items.c.lamoda_sku.is_(None))
        condition.append(t_order_items.c.external_sku == external_sku if external_sku is not None
                         else t_order_items.c.external_sku.is_(None))
        fixed += conn.execute(update(t_order_items).where(and_(*condition))
                              .values(product_size_id=psid)).rowcount or 0
    return fixed


def rebuild_sales_daily(conn: Connection) -> int:
    """sales_daily заново из order_items (объём маленький — десятки тысяч позиций в год).

    Продажа — позиция в статусе DELIVERED, день — дата этой строки. Возврат — первая
    строка позиции в статусе возврата, и только если её выкуп (DELIVERED) у нас есть:
    позиция, у которой мы застали один возврат (выкуп был до начала истории), в спрос
    не идёт ни плюсом, ни минусом — иначе история начиналась бы с ложного минуса."""
    delivered = {}
    for r in conn.execute(select(t_order_items.c.order_id, t_order_items.c.lamoda_item_id,
                                 t_order_items.c.product_size_id, t_order_items.c.event_date,
                                 t_order_items.c.price_actual)
                          .where(t_order_items.c.status.in_(SALE_STATUSES),
                                 t_order_items.c.product_size_id.is_not(None))):
        delivered[(r.order_id, r.lamoda_item_id)] = r
    returns = {}
    for r in conn.execute(select(t_order_items.c.order_id, t_order_items.c.lamoda_item_id,
                                 func.min(t_order_items.c.event_date).label("event_date"))
                          .where(t_order_items.c.status.in_(RETURN_STATUSES))
                          .group_by(t_order_items.c.order_id, t_order_items.c.lamoda_item_id)):
        returns[(r.order_id, r.lamoda_item_id)] = r.event_date

    agg: dict[tuple[int, date], dict] = defaultdict(lambda: {"qty_delivered": 0, "qty_returned": 0, "revenue": 0})
    for key, row in delivered.items():
        bucket = agg[(row.product_size_id, row.event_date)]
        bucket["qty_delivered"] += 1
        bucket["revenue"] += row.price_actual or 0
        returned_on = returns.get(key)
        if returned_on is not None:
            back = agg[(row.product_size_id, returned_on)]
            back["qty_returned"] += 1
            back["revenue"] -= row.price_actual or 0
    conn.execute(delete(t_sales_daily))
    rows = [{"product_size_id": psid, "sale_date": day, **values} for (psid, day), values in agg.items()]
    if rows:
        conn.execute(t_sales_daily.insert(), rows)
    return len(rows)

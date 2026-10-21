"""Данные и действия страниц: строки последнего пересчёта, подтверждение, сборка поставки."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import desc, func, select, update
from sqlalchemy.engine import Connection, Engine

from forecast.db.schema import (
    t_barcode_pool, t_job_runs, t_order_items, t_product_models, t_product_sizes, t_purchase_recommendations,
    t_shipment_recommendations, t_stock_1c, t_stock_fbo, t_suppliers,
)
from forecast.timeutil import utcnow

FBO_COLUMNS = ["Артикул", "Цвет", "Размер", "Остаток ЦС", "Остаток FBO", "Рекомендация к отгрузке",
               "Достоверность, %", "Пояснение", "Подтверждено к поставке", "Lamoda SKU", "Баркод"]
PURCHASE_COLUMNS = ["Артикул", "Цвет", "Размер", "Баркод", "Поставщик", "Остаток ЦС", "Остаток FBO",
                    "В пути на FBO", "Заказано ранее", "Спрос в день (все каналы)", "Точка заказа", "К закупке",
                    "Срок готовности", "Пояснение"]


class InputError(ValueError):
    pass


def parse_qty(raw: str) -> int | None:
    """Поле количества со страницы: пусто → None, иначе целое ≥ 0."""
    text = (raw or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise InputError(f"«{raw}» — не число")
    if value < 0 or value != value.to_integral_value():
        raise InputError(f"«{raw}» — нужно целое число штук, не меньше нуля")
    return int(value)


def _first_barcodes(conn: Connection) -> dict[int, str]:
    out = {}
    for psid, barcode in conn.execute(select(t_barcode_pool.c.product_size_id, t_barcode_pool.c.barcode)
                                      .where(t_barcode_pool.c.valid_to.is_(None))
                                      .order_by(t_barcode_pool.c.barcode_id)):
        out.setdefault(psid, barcode)
    return out


def latest_batch(conn: Connection, table) -> int | None:
    return conn.execute(select(func.max(table.c.batch_id))).scalar()


def batch_info(conn: Connection, batch_id: int | None) -> dict | None:
    if batch_id is None:
        return None
    row = conn.execute(select(t_job_runs).where(t_job_runs.c.run_id == batch_id)).first()
    if row is None:
        return {"run_id": batch_id}
    details = row.details or {}
    return {"run_id": batch_id, "started_at": row.started_at, "stock_1c_date": details.get("stock_1c_date"),
            "stock_fbo_date": details.get("stock_fbo_date"), "fbo_lead_days": details.get("fbo_lead_days")}


@dataclass
class FboRow:
    recommendation_id: int
    article: str
    color: str
    size: str
    stock_cs: int
    stock_fbo: int
    qty_to_ship: int
    confidence_pct: float | None
    explanation: str
    confirmed_qty: int | None
    lamoda_sku: str
    barcode: str
    status: str
    confirmed_by: str | None
    assembly_id: int | None

    def excel(self) -> list:
        return [self.article, self.color, self.size, self.stock_cs, self.stock_fbo, self.qty_to_ship,
                self.confidence_pct, self.explanation, self.confirmed_qty, self.lamoda_sku, self.barcode]

    @property
    def over_stock(self) -> bool:
        return self.confirmed_qty is not None and self.confirmed_qty > self.stock_cs


def _int(value) -> int:
    return int(value or 0)


def fbo_rows(conn: Connection, *, batch_id: int | None = None, assembly_id: int | None = None,
             show_all: bool = True, query: str = "") -> list[FboRow]:
    t = t_shipment_recommendations
    stmt = (select(t, t_product_sizes.c.size_label, t_product_sizes.c.lamoda_sku, t_product_models.c.article,
                   t_product_models.c.color)
            .join(t_product_sizes, t_product_sizes.c.product_size_id == t.c.product_size_id)
            .join(t_product_models, t_product_models.c.product_model_id == t_product_sizes.c.product_model_id))
    if assembly_id is not None:
        stmt = stmt.where(t.c.assembly_id == assembly_id)
    else:
        if batch_id is None:
            return []
        stmt = stmt.where(t.c.batch_id == batch_id)
        if not show_all:
            stmt = stmt.where((t.c.qty_to_ship > 0) | t.c.confirmed_qty.is_not(None))
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(t_product_models.c.article.ilike(like) | t_product_models.c.color.ilike(like))
    # размеры — в порядке справочника (как в карте мэппинга: 46, 48… / S, M, L), а не по алфавиту
    stmt = stmt.order_by(t_product_models.c.article, t_product_models.c.color, t_product_sizes.c.product_size_id)
    barcodes = _first_barcodes(conn)
    return [FboRow(
        recommendation_id=r.recommendation_id, article=r.article, color=r.color or "", size=r.size_label,
        stock_cs=_int(r.stock_1c_qty_snapshot), stock_fbo=_int(r.stock_fbo_qty_snapshot),
        qty_to_ship=_int(r.qty_to_ship), confidence_pct=float(r.confidence_pct) if r.confidence_pct is not None else None,
        explanation=r.confidence_explanation or "",
        confirmed_qty=int(r.confirmed_qty) if r.confirmed_qty is not None else None,
        lamoda_sku=r.lamoda_sku or "", barcode=barcodes.get(r.product_size_id, ""), status=r.status,
        confirmed_by=r.confirmed_by, assembly_id=r.assembly_id,
    ) for r in conn.execute(stmt)]


def save_confirmed(engine: Engine, values: dict[int, str], user: str) -> tuple[int, list[str]]:
    """Сохраняет «подтверждено к поставке»: заполненное → confirmed, очищенное → pending.
    Строки, уже собранные в поставку, не меняются."""
    errors, changed = [], 0
    parsed = {}
    for rec_id, raw in values.items():
        try:
            parsed[rec_id] = parse_qty(raw)
        except InputError as exc:
            errors.append(str(exc))
    if errors:
        return 0, errors
    t = t_shipment_recommendations
    with engine.begin() as conn:
        current = {r.recommendation_id: r for r in conn.execute(
            select(t.c.recommendation_id, t.c.confirmed_qty, t.c.status)
            .where(t.c.recommendation_id.in_(list(parsed))))}
        for rec_id, qty in parsed.items():
            row = current.get(rec_id)
            if row is None or row.status not in ("pending", "confirmed"):
                continue
            old = int(row.confirmed_qty) if row.confirmed_qty is not None else None
            if old == qty:
                continue
            if qty is None:
                conn.execute(update(t).where(t.c.recommendation_id == rec_id).values(
                    confirmed_qty=None, confirmed_by=None, confirmed_at=None, status="pending"))
            else:
                conn.execute(update(t).where(t.c.recommendation_id == rec_id).values(
                    confirmed_qty=qty, confirmed_by=user, confirmed_at=utcnow(), status="confirmed"))
            changed += 1
    return changed, []


def assemble_shipment(engine: Engine, user: str) -> tuple[int | None, int, int]:
    """«Собрать поставку»: только строки с заполненным «подтверждено» (> 0) последнего
    пересчёта. Они получают номер сборки и статус sent_to_edo; выгрузка — по номеру."""
    from forecast.jobs import finish_job, start_job

    t = t_shipment_recommendations
    with engine.connect() as conn:
        batch_id = latest_batch(conn, t)
        rows = conn.execute(select(t.c.recommendation_id, t.c.confirmed_qty)
                            .where(t.c.batch_id == batch_id, t.c.status == "confirmed",
                                   t.c.confirmed_qty > 0)).all()
    if not rows:
        return None, 0, 0
    run_id = start_job(engine, "fbo_assembly", user)
    with engine.begin() as conn:
        conn.execute(update(t).where(t.c.recommendation_id.in_([r.recommendation_id for r in rows]))
                     .values(assembly_id=run_id, status="sent_to_edo"))
    units = sum(int(r.confirmed_qty) for r in rows)
    finish_job(engine, run_id, "ok", f"строк {len(rows)}, штук {units}", {"rows": len(rows), "units": units,
                                                                          "batch_id": batch_id})
    return run_id, len(rows), units


@dataclass
class PurchaseRow:
    recommendation_id: int
    article: str
    color: str
    size: str
    barcode: str
    supplier: str
    stock_cs: int
    stock_fbo: int
    in_transit: int
    on_order: int
    daily_demand: float
    reorder_point: float
    qty: int
    ready_by: object
    explanation: str
    status: str
    ordered_qty: int | None
    ordered_by: str | None

    def excel(self) -> list:
        return [self.article, self.color, self.size, self.barcode, self.supplier, self.stock_cs, self.stock_fbo,
                self.in_transit, self.on_order, round(self.daily_demand, 3), round(self.reorder_point, 1),
                self.qty, self.ready_by, self.explanation]


def purchase_rows(conn: Connection, *, statuses: tuple[str, ...], batch_id: int | None = None,
                  query: str = "") -> list[PurchaseRow]:
    t = t_purchase_recommendations
    stmt = (select(t, t_product_sizes.c.size_label, t_product_models.c.article, t_product_models.c.color,
                   t_suppliers.c.name.label("supplier_name"))
            .join(t_product_sizes, t_product_sizes.c.product_size_id == t.c.product_size_id)
            .join(t_product_models, t_product_models.c.product_model_id == t_product_sizes.c.product_model_id)
            .outerjoin(t_suppliers, t_suppliers.c.supplier_id == t.c.supplier_id)
            .where(t.c.status.in_(statuses)))
    if batch_id is not None:
        stmt = stmt.where(t.c.batch_id == batch_id)
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(t_product_models.c.article.ilike(like) | t_product_models.c.color.ilike(like))
    stmt = stmt.order_by(t_suppliers.c.name, t_product_models.c.article, t_product_models.c.color,
                         t_product_sizes.c.product_size_id)
    barcodes = _first_barcodes(conn)
    return [PurchaseRow(
        recommendation_id=r.recommendation_id, article=r.article, color=r.color or "", size=r.size_label,
        barcode=barcodes.get(r.product_size_id, ""), supplier=r.supplier_name or "не указан",
        stock_cs=_int(r.stock_1c_qty_snapshot), stock_fbo=_int(r.stock_fbo_qty_snapshot),
        in_transit=_int(r.in_transit_qty_snapshot), on_order=_int(r.on_order_qty_snapshot),
        daily_demand=float(r.daily_demand or 0), reorder_point=float(r.reorder_point_qty or 0),
        qty=_int(r.qty_to_purchase), ready_by=r.ready_by_date, explanation=r.explanation or "", status=r.status,
        ordered_qty=int(r.ordered_qty) if r.ordered_qty is not None else None, ordered_by=r.ordered_by,
    ) for r in conn.execute(stmt)]


def save_ordered(engine: Engine, values: dict[int, str], user: str) -> tuple[int, list[str]]:
    """«Заказано у поставщика»: заполненное → ordered (до срока готовности считается «в пути»
    для закупки), очищенное у строки текущего пересчёта → снова pending."""
    errors, parsed = [], {}
    for rec_id, raw in values.items():
        try:
            parsed[rec_id] = parse_qty(raw)
        except InputError as exc:
            errors.append(str(exc))
    if errors:
        return 0, errors
    t = t_purchase_recommendations
    changed = 0
    with engine.begin() as conn:
        current = {r.recommendation_id: r for r in conn.execute(
            select(t.c.recommendation_id, t.c.ordered_qty, t.c.status).where(t.c.recommendation_id.in_(list(parsed))))}
        for rec_id, qty in parsed.items():
            row = current.get(rec_id)
            if row is None or row.status not in ("pending", "ordered"):
                continue
            old = int(row.ordered_qty) if row.ordered_qty is not None else None
            if old == qty or (qty == 0 and old is None):
                continue
            if qty is None or qty == 0:
                conn.execute(update(t).where(t.c.recommendation_id == rec_id).values(
                    ordered_qty=None, ordered_by=None, ordered_at=None, status="pending"))
            else:
                conn.execute(update(t).where(t.c.recommendation_id == rec_id).values(
                    ordered_qty=qty, ordered_by=user, ordered_at=utcnow(), status="ordered"))
            changed += 1
    return changed, []


def mark_received(engine: Engine, rec_id: int) -> None:
    t = t_purchase_recommendations
    with engine.begin() as conn:
        conn.execute(update(t).where(t.c.recommendation_id == rec_id, t.c.status == "ordered")
                     .values(status="received"))


# ---------------- главная ----------------

def last_runs(conn: Connection, jobs) -> dict[str, object]:
    out = {}
    for job in jobs:
        out[job] = conn.execute(select(t_job_runs).where(t_job_runs.c.job == job)
                                .order_by(desc(t_job_runs.c.run_id)).limit(1)).first()
    return out


def recent_assemblies(conn: Connection, limit: int = 10):
    return conn.execute(select(t_job_runs).where(t_job_runs.c.job == "fbo_assembly", t_job_runs.c.status == "ok")
                        .order_by(desc(t_job_runs.c.run_id)).limit(limit)).all()


def catalog_stats(conn: Connection) -> dict:
    def scalar(stmt):
        return conn.execute(stmt).scalar() or 0
    return {
        "models": scalar(select(func.count()).select_from(t_product_models)),
        "sizes": scalar(select(func.count()).select_from(t_product_sizes)),
        "sizes_with_lamoda": scalar(select(func.count()).select_from(t_product_sizes)
                                    .where(t_product_sizes.c.lamoda_sku.is_not(None))),
        "barcodes": scalar(select(func.count()).select_from(t_barcode_pool)),
        "suppliers": scalar(select(func.count()).select_from(t_suppliers)),
        "unmapped_items": scalar(select(func.count()).select_from(t_order_items)
                                 .where(t_order_items.c.product_size_id.is_(None))),
        "stock_1c_date": conn.execute(select(func.max(t_stock_1c.c.snapshot_date))).scalar(),
        "stock_fbo_date": conn.execute(select(func.max(t_stock_fbo.c.snapshot_date))).scalar(),
    }


def unmapped_order_skus(conn: Connection, limit: int = 100):
    return conn.execute(
        select(t_order_items.c.lamoda_sku, t_order_items.c.external_sku, func.count().label("n"))
        .where(t_order_items.c.product_size_id.is_(None))
        .group_by(t_order_items.c.lamoda_sku, t_order_items.c.external_sku)
        .order_by(desc("n")).limit(limit)).all()


def suppliers(conn: Connection):
    return conn.execute(select(t_suppliers).order_by(t_suppliers.c.name)).all()


def update_supplier(engine: Engine, supplier_id: int, *, production_days: str, delivery_days: str, moq: str) -> list[str]:
    try:
        values = {"production_days": parse_qty(production_days), "delivery_days": parse_qty(delivery_days),
                  "moq": parse_qty(moq)}
    except InputError as exc:
        return [str(exc)]
    values = {k: v for k, v in values.items() if v is not None}
    if values:
        with engine.begin() as conn:
            conn.execute(update(t_suppliers).where(t_suppliers.c.supplier_id == supplier_id).values(**values))
    return []

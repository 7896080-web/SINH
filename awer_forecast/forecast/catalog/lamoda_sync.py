"""Сверка справочника с каталогом Lamoda (`GET /v2/nomenclatures`).

Карта мэппинга — первичный источник (раздел 3 ТЗ); API дополняет её: находит размер
по баркоду из пула и проставляет недостающие `product_sizes.lamoda_sku` (размерный SKU),
`product_models.lamoda_sku` (`parentSku`) и `barcode_pool.seller_sku` (`externalSku`).
Без размерного SKU Lamoda не сопоставить ни остатки FBO, ни заказы — в их ответах
баркода нет. Расхождения (у размера в справочнике другой SKU, чем на Lamoda) не
перезаписываются, а попадают в отчёт.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from forecast.catalog.resolver import SkuResolver, normalize_code
from forecast.db.schema import t_barcode_pool, t_product_models, t_product_sizes

MAX_LISTED = 200


def sync_nomenclatures(conn: Connection, nomenclatures: Iterable[dict]) -> dict:
    resolver = SkuResolver.load(conn)
    sizes = {r.product_size_id: {"lamoda_sku": r.lamoda_sku, "model_id": r.product_model_id}
             for r in conn.execute(select(t_product_sizes.c.product_size_id, t_product_sizes.c.lamoda_sku,
                                          t_product_sizes.c.product_model_id))}
    models = {r.product_model_id: r.lamoda_sku for r in conn.execute(
        select(t_product_models.c.product_model_id, t_product_models.c.lamoda_sku))}
    report = {"total": 0, "matched": 0, "sizes_filled": 0, "models_filled": 0, "seller_sku_filled": 0,
              "unmatched": [], "mismatched": []}
    for item in nomenclatures:
        report["total"] += 1
        sku = normalize_code(item.get("sku"))
        parent = normalize_code(item.get("parentSku"))
        external = normalize_code(item.get("externalSku"))
        barcode = normalize_code(item.get("barcode"))
        psid = resolver.resolve(barcodes=[barcode] if barcode else [], lamoda_sku=sku, external_sku=external)
        if psid is None:
            if len(report["unmatched"]) < MAX_LISTED:
                report["unmatched"].append({"sku": sku, "externalSku": external, "barcode": barcode,
                                            "name": item.get("name"), "size": item.get("externalSize")})
            continue
        report["matched"] += 1
        size_row = sizes[psid]
        if sku:
            if not size_row["lamoda_sku"]:
                conn.execute(update(t_product_sizes).where(t_product_sizes.c.product_size_id == psid)
                             .values(lamoda_sku=sku))
                size_row["lamoda_sku"] = sku
                resolver.by_lamoda_sku[sku] = psid
                report["sizes_filled"] += 1
            elif normalize_code(size_row["lamoda_sku"]) != sku and len(report["mismatched"]) < MAX_LISTED:
                report["mismatched"].append({"product_size_id": psid, "in_catalog": size_row["lamoda_sku"],
                                             "on_lamoda": sku, "barcode": barcode})
        if parent and not models.get(size_row["model_id"]):
            conn.execute(update(t_product_models)
                         .where(t_product_models.c.product_model_id == size_row["model_id"])
                         .values(lamoda_sku=parent))
            models[size_row["model_id"]] = parent
            report["models_filled"] += 1
        if external and barcode:
            result = conn.execute(update(t_barcode_pool).where(
                t_barcode_pool.c.barcode == barcode, t_barcode_pool.c.product_size_id == psid,
                t_barcode_pool.c.seller_sku.is_(None)).values(seller_sku=external))
            report["seller_sku_filled"] += result.rowcount or 0
    report["unmatched_total"] = report["total"] - report["matched"]
    return report

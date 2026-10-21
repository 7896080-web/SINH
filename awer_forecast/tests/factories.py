"""Общие помощники тестов: справочник, Excel-карта; фейковый Lamoda — в forecast.integrations.lamoda.fake."""
from __future__ import annotations

import io

from openpyxl import Workbook

from forecast.db.schema import t_barcode_pool, t_product_models, t_product_sizes, t_suppliers
from forecast.integrations.lamoda.fake import FakeLamoda, item, order, price  # noqa: F401 — для тестов


def make_xlsx(rows: list[list], header: list[str], title_rows: list[list] | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in title_rows or []:
        ws.append(row)
    ws.append(header)
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def add_size(conn, article: str, color: str, size: str, barcodes: list[str], *, lamoda_sku: str | None = None,
             seller_sku: str | None = None, supplier: dict | None = None, available: bool = True,
             status: str = "active", collection: str = "base") -> int:
    model = conn.execute(t_product_models.select().where(
        t_product_models.c.article == article, t_product_models.c.color == color)).first()
    if model is None:
        supplier_id = None
        if supplier:
            supplier_id = conn.execute(t_suppliers.insert().values(**supplier)).inserted_primary_key[0]
        model_id = conn.execute(t_product_models.insert().values(
            article=article, color=color, category="Куртки", supplier_id=supplier_id,
            available_to_reorder=available, status=status, collection_type=collection)).inserted_primary_key[0]
    else:
        model_id = model.product_model_id
    psid = conn.execute(t_product_sizes.insert().values(
        product_model_id=model_id, size_label=size, lamoda_sku=lamoda_sku)).inserted_primary_key[0]
    for barcode in barcodes:
        conn.execute(t_barcode_pool.insert().values(product_size_id=psid, barcode=barcode, seller_sku=seller_sku))
    return psid

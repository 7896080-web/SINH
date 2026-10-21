"""Сопоставление записей площадок и 1С с цветоразмерным SKU (`product_sizes`).

Главный ключ — баркод, причём через ВЕСЬ пул `barcode_pool`: у одного размера может
быть несколько баркодов одновременно (разные в 1С и на площадках), и совпадения
любого одного достаточно (раздел 3 ТЗ). Join идёт по пулу, не по одному
«каноническому» полю.

У Lamoda в ответах заказов и остатков FBO баркода нет — там `sku` (размерный SKU
Lamoda) и `externalSku` (SKU продавца). Поэтому вторые ключи — `product_sizes.lamoda_sku`
и `barcode_pool.seller_sku`; `externalSku` дополнительно пробуется как баркод: часто
продавец заводит на Lamoda баркод в качестве своего SKU.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.engine import Connection

from forecast.db.schema import t_barcode_pool, t_product_sizes


def normalize_code(value) -> str:
    """Баркод/SKU к строке без пробелов. Число из Excel (2000041459377.0) — к целому.

    Ведущие нули Excel теряет ещё при сохранении числа, восстановить их нельзя —
    поэтому в карте мэппинга колонку баркодов стоит держать текстовой (см. README)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if value != value or not value.is_integer():   # NaN или дробь — не баркод
            return ""
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return "".join(str(value).split())


@dataclass
class SkuResolver:
    by_barcode: dict[str, int] = field(default_factory=dict)
    by_seller_sku: dict[str, int] = field(default_factory=dict)
    by_lamoda_sku: dict[str, int] = field(default_factory=dict)
    ambiguous_seller_sku: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, conn: Connection) -> "SkuResolver":
        resolver = cls()
        for barcode, seller_sku, psid in conn.execute(
            select(t_barcode_pool.c.barcode, t_barcode_pool.c.seller_sku, t_barcode_pool.c.product_size_id)
        ):
            resolver.by_barcode[normalize_code(barcode)] = psid
            sku = normalize_code(seller_sku)
            if sku:
                previous = resolver.by_seller_sku.setdefault(sku, psid)
                if previous != psid:
                    resolver.ambiguous_seller_sku.add(sku)
        for sku in resolver.ambiguous_seller_sku:
            resolver.by_seller_sku.pop(sku, None)
        for lamoda_sku, psid in conn.execute(
            select(t_product_sizes.c.lamoda_sku, t_product_sizes.c.product_size_id)
            .where(t_product_sizes.c.lamoda_sku.is_not(None))
        ):
            code = normalize_code(lamoda_sku)
            if code:
                resolver.by_lamoda_sku[code] = psid
        return resolver

    def resolve_barcodes(self, barcodes: Iterable) -> set[int]:
        """Все размеры, на которые указывает хоть один баркод из списка."""
        found = set()
        for barcode in barcodes:
            psid = self.by_barcode.get(normalize_code(barcode))
            if psid is not None:
                found.add(psid)
        return found

    def resolve(self, *, barcodes: Iterable = (), lamoda_sku=None, external_sku=None) -> int | None:
        """Размер по любому из известных идентификаторов записи.

        Порядок: баркоды (любой из пула) → SKU Lamoda → SKU продавца → SKU продавца как
        баркод. Если баркоды записи указывают на РАЗНЫЕ размеры — это конфликт данных,
        возвращаем None: молча выбрать один значило бы записать продажу не тому товару."""
        by_barcodes = self.resolve_barcodes(barcodes)
        if len(by_barcodes) == 1:
            return next(iter(by_barcodes))
        if len(by_barcodes) > 1:
            return None
        code = normalize_code(lamoda_sku)
        if code and code in self.by_lamoda_sku:
            return self.by_lamoda_sku[code]
        code = normalize_code(external_sku)
        if code:
            if code in self.by_seller_sku:
                return self.by_seller_sku[code]
            if code in self.by_barcode:
                return self.by_barcode[code]
        return None

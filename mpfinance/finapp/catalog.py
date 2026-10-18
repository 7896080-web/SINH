"""Справочник товаров 1С: баркод → товар (размер-цвет).

Источник — команда обработки `BARCODE_DICT` (регистр «Штрихкоды», все баркоды
независимо от остатка), файл `barcodes_<метка>.txt`, строка на баркод:

    uid_1c|артикул|наименование|баркод|размер|цвет

Тот же справочник, по которому 1С потом ищет номенклатуру в документе продаж
(`НайтиПоШтрихкоду`): строка отчёта, сопоставленная здесь, найдётся и там.

Пока обмен с 1С выключен (его включают последним этапом), справочник берётся
из базы sync_admin — там те же баркоды из 1С (`syncdb.catalog_rows`, раз в сутки
и кнопкой на «Диагностике»). Файл можно загрузить и вручную.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from finapp import progress, settings
from finapp.models import Barcode, Product
from finapp.timeutils import now_utc

FIELDS = 6
_CHUNK = 500


class CatalogError(ValueError):
    pass


def _split_from_the_right(line: str) -> list[str] | None:
    """«|» внутри наименования: поля после него режутся справа (как в sync_admin)."""
    parts = line.split("|")
    if len(parts) < FIELDS:
        return None
    if len(parts) == FIELDS:
        return parts
    tail = FIELDS - 3
    return [parts[0], parts[1], "|".join(parts[2:-tail])] + parts[-tail:]


def parse_barcode_dict(text: str) -> list[dict]:
    rows = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        parts = _split_from_the_right(line)
        if parts is None:
            continue
        uid, barcode = parts[0].strip(), parts[3].strip()
        if not uid or not barcode:
            continue
        rows.append({"uid_1c": uid[:64], "article": parts[1].strip()[:200],
                     "name": parts[2].strip()[:500], "barcode": barcode[:64],
                     "size": parts[4].strip()[:100], "color": parts[5].strip()[:100]})
    return rows


def decode_upload(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise CatalogError("файл не читается ни как UTF-8, ни как cp1251")


def load_catalog(db: Session, rows: list[dict], stamp: str = settings.CATALOG_LOADED_AT) -> dict:
    """Заменить снимок баркодов и обновить товары. Не коммитит.

    Пустой разбор — отказ: обрезанный файл иначе стёр бы справочник, и все
    строки отчётов стали бы «без товара 1С». Товары не удаляются никогда — на
    них ссылаются себестоимость и прошлые отчёты."""
    if not rows:
        raise CatalogError("в файле нет ни одной строки справочника "
                           "(ждём uid|артикул|наименование|баркод|размер|цвет)")
    products: dict[str, dict] = {}
    pairs: set[tuple[str, str]] = set()
    for row in rows:
        products.setdefault(row["uid_1c"], row)
        pairs.add((row["barcode"], row["uid_1c"]))

    now = now_utc()
    uids = sorted(products)
    existing: dict[str, Product] = {}
    for start in range(0, len(uids), _CHUNK):
        for product in db.query(Product).filter(Product.uid_1c.in_(uids[start:start + _CHUNK])):
            existing[product.uid_1c] = product
    added = 0
    for i, uid in enumerate(uids, start=1):
        progress.every(1000, i, len(uids), "справочник: товары")
        row = products[uid]
        product = existing.get(uid)
        if product is None:
            product = Product(uid_1c=uid)
            db.add(product)
            added += 1
        product.article, product.name = row["article"], row["name"]
        product.size, product.color = row["size"], row["color"]
        product.seen_at = now
    db.flush()

    progress.report(None, None, f"справочник: баркоды ({len(pairs)})")
    db.query(Barcode).delete(synchronize_session=False)
    db.bulk_insert_mappings(Barcode, [{"barcode": b, "uid_1c": u} for b, u in sorted(pairs)])
    settings.put(db, stamp, now.isoformat(timespec="seconds"))
    settings.put(db, settings.CATALOG_ROWS, str(len(pairs)))
    return {"barcodes": len(pairs), "products": len(uids), "new_products": added}


def owners(db: Session, barcodes) -> dict[str, set[str]]:
    """Баркод → множество товаров 1С (больше одного — ошибка справочника)."""
    out: dict[str, set[str]] = {}
    barcodes = sorted(set(barcodes))
    for start in range(0, len(barcodes), _CHUNK):
        for bc, uid in db.query(Barcode.barcode, Barcode.uid_1c).filter(
                Barcode.barcode.in_(barcodes[start:start + _CHUNK])):
            out.setdefault(bc, set()).add(uid)
    return out


def load_from_sync_admin(db: Session) -> dict:
    """Справочник из базы sync_admin (только чтение). Не коммитит."""
    from finapp import syncdb
    try:
        rows = syncdb.catalog_rows()
    except syncdb.SyncDbError as e:
        raise CatalogError(str(e)) from e
    return load_catalog(db, rows, stamp=settings.SYNC_CATALOG_AT)

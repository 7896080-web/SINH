"""Сопоставление артикулов Lamoda с товарами 1С — по правилам sync_admin.

Правила (ТЗ, 6.2; sync_admin — `app/workers/matching.py`,
`reconciliation.import_barcode_dict`):

1. **Ключ — штрихкод, не название и не артикул.** Названия и артикулы в 1С и
   Lamoda пишутся по-разному («3030 KAHVE 4XL АВЕР Рубашка» против «3030-3032
   АВЕР Рубашка Д/р одн Коричневый», `2XL` = `XXL`), и сравнивать их значит
   угадывать. Артикул 1С и размер со страницы — для глаза человека, решение
   принимается по штрихкоду.
2. **У цветоразмерного SKU 1С — пул штрихкодов, принадлежащих только ему.**
   Артикул Lamoda сопоставлен, если его штрихкод (EAN из «Одежды полной»)
   совпал с ЛЮБЫМ штрихкодом пула, не обязательно первым.
3. **Штрихкод в пулах двух SKU — нарушение** («неоднозначно»): по нему нельзя
   сказать, какой товар уедет. В 1С проверка поставки на таком штрихкоде
   отказывает (`ТоварПоставкиПоШтрихкоду`).
4. **Строго один к одному.** Два артикула Lamoda на один SKU 1С — нарушение
   («один SKU у двух артикулов»): коды пошли бы по двум GTIN на одну вещь. В
   sync_admin иначе — там несколько SKU 1С законно ведут на одну карточку
   площадки; у Lamoda таких случаев нет.
5. **Догадки нет.** Автопривязку sync_admin «по соседям карточки» не переносим:
   у Lamoda на артикул один штрихкод, соседей нет, а перемещение по угаданному
   товару не отменить. Не нашлось — так и показываем.

Справочник 1С — снимок `barcodes_*.txt` (формат sync_admin): по команде
`BARCODE_DICT` из обработки (`mark-2`) или файлом вручную. Снимок заменяется
целиком: справочник — отражение 1С, а не накопление.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from markapp import settings
from markapp.models import CatalogItem, GtinPair, NkCard, OnecBarcode
from markapp.timeutils import now_utc

DICT_LOADED_AT = "onec_dict_loaded_at"
DICT_SOURCE = "onec_dict_source"
DICT_ROWS = "onec_dict_rows"

BARCODE_DICT_FIELDS = 6
CHUNK = 500

STATUS_LABELS = {
    "ok": "сопоставлен",
    "not_in_1c": "нет в 1С",
    "ambiguous": "штрихкод у нескольких SKU 1С",
    "shared_sku": "один SKU у двух артикулов",
    "no_ean": "нет штрихкода в каталоге",
}
# Что значит и что делать — рядом со статусом, а не в голове у оператора.
STATUS_HINTS = {
    "ok": "Штрихкод Lamoda есть в пуле ровно одного SKU 1С, и на этот SKU не ведёт другой артикул.",
    "not_in_1c": "Штрихкода из каталога Lamoda нет в 1С. Проверка поставки откажет по этой строке. "
                 "Исправить штрихкод в карточке Lamoda или добавить его товару в 1С.",
    "ambiguous": "Один штрихкод записан на нескольких SKU 1С — какой уедет, не определить. "
                 "Исправить в 1С: у штрихкода должен быть один владелец.",
    "shared_sku": "Штрихкоды двух артикулов Lamoda — в пуле одного SKU 1С. Перемещение такой "
                  "поставки закрыто. Исправить штрихкод в карточке Lamoda или пул в 1С.",
    "no_ean": "В «Одежде полной» у артикула нет штрихкода — сопоставлять не по чему.",
}
PROBLEM_STATUSES = ("not_in_1c", "ambiguous", "shared_sku", "no_ean")


class MappingError(ValueError):
    pass


# --- Справочник 1С ---------------------------------------------------------------

def _split_from_the_right(line: str, fields: int) -> list[str] | None:
    """Как в sync_admin: «|» внутри наименования режется справа — поля после
    наименования фиксированы, само наименование может нести что угодно."""
    parts = line.split("|")
    if len(parts) < fields:
        return None
    if len(parts) == fields:
        return parts
    tail = fields - 3
    return [parts[0], parts[1], "|".join(parts[2:-tail])] + parts[-tail:]


def parse_barcode_dict(text: str) -> list[dict]:
    """`uid|артикул|наименование|баркод|размер|цвет` — строка на баркод."""
    rows = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        p = _split_from_the_right(line, BARCODE_DICT_FIELDS)
        if p is None:
            p = line.split("|")
            if len(p) < 4:
                continue
        item, barcode = p[0].strip(), p[3].strip()
        if not item or not barcode:
            continue
        rows.append({"item_id": item, "article": p[1].strip(), "name": p[2].strip(),
                     "barcode": barcode, "size": p[4].strip() if len(p) > 4 else "",
                     "color": p[5].strip() if len(p) > 5 else ""})
    return rows


def decode_upload(data: bytes) -> str:
    """Файл из 1С — UTF-8 (с BOM или без); на случай пересохранения — cp1251."""
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise MappingError("файл не читается ни как UTF-8, ни как cp1251")


def load_dictionary(db: Session, rows: list[dict], source: str) -> dict:
    """Заменить снимок справочника. Не коммитит.

    Пустой разбор — отказ, а не «справочник пуст»: обрезанный или чужой файл
    иначе стёр бы снимок, и вся страница стала бы «нет в 1С».
    """
    if not rows:
        raise MappingError("в файле нет ни одной строки справочника "
                           "(ждём uid|артикул|наименование|баркод|размер|цвет)")
    seen: set[tuple[str, str]] = set()
    unique = []
    for r in rows:
        key = (r["barcode"], r["item_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append({k: (r[k] or "")[:lim] for k, lim in
                       (("barcode", 64), ("item_id", 64), ("article", 200), ("name", 500),
                        ("size", 100), ("color", 100))})
    db.query(OnecBarcode).delete(synchronize_session=False)
    db.bulk_insert_mappings(OnecBarcode, unique)
    owners = defaultdict(set)
    for b, item in seen:
        owners[b].add(item)
    stats = {"rows": len(unique), "barcodes": len(owners),
             "items": len({i for _, i in seen}),
             "ambiguous": sum(1 for s in owners.values() if len(s) > 1)}
    settings.put(db, DICT_LOADED_AT, now_utc().isoformat(timespec="seconds"))
    settings.put(db, DICT_SOURCE, source[:200])
    settings.put(db, DICT_ROWS, str(stats["rows"]))
    return stats


def dictionary_info(db: Session) -> dict:
    return {"loaded_at": settings.get(db, DICT_LOADED_AT), "source": settings.get(db, DICT_SOURCE),
            "rows": settings.get(db, DICT_ROWS)}


# --- Сопоставление ------------------------------------------------------------------

@dataclass
class MapRow:
    supplier_sku: str
    ean: str
    lamoda_size: str
    lamoda_color: str
    status: str
    item_id: str = ""
    onec_article: str = ""
    onec_name: str = ""
    onec_size: str = ""
    onec_color: str = ""
    pool: list[str] = field(default_factory=list)
    others: list[str] = field(default_factory=list)   # другие SKU (ambiguous) или артикулы (shared_sku)
    gtin: str = ""
    nk_color: str = ""
    nk_size: str = ""

    @property
    def label(self) -> str:
        return STATUS_LABELS[self.status]


def _by_barcodes(db: Session, barcodes: list[str]) -> list[OnecBarcode]:
    out = []
    for i in range(0, len(barcodes), CHUNK):
        out += db.query(OnecBarcode).filter(OnecBarcode.barcode.in_(barcodes[i:i + CHUNK])).all()
    return out


def _by_items(db: Session, items: list[str]) -> list[OnecBarcode]:
    out = []
    for i in range(0, len(items), CHUNK):
        out += db.query(OnecBarcode).filter(OnecBarcode.item_id.in_(items[i:i + CHUNK])).all()
    return out


def build(db: Session) -> list[MapRow]:
    """Каждый артикул «Одежды полной» — с его товаром 1С и статусом по правилам."""
    catalog = db.query(CatalogItem).order_by(CatalogItem.supplier_sku).all()
    eans = sorted({c.ean for c in catalog if c.ean})
    owners: dict[str, list[OnecBarcode]] = defaultdict(list)
    for b in _by_barcodes(db, eans):
        owners[b.barcode].append(b)
    items = sorted({b.item_id for lst in owners.values() for b in lst})
    pools: dict[str, list[str]] = defaultdict(list)
    for b in _by_items(db, items):
        pools[b.item_id].append(b.barcode)
    gtins = {p.supplier_sku: p.gtin for p in db.query(GtinPair).all()}
    cards = {c.gtin: c for c in db.query(NkCard).filter(NkCard.status == "ok").all()}

    rows: list[MapRow] = []
    for c in catalog:
        r = MapRow(supplier_sku=c.supplier_sku, ean=c.ean, lamoda_size=c.size,
                   lamoda_color=c.color, status="ok", gtin=gtins.get(c.supplier_sku, ""))
        card = cards.get(r.gtin)
        if card is not None:
            r.nk_color, r.nk_size = card.color, card.size
        if not c.ean:
            r.status = "no_ean"
        else:
            found = {b.item_id: b for b in owners.get(c.ean, [])}
            if not found:
                r.status = "not_in_1c"
            elif len(found) > 1:
                r.status = "ambiguous"
                r.others = [f"{b.article} {b.size} {b.color}".strip() for b in found.values()]
            else:
                b = next(iter(found.values()))
                r.item_id, r.onec_article, r.onec_name = b.item_id, b.article, b.name
                r.onec_size, r.onec_color = b.size, b.color
                r.pool = sorted(set(pools.get(b.item_id, [])))
        rows.append(r)

    # Правило 4: один SKU 1С — один артикул Lamoda.
    by_item: dict[str, list[MapRow]] = defaultdict(list)
    for r in rows:
        if r.status == "ok":
            by_item[r.item_id].append(r)
    for group in by_item.values():
        if len(group) > 1:
            for r in group:
                r.status = "shared_sku"
                r.others = [o.supplier_sku for o in group if o is not r]
    return rows


def counts(rows: list[MapRow]) -> dict[str, int]:
    out = {s: 0 for s in STATUS_LABELS}
    for r in rows:
        out[r.status] += 1
    return out


def select(rows: list[MapRow], status: str = "", q: str = "") -> list[MapRow]:
    q = (q or "").strip().lower()
    out = []
    for r in rows:
        if status == "problems" and r.status not in PROBLEM_STATUSES:
            continue
        if status and status != "problems" and r.status != status:
            continue
        if q and not any(q in (v or "").lower() for v in
                         (r.supplier_sku, r.ean, r.onec_article, r.onec_name, r.gtin, " ".join(r.pool))):
            continue
        out.append(r)
    return out

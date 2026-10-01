"""Сопоставление каталога площадки с товарами 1С — по правилам sync_admin.

1. **Ключ — штрихкод.** Баркод площадки сопоставлен, если он есть в справочнике
   1С (`OnecBarcode`) у ровно ОДНОГО SKU (размер-цвет). У двух SKU — нарушение
   «неоднозначно»: какой товар имеется в виду, сказать нельзя, и цену по нему
   не считаем.
2. **Пул размер-цвета площадки.** Строки каталога с одним `external_id` — это
   один размер на площадке с несколькими баркодами. Если сопоставленные баркоды
   пула ведут к ОДНОМУ SKU 1С, остальные баркоды пула относятся к нему же
   (так работает автопривязка sync_admin). Ведут к разным — пул не мержим.
3. **Подтверждённые человеком связи** (`ManualLink`, страница «Сопоставление»
   по правилам артикулов) — для баркодов, которых в справочнике 1С нет вовсе.
   Существующую связь по баркоду справочника они не перебивают никогда.

Справочник 1С — снимок `barcodes_*.txt` (формат sync_admin): по команде
`BARCODE_DICT` или файлом вручную. Снимок заменяется целиком.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from priceapp import settings
from priceapp.models import ManualLink, OnecBarcode, PlatformItem
from priceapp.timeutils import now_utc

BARCODE_DICT_FIELDS = 6

STATUS_LABELS = {
    "ok": "сопоставлен по баркоду",
    "pool": "по пулу размера",
    "manual": "подтверждён вручную",
    "not_in_1c": "нет в 1С",
    "ambiguous": "баркод у нескольких SKU 1С",
}
MAPPED = ("ok", "pool", "manual")


class MappingError(ValueError):
    pass


# --- Справочник 1С ---------------------------------------------------------------

def _split_from_the_right(line: str, fields: int) -> list[str] | None:
    """Как в sync_admin: «|» внутри наименования режется справа."""
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
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise MappingError("файл не читается ни как UTF-8, ни как cp1251")


def load_dictionary(db: Session, rows: list[dict], source: str) -> dict:
    """Заменить снимок справочника. Не коммитит. Пустой разбор — отказ: обрезанный
    файл иначе стёр бы снимок, и весь каталог стал бы «нет в 1С»."""
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
    settings.put(db, settings.DICT_LOADED_AT, now_utc().isoformat(timespec="seconds"))
    settings.put(db, settings.DICT_ROWS, str(len(unique)))
    return {"rows": len(unique), "items": len({i for _, i in seen}), "source": source}


# --- Сопоставление ------------------------------------------------------------------

@dataclass
class MapRow:
    item: PlatformItem
    status: str
    item_id: str = ""
    others: list[str] = field(default_factory=list)   # SKU 1С при «неоднозначно»

    @property
    def label(self) -> str:
        return STATUS_LABELS[self.status]


def owners_of(db: Session, barcodes) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    barcodes = list(set(barcodes))
    for i in range(0, len(barcodes), 500):
        for b in db.query(OnecBarcode.barcode, OnecBarcode.item_id).filter(
                OnecBarcode.barcode.in_(barcodes[i:i + 500])):
            out[b.barcode].add(b.item_id)
    return out


def build(db: Session, account_id: int) -> list[MapRow]:
    """Статус каждой строки каталога кабинета."""
    items = (db.query(PlatformItem).filter(PlatformItem.account_id == account_id)
             .order_by(PlatformItem.article, PlatformItem.size, PlatformItem.barcode).all())
    owners = owners_of(db, [i.barcode for i in items])
    manual = {m.barcode: m.item_id for m in
              db.query(ManualLink).filter(ManualLink.account_id == account_id)}

    rows: list[MapRow] = []
    for it in items:
        own = owners.get(it.barcode, set())
        if len(own) == 1:
            rows.append(MapRow(it, "ok", next(iter(own))))
        elif len(own) > 1:
            rows.append(MapRow(it, "ambiguous", others=sorted(own)))
        elif it.barcode in manual:
            rows.append(MapRow(it, "manual", manual[it.barcode]))
        else:
            rows.append(MapRow(it, "not_in_1c"))

    # Пул размера на площадке: сопоставленные соседи ведут к одному SKU — остальные к нему же.
    pools: dict[str, list[MapRow]] = defaultdict(list)
    for r in rows:
        if r.item.external_id:
            pools[r.item.external_id].append(r)
    for pool in pools.values():
        targets = {r.item_id for r in pool if r.status in ("ok", "manual")}
        if len(targets) != 1 or any(r.status == "ambiguous" for r in pool):
            continue
        target = next(iter(targets))
        for r in pool:
            if r.status == "not_in_1c":
                r.status, r.item_id = "pool", target
    return rows


def account_items(db: Session, account_id: int) -> dict[str, list[PlatformItem]]:
    """SKU 1С -> строки каталога кабинета, которые на него ведут (для расчёта цены)."""
    out: dict[str, list[PlatformItem]] = defaultdict(list)
    for r in build(db, account_id):
        if r.status in MAPPED:
            out[r.item_id].append(r.item)
    return dict(out)


def counts(rows: list[MapRow]) -> dict[str, int]:
    c = {k: 0 for k in STATUS_LABELS}
    for r in rows:
        c[r.status] += 1
    return c

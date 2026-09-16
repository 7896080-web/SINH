"""One-off: import the 1C barcode dictionary (barcodes_*.txt), but ONLY barcodes
that marketplaces actually use (present in PlatformCatalogItem). Creates missing
Product (stock 0, broadcast OFF) + Barcode with size/color, and clears the
matching MappingConflict. Bounded to the marketplace intersection, not all 154k.
Dedupes by uid (one товар can have many barcodes). Idempotent.
Run from project root: .venv\\Scripts\\python.exe import_barcode_dict.py
"""
from pathlib import Path

from app.database import SessionLocal
from app.models import Product, Barcode, PlatformCatalogItem, MappingConflict

DIRS = [r"C:\sync\results", r"C:\sync\archive"]


def newest_dict_file():
    files = []
    for d in DIRS:
        files += list(Path(d).glob("barcodes_*.txt"))
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def main():
    s = SessionLocal()
    f = newest_dict_file()
    if f is None:
        print("нет файла barcodes_*.txt")
        s.close()
        return
    print("файл:", f.name)

    catalog_bcs = {r[0] for r in s.query(PlatformCatalogItem.barcode).distinct().all() if r[0]}
    existing_uids = {r[0] for r in s.query(Product.uid_1c).all()}
    existing_bcs = {b.barcode for b in s.query(Barcode).all()}
    print("баркодов в каталогах площадок:", len(catalog_bcs),
          "| товаров в базе:", len(existing_uids), "| баркодов в базе:", len(existing_bcs))

    prod_created = bc_created = 0
    resolved = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        p = line.rstrip("\n").split("|")
        if len(p) < 4:
            continue
        uid = p[0].strip()
        barcode = p[3].strip()
        if not uid or not barcode or barcode not in catalog_bcs:
            continue
        size = p[4].strip() if len(p) > 4 else ""
        color = p[5].strip() if len(p) > 5 else ""

        if uid not in existing_uids:
            s.add(Product(uid_1c=uid, article=p[1].strip(), name=p[2].strip(),
                          stock_on_hand=0, size=(size or None), color=(color or None)))
            existing_uids.add(uid)
            prod_created += 1

        if barcode not in existing_bcs:
            s.add(Barcode(barcode=barcode, uid_1c=uid, source_platform="1c_dict"))
            existing_bcs.add(barcode)
            bc_created += 1

        resolved.add(barcode)

    s.flush()  # закрепим товары/баркоды до чистки конфликтов

    conflicts_cleared = 0
    resolved = list(resolved)
    for i in range(0, len(resolved), 500):
        conflicts_cleared += s.query(MappingConflict).filter(
            MappingConflict.barcode.in_(resolved[i:i + 500])
        ).delete(synchronize_session=False)

    s.commit()
    print("products created:", prod_created)
    print("barcodes created:", bc_created)
    print("conflicts cleared:", conflicts_cleared)
    print("итого — products:", s.query(Product).count(),
          "| barcodes:", s.query(Barcode).count(),
          "| conflicts left:", s.query(MappingConflict).count())
    s.close()


if __name__ == "__main__":
    main()

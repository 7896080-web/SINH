"""One-off: seed Product + Barcode rows from the 1C stock export files.

Reads C:\\sync\\results\\stock_*.txt (uid|article|name|qty|barcode,barcode),
creates missing Product (stock_on_hand = qty from 1C, broadcast OFF by default)
and missing Barcode (source_platform='1c_export'). Idempotent: existing rows
are left untouched. Does NOT archive the files (worker keeps its normal cycle).
Run from the project root:  .venv\\Scripts\\python.exe seed_products.py
"""
from pathlib import Path

from app.database import SessionLocal
from app.models import Product, Barcode

RESULTS_DIR = r"C:\sync\results"


def main():
    s = SessionLocal()
    rows = {}
    for f in sorted(Path(RESULTS_DIR).glob("stock_*.txt")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            p = line.split("|")
            if len(p) < 5:
                continue
            digits = p[3].strip().lstrip("-")
            if not digits.isdigit():
                continue
            uid = p[0].strip()
            codes = [b.strip() for b in p[4].split(",") if b.strip()]
            rows[uid] = (p[1].strip(), p[2].strip(), int(p[3]), codes)

    seen = {b.barcode for b in s.query(Barcode).all()}
    products_created = barcodes_created = 0
    for uid, (article, name, qty, codes) in rows.items():
        if not uid:
            continue
        if s.query(Product).filter(Product.uid_1c == uid).first() is None:
            s.add(Product(uid_1c=uid, article=article, name=name, stock_on_hand=qty))
            products_created += 1
        for code in codes:
            if code and code not in seen:
                s.add(Barcode(barcode=code, uid_1c=uid, source_platform="1c_export"))
                seen.add(code)
                barcodes_created += 1
    s.commit()

    print("lines parsed from 1C:", len(rows))
    print("products created:", products_created)
    print("barcodes created:", barcodes_created)
    print("total products now:", s.query(Product).count())
    print("total barcodes now:", s.query(Barcode).count())
    s.close()


if __name__ == "__main__":
    main()

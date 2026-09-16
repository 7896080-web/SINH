# -*- coding: utf-8 -*-
"""Этап 2: размер/цвет товара в приложении.
Правит модель, роутеры и шаблоны (строковые вставки), пишет миграцию и
обновлённый seed_products.py (с size/color). Идемпотентно, устойчиво к CRLF."""
import io
import os

ROOT = r"C:\sync_admin"

EDITS = [
    # models.py
    (r"app\models.py",
     '    article = Column(String(64), index=True)\n    name = Column(String(255), index=True)',
     '    article = Column(String(64), index=True)\n    name = Column(String(255), index=True)\n    size = Column(String(32))\n    color = Column(String(128))'),
    # stock_control.py row dict
    (r"app\routers\stock_control.py",
     '        "uid_1c": p.uid_1c, "article": p.article, "name": p.name,\n        "stock_on_hand": p.stock_on_hand or 0, "reserve": p.reserve or 0,',
     '        "uid_1c": p.uid_1c, "article": p.article, "name": p.name,\n        "size": p.size, "color": p.color,\n        "stock_on_hand": p.stock_on_hand or 0, "reserve": p.reserve or 0,'),
    # sync_products.py row dict
    (r"app\routers\sync_products.py",
     '        "uid_1c": product.uid_1c, "article": product.article, "name": product.name,\n        "stock_on_hand": product.stock_on_hand, "reserve": product.reserve,',
     '        "uid_1c": product.uid_1c, "article": product.article, "name": product.name,\n        "size": product.size, "color": product.color,\n        "stock_on_hand": product.stock_on_hand, "reserve": product.reserve,'),
    # anomalies.py select
    (r"app\routers\anomalies.py",
     '            Product.article, Product.name,\n            PlatformAccount.name.label("account_name"), PlatformAccount.platform.label("account_platform"),',
     '            Product.article, Product.name, Product.size, Product.color,\n            PlatformAccount.name.label("account_name"), PlatformAccount.platform.label("account_platform"),'),
    # anomalies.py group_by
    (r"app\routers\anomalies.py",
     '        SyncAnomaly.uid_1c, SyncAnomaly.account_id, SyncAnomaly.reason,\n        Product.article, Product.name, PlatformAccount.name, PlatformAccount.platform,\n    )',
     '        SyncAnomaly.uid_1c, SyncAnomaly.account_id, SyncAnomaly.reason,\n        Product.article, Product.name, Product.size, Product.color,\n        PlatformAccount.name, PlatformAccount.platform,\n    )'),
    # stock_control.html header
    (r"app\templates\stock_control.html",
     '      <th></th>\n      <th>Артикул</th>\n      <th>Наименование</th>\n      <th>Остаток ЦС</th>',
     '      <th></th>\n      <th>Артикул</th>\n      <th>Размер</th>\n      <th>Цвет</th>\n      <th>Наименование</th>\n      <th>Остаток ЦС</th>'),
    # stock_control.html cell
    (r"app\templates\stock_control.html",
     '      <td class="mono">{{ r.article }}</td>\n      <td>{{ r.name }}{% if not r.has_barcode %} <span class="scv-badge scv-badge--off">нет баркода</span>{% endif %}</td>',
     '      <td class="mono">{{ r.article }}</td>\n      <td>{{ r.size or \'\' }}</td>\n      <td>{{ r.color or \'\' }}</td>\n      <td>{{ r.name }}{% if not r.has_barcode %} <span class="scv-badge scv-badge--off">нет баркода</span>{% endif %}</td>'),
    # sync_products_rows.html header
    (r"app\templates\sync_products_rows.html",
     '            <th>Артикул</th>\n            <th>Наименование</th>\n            <th style="text-align:right;">Остаток</th>',
     '            <th>Артикул</th>\n            <th>Размер</th>\n            <th>Цвет</th>\n            <th>Наименование</th>\n            <th style="text-align:right;">Остаток</th>'),
    # sync_products_row.html cell
    (r"app\templates\sync_products_row.html",
     "    <td class=\"mono\">{{ row.article or '—' }}</td>\n    <td>\n        {{ row.name or '—' }}",
     "    <td class=\"mono\">{{ row.article or '—' }}</td>\n    <td>{{ row.size or '' }}</td>\n    <td>{{ row.color or '' }}</td>\n    <td>\n        {{ row.name or '—' }}"),
    # mapping_rows.html header (mapped)
    (r"app\templates\mapping_rows.html",
     '                <th>Баркод</th>\n                <th>Артикул</th>\n                <th>Наименование</th>\n                <th>Источник</th>',
     '                <th>Баркод</th>\n                <th>Артикул</th>\n                <th>Размер</th>\n                <th>Цвет</th>\n                <th>Наименование</th>\n                <th>Источник</th>'),
    # mapping_rows.html cell (mapped)
    (r"app\templates\mapping_rows.html",
     "                <td class=\"mono\">{{ row.product.article or '—' }}</td>\n                <td>{{ row.product.name or '—' }}</td>\n                <td>{{ row.source_platform or 'выгрузка из 1С' }}</td>",
     "                <td class=\"mono\">{{ row.product.article or '—' }}</td>\n                <td>{{ row.product.size or '' }}</td>\n                <td>{{ row.product.color or '' }}</td>\n                <td>{{ row.product.name or '—' }}</td>\n                <td>{{ row.source_platform or 'выгрузка из 1С' }}</td>"),
    # anomalies_rows.html cell
    (r"app\templates\anomalies_rows.html",
     "                <div>{{ row.name or '—' }}</div>\n                <div class=\"mono help-text\">{{ row.article or '—' }}</div>",
     "                <div>{{ row.name or '—' }}</div>\n                <div class=\"mono help-text\">{{ row.article or '—' }}</div>\n                <div class=\"help-text\">{{ row.size or '' }}{% if row.color %} · {{ row.color }}{% endif %}</div>"),
]


def apply_edit(path, old, new):
    with io.open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if new in text:
        return "skip"
    if old in text:
        text = text.replace(old, new, 1)
    else:
        old2, new2 = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        if old2 in text:
            text = text.replace(old2, new2, 1)
        else:
            return "MISS"
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return "ok"


for rel, old, new in EDITS:
    path = os.path.join(ROOT, rel)
    print(apply_edit(path, old, new), rel)

# --- миграция ---
MIG = os.path.join(ROOT, r"alembic\versions\a7c3e5f1b208_product_size_color.py")
io.open(MIG, "w", encoding="utf-8", newline="").write(
    'from alembic import op\n'
    'import sqlalchemy as sa\n\n'
    'revision = "a7c3e5f1b208"\n'
    'down_revision = "f2a9c1d7b0e4"\n'
    'branch_labels = None\n'
    'depends_on = None\n\n\n'
    'def upgrade():\n'
    '    op.add_column("products", sa.Column("size", sa.String(length=32), nullable=True))\n'
    '    op.add_column("products", sa.Column("color", sa.String(length=128), nullable=True))\n\n\n'
    'def downgrade():\n'
    '    op.drop_column("products", "color")\n'
    '    op.drop_column("products", "size")\n')
print("migration written")

# --- обновлённый seed (с size/color, обновляет существующие) ---
SEED = os.path.join(ROOT, "seed_products.py")
io.open(SEED, "w", encoding="utf-8", newline="").write('''from pathlib import Path
from app.database import SessionLocal
from app.models import Product, Barcode

DIRS = [r"C:\\sync\\results", r"C:\\sync\\archive"]


def main():
    s = SessionLocal()
    files = []
    for d in DIRS:
        p = Path(d)
        if p.exists():
            files += list(p.glob("stock_*.txt"))
    files.sort(key=lambda f: f.stat().st_mtime)
    rows = {}
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            p = line.split("|")
            if len(p) < 5:
                continue
            d = p[3].strip().lstrip("-")
            if not d.isdigit():
                continue
            rows[p[0].strip()] = {
                "article": p[1].strip(), "name": p[2].strip(), "qty": int(p[3]),
                "barcodes": [b.strip() for b in p[4].split(",") if b.strip()],
                "size": p[5].strip() if len(p) > 5 else "",
                "color": p[6].strip() if len(p) > 6 else "",
            }
    seen = {b.barcode for b in s.query(Barcode).all()}
    pc = bc = uc = 0
    for uid, r in rows.items():
        if not uid:
            continue
        prod = s.query(Product).filter(Product.uid_1c == uid).first()
        if prod is None:
            prod = Product(uid_1c=uid, article=r["article"], name=r["name"],
                           stock_on_hand=r["qty"], size=r["size"] or None, color=r["color"] or None)
            s.add(prod)
            pc += 1
        else:
            prod.article = r["article"] or prod.article
            prod.name = r["name"] or prod.name
            if r["size"]:
                prod.size = r["size"]
            if r["color"]:
                prod.color = r["color"]
            uc += 1
        for code in r["barcodes"]:
            if code and code not in seen:
                s.add(Barcode(barcode=code, uid_1c=uid, source_platform="1c_export"))
                seen.add(code)
                bc += 1
    s.commit()
    print("products created:", pc)
    print("products updated:", uc)
    print("barcodes created:", bc)
    print("with size:", s.query(Product).filter(Product.size.isnot(None)).count())
    print("with color:", s.query(Product).filter(Product.color.isnot(None)).count())
    s.close()


if __name__ == "__main__":
    main()
''')
print("seed_products.py written")
print("DONE")

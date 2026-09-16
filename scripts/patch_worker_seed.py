# -*- coding: utf-8 -*-
"""B: автозавод новых SKU в воркере. Впаивает в часовой цикл сверки заведение/
обновление товаров из выгрузки 1С. Идемпотентно, устойчиво к CRLF."""
import io
import os

ROOT = r"C:\sync_admin"

FTP_OLD = '''def fetch_stock_export_files(exchange: "LocalExchange") -> dict[str, int]:
    """Забирает все накопившиеся stock_*.txt из каталога results, объединяет,
    архивирует. Обычно один файл за раз, но на всякий случай — несколько."""
    combined = {}
    for filename in exchange.list_stock_files():
        content = exchange.download_and_archive_result(filename)
        combined.update(parse_stock_export_file(content))
    return combined'''

FTP_NEW = FTP_OLD + '''


def parse_stock_export_rows(content: str) -> list[dict]:
    """Полный разбор stock_*.txt: 'uid|артикул|наименование|кол-во|баркод1,баркод2|размер|цвет'."""
    rows = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        digits = parts[3].strip().lstrip("-")
        if not digits.isdigit():
            continue
        rows.append({
            "uid_1c": parts[0].strip(),
            "article": parts[1].strip(),
            "name": parts[2].strip(),
            "quantity": int(parts[3]),
            "barcodes": [b.strip() for b in parts[4].split(",") if b.strip()],
            "size": parts[5].strip() if len(parts) > 5 else "",
            "color": parts[6].strip() if len(parts) > 6 else "",
        })
    return rows


def fetch_stock_export_rows(exchange: "LocalExchange") -> list[dict]:
    """Как fetch_stock_export_files, но отдаёт полные строки. Читает все
    stock_*.txt из results и архивирует. Дубли uid схлопываются."""
    combined = {}
    for filename in exchange.list_stock_files():
        content = exchange.download_and_archive_result(filename)
        for r in parse_stock_export_rows(content):
            combined[r["uid_1c"]] = r
    return list(combined.values())'''

RECON_OLD = 'def run_reconciliation(db: Session, stock_from_1c: dict[str, int]) -> dict:'

RECON_NEW = '''def import_product_master(db: Session, rows: list[dict]) -> dict:
    """Заводит/обновляет ассортимент из полной выгрузки 1С. Создаёт недостающие
    Product (остаток = кол-во из 1С, трансляция OFF) и Barcode; у существующих
    обновляет артикул/наименование/размер/цвет, НЕ трогая остаток и ручные поля.
    Идемпотентно."""
    stats = {"created": 0, "updated": 0, "barcodes": 0}
    seen = {b.barcode for b in db.query(Barcode).all()}
    for r in rows:
        uid = (r.get("uid_1c") or "").strip()
        if not uid:
            continue
        product = db.query(Product).filter(Product.uid_1c == uid).first()
        if product is None:
            product = Product(
                uid_1c=uid, article=r.get("article"), name=r.get("name"),
                stock_on_hand=r.get("quantity") or 0,
                size=(r.get("size") or None), color=(r.get("color") or None),
            )
            db.add(product)
            stats["created"] += 1
        else:
            if r.get("article"):
                product.article = r["article"]
            if r.get("name"):
                product.name = r["name"]
            if r.get("size"):
                product.size = r["size"]
            if r.get("color"):
                product.color = r["color"]
            stats["updated"] += 1
        for code in r.get("barcodes", []):
            if code and code not in seen:
                db.add(Barcode(barcode=code, uid_1c=uid, source_platform="1c_export"))
                seen.add(code)
                stats["barcodes"] += 1
    db.commit()
    return stats


def run_reconciliation(db: Session, stock_from_1c: dict[str, int]) -> dict:'''

SCHED_IMP_OLD = '''from app.workers.ftp_channel import (
    LocalExchange, build_task_batch, apply_result_batch, detect_timed_out_tasks,
    fetch_stock_export_files,
)
from app.workers.reconciliation import run_reconciliation'''

SCHED_IMP_NEW = '''from app.workers.ftp_channel import (
    LocalExchange, build_task_batch, apply_result_batch, detect_timed_out_tasks,
    fetch_stock_export_files, fetch_stock_export_rows,
)
from app.workers.reconciliation import run_reconciliation, import_product_master'''

SCHED_JOB_OLD = '''        exchange = _build_ftp_exchange()
        stock = fetch_stock_export_files(exchange)
        if stock:
            stats = run_reconciliation(db, stock)
            logger.info("reconciliation: %s", stats)
        else:
            logger.info("reconciliation: нет свежего файла выгрузки остатков — пропуск")'''

SCHED_JOB_NEW = '''        exchange = _build_ftp_exchange()
        rows = fetch_stock_export_rows(exchange)
        if rows:
            imp = import_product_master(db, rows)
            logger.info("import_products: %s", imp)
            stock = {}
            for r in rows:
                for bc in r["barcodes"]:
                    stock[bc] = r["quantity"]
            stats = run_reconciliation(db, stock)
            logger.info("reconciliation: %s", stats)
        else:
            logger.info("reconciliation: нет свежего файла выгрузки остатков — пропуск")'''

EDITS = [
    (r"app\workers\ftp_channel.py", FTP_OLD, FTP_NEW),
    (r"app\workers\reconciliation.py", RECON_OLD, RECON_NEW),
    (r"app\workers\scheduler.py", SCHED_IMP_OLD, SCHED_IMP_NEW),
    (r"app\workers\scheduler.py", SCHED_JOB_OLD, SCHED_JOB_NEW),
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
    print(apply_edit(os.path.join(ROOT, rel), old, new), rel)
print("DONE")

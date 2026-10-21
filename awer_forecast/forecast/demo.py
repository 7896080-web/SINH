"""Демо на тестовых данных: весь ежедневный цикл на выдуманном ассортименте AWER.

Что настоящее: БД (миграции), импорт Excel-карты, клиент Lamoda v2 и приём данных, чтение базы Sync Admin,
разбор файлов 1С, расчёт и страницы. Что выдумано: ассортимент, продажи за 120 дней,
остатки и ответы Lamoda (имитация поверх httpx.MockTransport, без сети), файлы 1С.

История ежедневных снимков остатка FBO, которая в жизни копится опросом каждый день,
здесь дописывается в БД напрямую — иначе нечего было бы цензурировать.

    python -m forecast demo            # собрать demo/demo.db
    python -m forecast demo --serve    # и открыть страницы (логин demo / пароль demo)
"""
from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from openpyxl import Workbook

from forecast.catalog.excel_import import import_catalog
from forecast.config import PROJECT_DIR, PlanningSettings, Settings
from forecast.db import make_engine
from forecast.db.ops import upsert
from forecast.db.schema import t_product_sizes, t_stock_fbo
from forecast.integrations.lamoda.fake import FakeLamoda, item, order
from forecast.jobs import run_daily
from forecast.timeutil import MSK, today_msk

SEED = 20261021
DAYS = 120


@dataclass
class DemoModel:
    article: str
    color: str
    category: str
    sizes: list[str]
    rate: float                    # продаж в день на всю модель на Lamoda
    price: float
    supplier: str | None
    lamoda_parent: str
    available: str = "да"
    collection: str = "базовая"
    retail_rate: float = 0.0       # розница (1С), в день на модель


MODELS = [
    DemoModel("AW-JK-101", "чёрный", "Куртки", ["46", "48", "50", "52", "54", "56"], 2.4, 8990, "Фабрика Иваново",
              "MP002XM0JK1B", retail_rate=0.6),
    DemoModel("AW-JK-101", "хаки", "Куртки", ["46", "48", "50", "52", "54", "56"], 1.1, 8990, "Фабрика Иваново",
              "MP002XM0JK1K"),
    DemoModel("AW-TS-210", "белый", "Футболки", ["S", "M", "L", "XL", "XXL"], 4.0, 1990, "Текстиль-Про",
              "MP002XM0TS2W", retail_rate=1.5),
    DemoModel("AW-PT-305", "синий", "Брюки", ["46", "48", "50", "52", "54"], 1.3, 4490, "Текстиль-Про",
              "MP002XM0PT3N"),
    DemoModel("AW-SW-410", "серый меланж", "Свитшоты", ["S", "M", "L", "XL"], 1.0, 3990, "Фабрика Иваново",
              "MP002XM0SW4G", available="нет"),
    DemoModel("AW-CP-900", "бордо", "Капсула", ["M", "L"], 0.3, 12990, None, "MP002XM0CP9B", collection="капсульная"),
]
SUPPLIERS = {"Фабрика Иваново": (45, 15, 60), "Текстиль-Про": (20, 7, 100)}
SIZE_WEIGHTS = {6: [0.08, 0.18, 0.26, 0.24, 0.16, 0.08], 5: [0.12, 0.26, 0.30, 0.22, 0.10],
                4: [0.18, 0.32, 0.32, 0.18], 2: [0.5, 0.5]}


@dataclass
class DemoSize:
    model: DemoModel
    size: str
    barcode_1c: str
    barcode_alt: str | None
    lamoda_sku: str
    rate: float
    retail_rate: float


def _sizes() -> list[DemoSize]:
    out, n = [], 0
    for m in MODELS:
        weights = SIZE_WEIGHTS[len(m.sizes)]
        for size, w in zip(m.sizes, weights):
            n += 1
            # у части размеров на Lamoda заведён другой баркод, чем в 1С: сопоставление — через пул
            alt = f"20000{n:08d}" if n % 3 == 0 else None
            out.append(DemoSize(m, size, f"46000{n:08d}", alt, f"{m.lamoda_parent}IN{size}", m.rate * w,
                                m.retail_rate * w))
    return out


def _catalog_xlsx(sizes: list[DemoSize], path: Path) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Карта"
    ws.append(["Карта сопоставления AWER — демо"])
    ws.append([])
    ws.append(["Артикул", "Цвет", "Размер", "Штрихкод", "Seller SKU", "Lamoda SKU", "Категория", "Поставщик",
               "Срок производства", "Срок доставки", "MOQ", "Доступен к заказу", "Тип коллекции"])
    for i, s in enumerate(sizes):
        barcodes = s.barcode_1c + (f", {s.barcode_alt}" if s.barcode_alt else "")
        supplier = SUPPLIERS.get(s.model.supplier) if s.model.supplier else None
        # Lamoda SKU в карте есть не у всех: недостающие проставит сверка с каталогом Lamoda
        ws.append([s.model.article, s.model.color, s.size, barcodes, None,
                   s.lamoda_sku if i % 2 == 0 else None, s.model.category, s.model.supplier,
                   supplier[0] if supplier else None, supplier[1] if supplier else None,
                   supplier[2] if supplier else None, s.model.available, s.model.collection])
    for col in "ABCDEFGHIJKLM":
        ws.column_dimensions[col].width = 16
    wb.save(path)
    return path.read_bytes()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _simulate(sizes: list[DemoSize], today: date, rng: random.Random):
    """Продажи Lamoda с учётом остатка FBO: поставка раз в ~3 недели, при нуле — продаж нет."""
    fake = FakeLamoda(seller_id="100500")
    fbo_history: dict[int, dict[date, int]] = {}
    shipments, order_n, item_n = [], 0, 0
    start = today - timedelta(days=DAYS)
    final_stock = {}
    for idx, s in enumerate(sizes):
        stock = max(2, round(s.rate * 25))
        history = {}
        for day_i in range(DAYS):
            day = start + timedelta(days=day_i)
            if day_i % 21 == 20 and s.model.collection == "базовая":
                stock += max(1, round(s.rate * 21))           # пришла поставка
            # у чёрной куртки 50-го — дефицит последние 12 дней: проверка цензурирования
            if s.model.article == "AW-JK-101" and s.model.color == "чёрный" and s.size == "50" and day_i >= DAYS - 12:
                stock = 0
            demand = sum(1 for _ in range(6) if rng.random() < s.rate / 6)
            sold = min(stock, demand)
            stock -= sold
            history[day] = stock
            for _ in range(sold):
                order_n += 1
                item_n += 1
                moment = datetime.combine(day, time(rng.randint(8, 21), rng.randint(0, 59)), MSK)
                status = "DELIVERED"
                updated = moment + timedelta(days=rng.randint(1, 3))
                if rng.random() < 0.07:                       # возврат после выкупа
                    status = "REFUNDED"
                    updated += timedelta(days=rng.randint(5, 15))
                if updated.date() >= today:
                    updated = datetime.combine(today - timedelta(days=1), time(12), MSK)
                ext = s.barcode_alt or s.barcode_1c
                fake.orders.append(order(f"{1000000 + order_n}", _iso(updated),
                                         [item(f"{9000000 + item_n}", s.lamoda_sku, status, s.model.price, ext)],
                                         created_at=_iso(moment)))
        fbo_history[idx] = history
        final_stock[idx] = stock
    for idx, s in enumerate(sizes):
        fake.stocks.append({"sellerId": "100500", "sku": s.lamoda_sku, "externalSku": s.barcode_alt or s.barcode_1c,
                            "warehouseCode": "BYKOVO", "quantity": final_stock[idx],
                            "createdAt": _iso(datetime.now(timezone.utc)), "updatedAt": _iso(datetime.now(timezone.utc))})
        fake.nomenclatures.append({"sku": s.lamoda_sku, "parentSku": s.model.lamoda_parent,
                                   "externalSku": s.barcode_alt or s.barcode_1c,
                                   "barcode": s.barcode_alt or s.barcode_1c, "externalSize": s.size,
                                   "name": f"{s.model.category} AWER {s.model.color}"})
    # одна поставка уже в пути: футболки M и L
    fake.shipments.append({"id": "FBO-DEMO-1", "externalShipmentId": "AWER-1", "status": "CONFIRMED",
                           "plannedDate": (today + timedelta(days=3)).isoformat(),
                           "createdAt": _iso(datetime.combine(today - timedelta(days=2), time(10), MSK)),
                           "warehouseCode": "BYKOVO", "statistics": {}})
    for s in sizes:
        if s.model.article == "AW-TS-210" and s.size in ("M", "L"):
            fake.shipment_items["FBO-DEMO-1"].append({"id": s.size, "sku": s.lamoda_sku, "ean": s.barcode_1c,
                                                      "quantityPlanned": 12, "quantityAccepted": 0})
    fake.orders.sort(key=lambda o: o["updatedAt"])
    return fake, fbo_history


def _write_1c_files(sizes: list[DemoSize], base: Path, today: date, rng: random.Random) -> tuple[Path, Path]:
    archive = base / "sync" / "archive"
    sales_dir = base / "sales"
    archive.mkdir(parents=True, exist_ok=True)
    sales_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for n, s in enumerate(sizes):
        qty = rng.choice([0, 0, 2, 5, 8, 15, 30]) if s.model.article != "AW-TS-210" else rng.choice([40, 60])
        if s.model.article == "AW-SW-410":
            qty = rng.choice([0, 1])
        barcodes = s.barcode_1c + (f",{s.barcode_alt}" if n % 2 else "")
        lines.append(f"uid-{n:04d}|{s.model.article}|{s.model.category} AWER {s.model.color}|{qty}|{barcodes}|"
                     f"{s.size}|{s.model.color}")
    lines.append("uid-9999|ЧУЖОЙ-1|Товар другого бренда|12|4699999999999|M|")   # в справочнике его нет — так и надо
    stock_path = archive / f"stock_{today:%Y%m%d}073000.txt"
    stock_path.write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")
    start = today - timedelta(days=90)
    rows = []
    for d in range(90):
        day = start + timedelta(days=d)
        for s in sizes:
            sold = sum(1 for _ in range(4) if rng.random() < s.retail_rate / 4)
            if sold:
                rows.append(f"{day:%Y%m%d}|{s.barcode_1c}|{sold}|{sold * s.model.price:.2f}|retail_1")
    sales_path = sales_dir / f"sales_{start:%Y%m%d}_{today - timedelta(days=1):%Y%m%d}_{today:%Y%m%d}080000.txt"
    sales_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return stock_path, sales_path


def _write_sync_admin_db(sizes: list[DemoSize], path: Path, today: date, rng: random.Random) -> None:
    """Имитация базы Sync Admin: заказы WB/Ozon/сайта — задания перемещений в 1С (ftp_tasks).
    Только колонки, которые читает forecast/integrations/syncadmin.py, с теми же именами."""
    import sqlite3
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE platform_accounts (id INTEGER PRIMARY KEY, platform VARCHAR(4) NOT NULL, name VARCHAR(128));
        CREATE TABLE ftp_tasks (id INTEGER PRIMARY KEY, command VARCHAR(32) NOT NULL, barcode VARCHAR(64),
            quantity INTEGER, order_id VARCHAR(128) NOT NULL, account_id INTEGER NOT NULL, movement_date DATE,
            status VARCHAR(7), created_at DATETIME, is_test BOOLEAN NOT NULL);
        INSERT INTO platform_accounts VALUES (1, 'wb', 'WB'), (2, 'ozon', 'Ozon'), (3, 'kit', 'Сайт');
    """)
    rows, n = [], 0
    start = today - timedelta(days=90)
    for d in range(90):
        day = start + timedelta(days=d)
        for s in sizes:
            for account_id, share in ((1, 0.5), (2, 0.35), (3, 0.15)):   # доля от розничного темпа
                if rng.random() < s.retail_rate * share:
                    n += 1
                    created = datetime.combine(day, time(rng.randint(6, 19), 0))   # UTC, как в Sync Admin
                    rows.append(("CREATE_MOVEMENT", s.barcode_1c, 1, f"D{n}", account_id, None, "done",
                                 created.isoformat(sep=" "), 0))
                    if rng.random() < 0.08:                                       # отмена до подтверждения
                        rows.append(("CANCEL_MOVEMENT", s.barcode_1c, 1, f"D{n}", account_id, None, "done",
                                     (created + timedelta(hours=5)).isoformat(sep=" "), 0))
    rows.append(("CREATE_MOVEMENT", sizes[0].barcode_1c, 50, "TEST1", 1, None, "done",
                 datetime.combine(today - timedelta(days=1), time(9)).isoformat(sep=" "), 1))  # симуляция — не спрос
    con.executemany("INSERT INTO ftp_tasks (command, barcode, quantity, order_id, account_id, movement_date, "
                    "status, created_at, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def build_demo(base: Path, *, quiet: bool = False) -> Settings:
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / "demo.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    url = f"sqlite:///{db_path}"
    cfg = Config(str(PROJECT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")

    rng = random.Random(SEED)
    today = today_msk()
    sizes = _sizes()
    engine = make_engine(url)
    card = _catalog_xlsx(sizes, base / "карта_мэппинга_демо.xlsx")
    with engine.begin() as conn:
        report = import_catalog(conn, card)
    fake, fbo_history = _simulate(sizes, today, rng)
    _write_1c_files(sizes, base, today, rng)
    _write_sync_admin_db(sizes, base / "sync_admin.db", today, rng)

    from forecast.security import hash_password
    settings = Settings(
        database_url=url, session_secret="demo-session-secret", login="demo", password_hash=hash_password("demo"),
        lamoda_base_url="https://public-api-seller.lamoda.ru/api", lamoda_client_id="id", lamoda_client_secret="secret",
        lamoda_seller_id="100500", lamoda_country="RU", orders_backfill_days=180, orders_overlap_hours=6,
        onec_exchange_dirs=(base / "sync" / "results", base / "sync" / "archive"), onec_sales_dir=base / "sales",
        planning=PlanningSettings(), sync_admin_database=str(base / "sync_admin.db"),
    )
    # история ежедневных снимков FBO (в жизни — результат ежедневного опроса)
    with engine.begin() as conn:
        psids = {(r.lamoda_sku or ""): r.product_size_id for r in conn.execute(
            t_product_sizes.select())}
        by_barcode = {}
        from forecast.catalog.resolver import SkuResolver
        resolver = SkuResolver.load(conn)
        rows = []
        for idx, s in enumerate(sizes):
            psid = resolver.resolve(barcodes=[s.barcode_1c])
            by_barcode[s.barcode_1c] = psid
            for day, qty in fbo_history[idx].items():
                if day < today:
                    rows.append({"product_size_id": psid, "warehouse_code": "BYKOVO", "snapshot_date": day, "qty": qty})
        upsert(conn, t_stock_fbo, rows, keys=["product_size_id", "warehouse_code", "snapshot_date"], update=["qty"])
    results = run_daily(engine, settings, started_by="demo", http=fake.client())
    engine.dispose()
    if not quiet:
        print(f"Справочник: моделей {report.models_created}, размеров {report.sizes_created}, баркодов {report.barcodes_added}")
        for r in results:
            print(f"  [{r.status}] {r.job}: {r.message}")
        print(f"\nБаза демо: {db_path}")
    return replace(settings)

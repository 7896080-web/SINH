"""Помощники тестов финансового модуля: файлы отчётов в памяти и готовые строки."""
import io
import zipfile
from datetime import date

from openpyxl import Workbook

from finapp.finance import values as v
from finapp.models import Barcode, PlatformAccount, Product, SalesReport, SalesReportLine

# Колонки детализации WB в порядке из справки WB (сокращённо — только те, что
# участвуют в расчёте, плюс «Предмет» левее «Названия», как в настоящем файле).
WB_HEADERS = [
    "№", "Номер поставки", "Предмет", "Код номенклатуры", "Бренд", "Артикул поставщика",
    "Название", "Размер", "Баркод", "Тип документа", "Обоснование для оплаты",
    "Дата заказа покупателем", "Дата продажи", "Кол-во", "Цена розничная",
    "Вайлдберриз реализовал Товар (Пр)", "К перечислению Продавцу за реализованный Товар",
    "Услуги по доставке товара покупателю", "Общая сумма штрафов",
    "Корректировка Вознаграждения Вайлдберриз (ВВ)", "Номер сборочного задания", "Srid",
    "Возмещение издержек по перевозке/по складским операциям с товаром", "Хранение",
    "Удержания", "Услуга по обработке товара",
]


def wb_row(**kw) -> list:
    """Строка детализации WB; по умолчанию — продажа одной штуки за 2500 с
    выплатой 2100."""
    values = {
        "№": 1, "Номер поставки": "", "Предмет": "Джинсы", "Код номенклатуры": 123456,
        "Бренд": "AWER", "Артикул поставщика": "A-1", "Название": "Джинсы прямые",
        "Размер": "46", "Баркод": "2000000000012", "Тип документа": "Продажа",
        "Обоснование для оплаты": "Продажа", "Дата заказа покупателем": "2026-09-01",
        "Дата продажи": "2026-09-03", "Кол-во": 1, "Цена розничная": 3000,
        "Вайлдберриз реализовал Товар (Пр)": 2500,
        "К перечислению Продавцу за реализованный Товар": 2100,
        "Услуги по доставке товара покупателю": 0, "Общая сумма штрафов": 0,
        "Корректировка Вознаграждения Вайлдберриз (ВВ)": 0, "Номер сборочного задания": 0,
        "Srid": "srid-1",
        "Возмещение издержек по перевозке/по складским операциям с товаром": 0,
        "Хранение": 0, "Удержания": 0, "Услуга по обработке товара": 0,
    }
    values.update(kw)
    return [values[h] for h in WB_HEADERS]


def make_xlsx(headers: list, rows: list[list], title_rows: list[list] = (), sheet: str = "Лист1") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for row in title_rows:
        ws.append(row)
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def zip_bytes(name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    return buf.getvalue()


def wb_file(rows: list[list]) -> bytes:
    return make_xlsx(WB_HEADERS, rows)


def make_account(db, platform: str = "wb", name: str = "Тестовый кабинет", is_active: bool = True,
                 sync_account_id: int | None = None) -> PlatformAccount:
    account = PlatformAccount(platform=platform, name=name, is_active=is_active,
                              sync_account_id=sync_account_id)
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def make_product(db, uid="u1", article="A-1", name="Джинсы", size="46", barcodes=("2000000000012",)):
    product = Product(uid_1c=uid, article=article, name=name, size=size, color="синий")
    db.add(product)
    for bc in barcodes:
        db.add(Barcode(barcode=bc, uid_1c=uid))
    db.commit()
    return product


def make_report(db, platform="wb", account_id=None, period=(date(2026, 9, 1), date(2026, 9, 7)),
                key=None, lines=()) -> SalesReport:
    """Отчёт напрямую в базу (без файла) — для расчётов и сверки."""
    report = SalesReport(platform=platform, account_id=account_id, period_from=period[0],
                         period_to=period[1], source_format="unified",
                         onec_key=key or f"SR-test-{platform}-{period[0]:%Y%m%d}-{account_id}",
                         lines_count=len(lines))
    db.add(report)
    db.flush()
    for spec in lines:
        data = {"operation": v.SALE, "operation_date": period[0], "quantity": 1}
        data.update(spec)
        for name in v.MONEY_FIELDS:
            data[f"{name}_kop"] = data.pop(name, 0)
        db.add(SalesReportLine(report_id=report.id, **data))
    db.commit()
    db.refresh(report)
    return report

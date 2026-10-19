"""Детализация WB: выручка по цене продавца, налог с «Вайлдберриз реализовал».

Сверено 19.10.2026 на отчёте №876850339 (12–18.10, 1 700 строк) построчно, ±1 ₽:
«Вайлдберриз реализовал» = цена продавца × (1 − СПП); к перечислению = цена
продавца × (1 − кВВ − эквайринг). СПП — скидка WB покупателю за свой счёт, на
выплату продавцу не влияет; поэтому выручка — по цене продавца, комиссия — кВВ и
эквайринг, а налог — с «реализовано» (владелец). Строки ниже — из этого отчёта."""
import io
import zipfile
from datetime import date

from finapp.finance import values as v
from finapp.finance.economics import Filters, unit_economics
from finapp.finance.formats import WB_DETAIL_VERSION, parse_records, parse_sheets
from finapp.finance.importer import import_report, number_from_filename, store_api_report
from finapp.finance.readers import read_sheets
from finapp.finance.settings import load_settings
from finapp.models import OneCUploadStatus, SalesReport, SalesReportLine
from tests.finance_factories import make_account, make_product, make_xlsx

HEADERS = [
    "№", "Предмет", "Код номенклатуры", "Артикул поставщика", "Название", "Размер", "Баркод",
    "Тип документа", "Обоснование для оплаты", "Дата заказа покупателем", "Дата продажи", "Кол-во",
    "Цена розничная", "Вайлдберриз реализовал Товар (Пр)", "Цена розничная с учетом согласованной скидки",
    "Платформенные скидки, %", "Размер кВВ, %",
    "Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов",
    "К перечислению Продавцу за реализованный Товар", "Услуги по доставке товара покупателю",
    "Общая сумма штрафов", "Виды доставок, штрафов и корректировок ВВ", "Склад", "Номер сборочного задания",
    "Srid", "Хранение", "Удержания",
]


def row(**kw):
    values = {h: 0 for h in HEADERS}
    values.update({"№": 1, "Предмет": "Джемперы", "Код номенклатуры": 35909861,
                   "Артикул поставщика": "A-1", "Название": "Джемпер премиум Турция", "Размер": "XL",
                   "Баркод": "2000000000012", "Тип документа": "", "Обоснование для оплаты": "",
                   "Дата заказа покупателем": "2026-10-08", "Дата продажи": "2026-10-13",
                   "Виды доставок, штрафов и корректировок ВВ": "", "Склад": "", "Srid": ""})
    values.update(kw)
    return [values[h] for h in HEADERS]


SALE = dict(**{"Тип документа": "Продажа", "Обоснование для оплаты": "Продажа", "Кол-во": 1,
               "Цена розничная": 6750, "Вайлдберриз реализовал Товар (Пр)": 4117,
               "Цена розничная с учетом согласованной скидки": 6750, "Платформенные скидки, %": 39.01,
               "Размер кВВ, %": 46.85,
               "Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов": 164.68,
               "К перечислению Продавцу за реализованный Товар": 3422.94,
               "Склад": "Склад поставщика - везу на склад WB", "Srid": "s-1"})
RETURN = dict(SALE, **{"Тип документа": "Возврат", "Обоснование для оплаты": "Возврат", "Srid": "s-2",
                       "Цена розничная": 3071, "Вайлдберриз реализовал Товар (Пр)": 2008,
                       "Цена розничная с учетом согласованной скидки": 3071, "Размер кВВ, %": 44.35,
                       "Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов": 12.65,
                       "К перечислению Продавцу за реализованный Товар": 1696.24, "Склад": "Склад WB"})


def delivery(kind, amount):
    return row(**{"Обоснование для оплаты": "Доставка", "Услуги по доставке товара покупателю": amount,
                  "Виды доставок, штрафов и корректировок ВВ": kind})


def report_rows():
    return [row(**SALE), row(**RETURN),
            delivery("К клиенту при продаже", 248.0), delivery("К клиенту при отмене", 303.0),
            delivery("От клиента при отмене", 173.0),
            row(**{"Обоснование для оплаты": "Хранение", "Хранение": 1100.5, "Баркод": ""}),
            row(**{"Обоснование для оплаты": "Удержание", "Удержания": 43512, "Баркод": "",
                   "Виды доставок, штрафов и корректировок ВВ": "Оказание услуг «WB Продвижение», документ №319971361"}),
            row(**{"Обоснование для оплаты": "Штраф", "Общая сумма штрафов": 10, "Баркод": "",
                   "Виды доставок, штрафов и корректировок ВВ": "Штраф за нарушение срока передачи товара"})]


def parsed_file(rows=None):
    return parse_sheets(read_sheets(make_xlsx(HEADERS, rows or report_rows()), "r.xlsx"), "wb")


def test_sale_is_read_from_the_seller_price_and_tax_base_is_what_wb_realized():
    parsed = parsed_file()
    assert parsed.errors == [] and parsed.detail_version == WB_DETAIL_VERSION
    sale, ret = parsed.lines[0], parsed.lines[1]
    assert (sale["revenue"], sale["realized"], sale["payout"]) == (675000, 411700, 342294)
    assert sale["commission"] == 675000 - 342294                      # кВВ (в нём СПП) + эквайринг
    assert (sale["fee_pct_bp"], sale["acquiring"], sale["list_price"]) == (4685, 16468, 675000)
    assert sale["warehouse"] == "Склад поставщика - везу на склад WB"
    assert (ret["revenue"], ret["realized"], ret["payout"], ret["acquiring"]) == (-307100, -200800, -169624, -1265)
    kinds = [line["charge_kind"] for line in parsed.lines[2:]]
    assert kinds == ["К клиенту при продаже", "К клиенту при отмене", "От клиента при отмене", "",
                     "Оказание услуг «WB Продвижение»", "Штраф за нарушение срока передачи товара"]


def test_old_file_without_seller_price_keeps_the_old_reading():
    old_headers = [h for h in HEADERS if h not in ("Цена розничная с учетом согласованной скидки", "Размер кВВ, %")]
    rows = [[value for h, value in zip(HEADERS, r) if h in old_headers] for r in report_rows()]
    parsed = parse_sheets(read_sheets(make_xlsx(old_headers, rows), "r.xlsx"), "wb")
    assert parsed.detail_version == 0
    sale = parsed.lines[0]
    assert sale["revenue"] == sale["realized"] == 411700 and sale["fee_pct_bp"] is None


def test_api_fields_are_the_same_columns():
    record = {"rrdId": 1, "reportId": 900, "docTypeName": "Продажа", "sellerOperName": "Продажа",
              "saleDt": "2026-10-13T12:00:00", "quantity": 1, "retailPrice": 6750, "retailAmount": 4117,
              "retailPriceWithDisc": 6750, "commissionPercent": 46.85, "acquiringFee": 164.68,
              "forPay": 3422.94, "sku": "2000000000012", "officeName": "Склад WB", "bonusTypeName": ""}
    delivery_record = {"rrdId": 2, "reportId": 900, "docTypeName": "", "sellerOperName": "Доставка",
                       "saleDt": "2026-10-13T12:00:00", "quantity": 0, "retailAmount": 0, "forPay": 0,
                       "deliveryService": 303, "bonusTypeName": "К клиенту при отмене", "sku": "2000000000012"}
    parsed = parse_records([record, delivery_record], "wb")
    sale, logistics = parsed.lines
    assert parsed.detail_version == WB_DETAIL_VERSION
    assert (sale["revenue"], sale["realized"], sale["fee_pct_bp"], sale["warehouse"]) == (675000, 411700, 4685, "Склад WB")
    assert logistics["charge_kind"] == "К клиенту при отмене"


def _imported(db, rows=None):
    make_product(db)
    outcome = import_report(db, platform="wb", file_bytes=make_xlsx(HEADERS, rows or report_rows()),
                            filename="Еженедельный детализированный отчет №876850339_72066 - 1.xlsx", user="t",
                            period_from=date(2026, 10, 12), period_to=date(2026, 10, 18))
    assert outcome.errors == []
    return outcome.report


def test_economics_seller_price_revenue_and_tax_from_realized(db):
    report = _imported(db)
    assert report.report_number == "876850339" and report.parser_version == WB_DETAIL_VERSION
    settings = load_settings(db)
    rows, unassigned, total, _ = unit_economics(db, Filters(date(2026, 10, 1), date(2026, 10, 31)), settings)
    t = total
    assert t.revenue == 675000 - 307100 and t.realized == 411700 - 200800
    assert t.spp == t.revenue - t.realized
    assert t.vat == round(t.realized * 5 / 105)                       # налог — с «реализовано»
    assert t.tax == round((t.realized - t.vat) * 5 / 100)
    assert t.acquiring == 16468 - 1265
    assert t.logistics_nonbuyout == 30300 + 17300                     # «при отмене» туда и обратно
    expected_fee = (675000 * 4685 - 307100 * 4435) / (675000 - 307100) / 10000
    assert abs(t.fee_pct - expected_fee) < 1e-4
    assert t.total_payout == 342294 - 169624 - (24800 + 30300 + 17300) - 110050 - 4351200 - 1000


def test_api_report_is_enriched_by_the_file_when_totals_match(db, client):
    make_product(db)
    account = make_account(db, name="Яворская")
    old_headers = [h for h in HEADERS if h not in ("Цена розничная с учетом согласованной скидки", "Размер кВВ, %",
                                                   "Виды доставок, штрафов и корректировок ВВ")]
    old_rows = [[value for h, value in zip(HEADERS, r) if h in old_headers] for r in report_rows()]
    old = parse_sheets(read_sheets(make_xlsx(old_headers, old_rows), "r.xlsx"), "wb")
    stored = store_api_report(db, platform="wb", account_id=account.id, parsed=old, period_from=date(2026, 10, 12),
                              period_to=date(2026, 10, 18), user="api", report_number="876850339")
    report = stored.report
    report.onec_status = OneCUploadStatus.done
    report.bank_payout_kop = 123
    db.commit()
    payout_before = sum(l.payout_kop for l in report.lines)

    data = make_xlsx(HEADERS, report_rows())
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Еженедельный детализированный отчет №876850339_72066 - 1.xlsx", data)
    response = client.post("/finance/reports/upload", data={"platform": "wb", "account_id": str(account.id)},
                           files={"file": ("ccf268c7-_____876850339_72066.zip", archive.getvalue())},
                           follow_redirects=True)
    assert "итоги файла сошлись с ним до копейки" in response.text
    db.expire_all()
    assert db.query(SalesReport).count() == 1
    report = db.get(SalesReport, report.id)
    assert report.parser_version == WB_DETAIL_VERSION
    assert report.onec_status == OneCUploadStatus.done and report.bank_payout_kop == 123
    lines = db.query(SalesReportLine).filter(SalesReportLine.report_id == report.id).all()
    assert sum(l.payout_kop for l in lines) == payout_before
    assert sum(l.revenue_kop for l in lines if l.operation in v.GOODS_OPERATIONS) == 675000 - 307100
    assert {l.charge_kind for l in lines if l.logistics_kop} == {"К клиенту при продаже", "К клиенту при отмене",
                                                                "От клиента при отмене"}
    again = client.post("/finance/reports/upload", data={"platform": "wb"},
                        files={"file": ("ccf268c7-_____876850339_72066.zip", archive.getvalue())},
                        follow_redirects=True)
    assert "Этот файл уже загружен" in again.text


def test_file_that_disagrees_with_the_loaded_report_changes_nothing(db):
    make_product(db)
    old_headers = [h for h in HEADERS if h != "Цена розничная с учетом согласованной скидки"]
    old_rows = [[value for h, value in zip(HEADERS, r) if h in old_headers] for r in report_rows()]
    old = parse_sheets(read_sheets(make_xlsx(old_headers, old_rows), "r.xlsx"), "wb")
    report = store_api_report(db, platform="wb", account_id=None, parsed=old, period_from=date(2026, 10, 12),
                              period_to=date(2026, 10, 18), user="api", report_number="876850339").report
    changed = report_rows()
    changed[0] = row(**dict(SALE, **{"К перечислению Продавцу за реализованный Товар": 3400}))
    outcome = import_report(db, platform="wb", file_bytes=make_xlsx(HEADERS, changed),
                            filename="отчет №876850339_72066.xlsx", user="t")
    assert outcome.report is None and "не сошлись — ничего не изменено" in outcome.errors[0]
    assert "2000000000012" in outcome.errors[0]
    db.expire_all()
    assert db.get(SalesReport, report.id).parser_version == 0
    assert sum(l.revenue_kop for l in report.lines if l.operation == "sale") == 411700


def test_report_number_from_a_renamed_archive():
    assert number_from_filename("ccf268c7-_____________________________________876850339_72066.zip") == "876850339"
    assert number_from_filename("Еженедельный детализированный отчет №848966494_531247 - 1.xlsx") == "848966494"
    assert number_from_filename("отчёт 2026-10-12.xlsx") == ""


def test_pages_show_kvv_spp_nonbuyout_and_kinds(db, client):
    _imported(db)
    period = "date_from=2026-10-01&date_to=2026-10-31"
    ue = client.get(f"/finance/unit-economics?{period}").text
    assert "кВВ по отчёту" in ue and "невыкуп" in ue
    dash = client.get(f"/finance?{period}").text
    assert 'id="wb-charges"' in dash
    assert "Оказание услуг «WB Продвижение»" in dash and "№319971361" not in dash
    assert "Невыкуп:" in dash and "К клиенту при отмене" in dash
    from io import BytesIO
    from openpyxl import load_workbook
    raw = client.get(f"/finance/unit-economics/export?{period}").content
    rows = list(load_workbook(BytesIO(raw), read_only=True).active.iter_rows(values_only=True))
    first = dict(zip(rows[0], rows[1]))
    assert first["Реализовано площадкой (база налога)"] == 2109 and first["Доставка невыкупа"] == 476


def test_unified_export_keeps_the_tax_base(db, client):
    report = _imported(db)
    raw = client.get(f"/finance/reports/{report.id}/export").content
    from openpyxl import load_workbook
    rows = list(load_workbook(io.BytesIO(raw), read_only=True).active.iter_rows(values_only=True))
    head = list(rows[0])
    sale = dict(zip(head, rows[1]))
    assert sale["Выручка"] == 6750 and sale["Реализовано площадкой (база налога)"] == 4117
    assert sale["кВВ, %"] == 46.85
    reparsed = parse_sheets([("Лист1", [head[5:]] + [list(r[5:]) for r in rows[1:]])], "wb")
    assert reparsed.errors == []
    assert reparsed.lines[0]["realized"] == 411700 and reparsed.lines[1]["realized"] == -200800
    assert reparsed.lines[0]["fee_pct_bp"] == 4685


def test_migration_backfills_realized(tmp_path):
    import sqlite3
    from tests.test_cost_file import _alembic
    url = f"sqlite:///{tmp_path / 'm.db'}"
    _alembic(url, "upgrade", "0009")
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.execute("INSERT INTO sales_reports (id, platform, period_from, period_to, source_format, source, "
                 "lines_count, unmatched_lines, onec_key, onec_status, onec_lines_sent) "
                 "VALUES (1, 'wb', '2026-10-12', '2026-10-18', 'wb_detail', 'api', 1, 0, 'k', 'none', 0)")
    conn.execute("INSERT INTO sales_report_lines (report_id, operation, operation_date, quantity, revenue_kop, "
                 "commission_kop, payout_kop, logistics_kop, storage_kop, penalty_kop, acceptance_kop, "
                 "deduction_kop, compensation_kop) VALUES (1, 'sale', '2026-10-13', 1, 411700, 0, 342294, 0, 0, 0, 0, 0, 0)")
    conn.commit()
    conn.close()
    _alembic(url, "upgrade", "head")
    conn = sqlite3.connect(tmp_path / "m.db")
    assert conn.execute("SELECT realized_kop, fee_pct_bp, charge_kind FROM sales_report_lines").fetchone() == (411700, None, None)
    assert conn.execute("SELECT parser_version FROM sales_reports").fetchone() == (0,)
    conn.close()
    _alembic(url, "downgrade", "0009")
    _alembic(url, "upgrade", "head")

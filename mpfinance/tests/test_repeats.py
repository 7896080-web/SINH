"""Одни и те же продажи в двух отчётах — отказ (`finance/repeats.py`).

Тот же файл и тот же отчёт площадки второй раз не ложились и раньше; здесь —
одни и те же ПРОДАЖИ, пришедшие разными отчётами: выгрузка AWER CRM файлом и
те же недели из базы CRM, месячный файл поверх недельного, продажи из API KIT
поверх продаж из CRM. Раньше было только предупреждение о пересечении
периодов, а выручка ложилась вдвое и в 1С уходило два документа."""
from datetime import date, datetime

from finapp.finance import fetch, repeats, values as v
from finapp.finance.formats import ParseResult
from finapp.finance.importer import import_report, store_api_report
from finapp.finance.sales_1c import upload_problems
from finapp.models import SalesReport
from tests.finance_factories import make_account, make_report, make_xlsx
from tests.test_crm_db import WEEK_A, WEEK_B, _crm_reports, _kit, _run, seed

HEADERS = ["Дата", "Номер заказа", "Артикул", "Баркод", "Сумма"]


def crm_file(rows, name="sales-items-2026-09-01_2026-09-30-2026-10-18_124936.xlsx"):
    total = sum(r[4] for r in rows)
    data = make_xlsx(HEADERS, rows + [[None] * 5, [None, None, None, "Итого:", total]], sheet="Продажи")
    return data, name


SEP_ROW = [datetime(2026, 9, 8, 12, 14, 37), 100608, "39615MAVIMEL", "2000932091174", 4600]


def test_crm_file_with_sales_already_taken_from_the_crm_base_is_refused(db, crm_db):
    seed(crm_db)
    account = _kit(db)
    _run(db, account)
    data, name = crm_file([SEP_ROW])
    outcome = import_report(db, platform="kit", file_bytes=data, filename=name, user="admin")
    assert outcome.report is None and outcome.repeat
    week_a = _crm_reports(db)[0]
    assert "уже загружены: 1 шт из 1" in outcome.errors[0] and f"#{week_a.id}" in outcome.errors[0]
    assert db.query(SalesReport).filter(SalesReport.source == "file").count() == 0


def test_crm_base_skips_a_week_already_loaded_by_file_and_says_why(db, crm_db):
    """Файл — без кабинета, база — с кабинетом: сравниваются всё равно (отчёт
    без кабинета может быть отчётом любого)."""
    seed(crm_db)
    data, name = crm_file([SEP_ROW])
    file_report = import_report(db, platform="kit", file_bytes=data, filename=name, user="admin").report
    account = _kit(db)
    job = _run(db, account)
    assert [(r.period_from, r.period_to) for r in _crm_reports(db)] == [WEEK_B]
    assert "неделя 07.09–13.09.2026: не загружена" in job.message and f"#{file_report.id}" in job.message


def test_overlapping_files_with_the_same_sales_are_refused(db):
    month, name = crm_file([SEP_ROW, [datetime(2026, 9, 20), 100716, "55045 6089", "2000932206462", 4100]])
    import_report(db, platform="kit", file_bytes=month, filename=name, user="admin")
    half, half_name = crm_file([SEP_ROW], name="sales-items-2026-09-01_2026-09-15-2026-10-18_130000.xlsx")
    outcome = import_report(db, platform="kit", file_bytes=half, filename=half_name, user="admin")
    assert outcome.repeat and outcome.report is None


def test_same_order_number_with_another_barcode_or_a_return_is_not_a_repeat(db):
    data, name = crm_file([SEP_ROW])
    import_report(db, platform="kit", file_bytes=data, filename=name, user="admin")
    other = [datetime(2026, 9, 8), 100608, "39615MAVIMEL", "2000932091150", 4600]           # другой размер
    refund = [datetime(2026, 9, 22), 100608, "39615MAVIMEL", "2000932091174", -4600]        # возврат
    data2, name2 = crm_file([other, refund], name="sales-items-2026-09-16_2026-09-30-2026-10-18_131000.xlsx")
    outcome = import_report(db, platform="kit", file_bytes=data2, filename=name2, user="admin")
    assert outcome.errors == [] and outcome.report is not None


def test_numbered_official_reports_are_not_compared_with_each_other(db):
    """У WB одна продажа законно бывает в двух официальных отчётах (сторно и
    корректная продажа); их единица — номер отчёта, повтор номера ловит ключ."""
    account = make_account(db)
    sale = dict(v.empty_line(), operation=v.SALE, quantity=1, revenue=100000, payout=90000,
                srid="srid-1", barcode="2000000000012", operation_date=date(2026, 9, 8))
    for number in ("101", "102"):
        outcome = store_api_report(db, platform="wb", account_id=account.id,
                                   parsed=ParseResult(format=fetch.KIT_API_FORMAT, lines=[dict(sale)]),
                                   period_from=WEEK_A[0], period_to=WEEK_A[1], user="api", report_number=number)
        assert outcome.errors == [] and outcome.report is not None


def test_one_line_for_two_pieces_matches_two_lines_of_one_piece(db):
    """KIT по API пишет позицию одной строкой на все штуки, CRM — строкой на
    каждую: сумма сравнивается за штуку."""
    account = _kit(db)
    piece = dict(v.empty_line(), operation=v.SALE, quantity=1, revenue=333600, payout=333600,
                 srid="100620", barcode="2000932170695", operation_date=date(2026, 9, 9))
    store_api_report(db, platform="kit", account_id=account.id,
                     parsed=ParseResult(format=fetch.CRM_DB_FORMAT, lines=[dict(piece), dict(piece)]),
                     period_from=WEEK_A[0], period_to=WEEK_A[1], user="crm", source=fetch.CRM_SOURCE,
                     kind=fetch.CRM_KEY)
    pair = dict(piece, quantity=2, revenue=667200, payout=667200)
    outcome = store_api_report(db, platform="kit", account_id=account.id,
                               parsed=ParseResult(format=fetch.KIT_API_FORMAT, lines=[pair]),
                               period_from=WEEK_A[0], period_to=WEEK_A[1], user="api", kind=fetch.KIT_SALES_KEY)
    assert outcome.repeat and "2 шт из 2" in outcome.errors[0]


def _legacy_pair(db):
    """Два отчёта с одними продажами, лёгшие до этой защиты (мимо загрузки)."""
    line = {"operation": v.SALE, "quantity": 1, "revenue": 460000, "payout": 443900,
            "srid": "100608", "barcode": "2000932091174"}
    first = make_report(db, platform="kit", period=(date(2026, 9, 1), date(2026, 9, 30)), key="SR-a",
                        lines=[dict(line)])
    second = make_report(db, platform="kit", period=WEEK_A, key="SR-b", lines=[dict(line)])
    return first, second


def test_repeat_left_from_before_blocks_sending_to_1c(db):
    first, second = _legacy_pair(db)
    problems = upload_problems(db, second, "Склад", check_in_flight=False)
    assert any(f"Те же продажи уже есть в отчётах #{first.id}" in p for p in problems)
    assert repeats.of_report(db, first).reports == [second]


def test_repeat_is_shown_on_the_report_page_and_the_dashboard(client, db):
    first, second = _legacy_pair(db)
    page = client.get(f"/finance/reports/{second.id}").text
    assert "Те же продажи уже есть в отчётах" in page and f"/finance/reports/{first.id}" in page
    dashboard = client.get("/finance?date_from=2026-09-01&date_to=2026-09-30").text
    assert "повтор" in dashboard and "Одни и те же продажи лежат в отчётах" in dashboard


def test_switching_crm_to_kit_api_replaces_rather_than_refuses(db, crm_db):
    """Неотправленные продажи прежнего источника за те же недели заменяются, а
    не считаются повтором — иначе переключиться было бы невозможно."""
    from finapp.finance.settings import save_setting
    from finapp.models import KitVariant
    from tests.test_finance_api_fetch import _cred, _kit_order, _kit_run
    seed(crm_db)
    account = _kit(db)
    _run(db, account)
    save_setting(db, "FIN_KIT_SALES_SOURCE", "api")
    _cred(db, account, "token", "t")
    db.add(KitVariant(account_id=account.id, variant_id="v-1", barcode="2000932091174"))
    db.commit()
    order = _kit_order(order_id="o-1")
    order["order_number"] = 100608
    order["delivery_chunks"][0]["items"][0].update(final_price="4600", quantity=1)
    _kit_run(db, account, [order])
    api = db.query(SalesReport).filter(SalesReport.source == "api").all()
    assert [(r.period_from, r.report_kind) for r in api] == [(WEEK_A[0], None)]
    assert [(r.period_from, r.period_to) for r in _crm_reports(db)] == [WEEK_B]

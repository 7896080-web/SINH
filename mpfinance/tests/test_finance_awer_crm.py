"""Продажи сайта на KIT из AWER CRM (sales-items-<с>_<по>-<выгружен>.xlsx).

Раскладка — как у настоящей выгрузки за 01–30.09.2026: лист «Продажи», колонки
Дата, Номер заказа, Артикул, Баркод, Сумма; пустая строка и «Итого:» под
таблицей; у одной позиции артикул пуст."""
from datetime import date, datetime

from finapp.finance import values as v
from finapp.finance.formats import parse_sheets
from finapp.finance.importer import import_report, period_from_filename
from finapp.finance.readers import read_sheets
from finapp.finance.reconcile import order_check
from finapp.models import SalesReportLine
from tests.finance_factories import make_account, make_product, make_xlsx

HEADERS = ["Дата", "Номер заказа", "Артикул", "Баркод", "Сумма"]
FILENAME = "sales-items-2026-09-01_2026-09-30-2026-10-18_124936.xlsx"
ROWS = [
    [datetime(2026, 9, 5, 12, 14, 37), 100608, "39615MAVIMEL", "2000932091174", 4600],
    [datetime(2026, 9, 9, 19, 6, 30), 100620, "36438 MINT", "2000932170695", 3336],
    [datetime(2026, 9, 9, 19, 6, 30), 100620, "2101 Авер лонгслив коричневый 2XL", "2000932309040", 3500],
    [datetime(2026, 9, 20, 13, 1, 33, 236000), 100716, None, "2000932261218", 6000],
]


def _file(rows=ROWS, total=17436):
    return make_xlsx(HEADERS, rows + [[None] * 5, [None, None, None, "Итого:", total]], sheet="Продажи")


def test_crm_sales_are_read_one_piece_per_row():
    parsed = parse_sheets(read_sheets(_file(), FILENAME), "kit")
    assert parsed.errors == [] and parsed.format.code == "kit_awer_crm"
    assert len(parsed.lines) == 4
    first = parsed.lines[0]
    # Разбор комиссию не знает: до импорта выплата равна сумме.
    assert (first["operation"], first["quantity"], first["revenue"], first["payout"]) == (v.SALE, 1, 460000, 460000)
    # Номер для покупателя — не номер, по которому принимает заказы Sync Admin.
    assert (first["srid"], first["order_id"]) == ("100608", "")
    assert first["barcode"] == "2000932091174" and parsed.lines[3]["article"] == ""
    # «Итого:» — итог СУММ (до комиссии), а не выплаты.
    assert (parsed.declared_revenue, parsed.declared_payout) == (1743600, None)


def test_import_deducts_commission_and_takes_the_period_from_the_file_name(db):
    make_product(db, barcodes=("2000932091174",))
    outcome = import_report(db, platform="kit", file_bytes=_file(), filename=FILENAME, user="admin")
    assert outcome.errors == []
    report = outcome.report
    # Продажи — с 05.09 по 20.09, а выгружен весь сентябрь.
    assert (report.period_from, report.period_to) == (date(2026, 9, 1), date(2026, 9, 30))
    assert report.declared_payout_kop is None
    assert not any("не совпал" in w for w in outcome.warnings)
    lines = db.query(SalesReportLine).filter_by(report_id=report.id).order_by(SalesReportLine.row_no).all()
    # 3,5%: 4600 → 161,00; 3336 → 116,76; 3500 → 122,50; 6000 → 210,00.
    assert [l.commission_kop for l in lines] == [16100, 11676, 12250, 21000]
    assert [l.payout_kop for l in lines] == [443900, 321924, 337750, 579000]
    assert sum(l.revenue_kop for l in lines) == 1743600
    assert any("Комиссия 3,5%" in n and "610,26" in n for n in outcome.notes)
    assert report.unmatched_lines == 3


def test_commission_rate_is_a_setting(db):
    from finapp.finance.settings import save_setting
    save_setting(db, "FIN_KIT_CRM_COMMISSION", "0")
    db.commit()
    report = import_report(db, platform="kit", file_bytes=_file(), filename=FILENAME, user="admin").report
    assert sum(l.payout_kop for l in db.query(SalesReportLine).filter_by(report_id=report.id)) == 1743600


def test_commission_rounds_half_up_and_returns_get_it_back():
    from decimal import Decimal
    from finapp.finance.formats import apply_commission
    lines = [dict(v.empty_line(), operation=v.SALE, revenue=343800),      # 120,33
             dict(v.empty_line(), operation=v.SALE, revenue=10),          # 0,35 → 0
             dict(v.empty_line(), operation=v.SALE, revenue=100),         # 3,5 → 4
             dict(v.empty_line(), operation=v.RETURN, revenue=-460000)]   # −161,00
    assert apply_commission(lines, Decimal("3.5")) == 12033 + 0 + 4 - 16100
    assert [l["payout"] for l in lines] == [331767, 10, 96, -443900]


def test_total_mismatch_is_reported(db):
    outcome = import_report(db, platform="kit", file_bytes=_file(total=17000), filename=FILENAME, user="admin")
    assert any("не совпала со строкой «Итого:»" in w for w in outcome.warnings)


def test_negative_amount_is_a_return():
    rows = ROWS[:1] + [[datetime(2026, 9, 22), 100608, "39615MAVIMEL", "2000932091174", -4600]]
    parsed = parse_sheets(read_sheets(_file(rows, total=0), FILENAME), "kit")
    assert [line["operation"] for line in parsed.lines] == [v.SALE, v.RETURN]
    assert parsed.lines[1]["revenue"] == -460000


def test_period_from_file_name():
    assert period_from_filename(FILENAME) == (date(2026, 9, 1), date(2026, 9, 30))
    assert period_from_filename("sales-items-2026-09-30_2026-09-01.xlsx") is None   # перепутан порядок
    assert period_from_filename("sales-items-2026-02-30_2026-03-01.xlsx") is None   # такой даты нет
    assert period_from_filename("Еженедельный детализированный отчет №848966494_531247.xlsx") is None


def test_order_check_does_not_call_every_order_missing(db, sync_db):
    """Sync Admin хранит заказы KIT по внутренним id; сверка по номеру для
    покупателя назвала бы «нет в отчётах» все принятые заказы месяца."""
    account = make_account(db, platform="kit", name="KIT", sync_account_id=5)
    sync_db.order(5, "a1b2:0:c3d4", at="2026-09-05 09:14:40")
    report = import_report(db, platform="kit", file_bytes=_file(), filename=FILENAME, user="admin",
                           account_id=account.id).report
    check = order_check(db, report)
    assert check.applicable is False and "номера заказов для покупателя" in check.reason

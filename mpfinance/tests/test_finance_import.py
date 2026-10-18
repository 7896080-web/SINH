"""Загрузка отчёта в базу: строгость, дубли, сопоставление с номенклатурой 1С."""
from datetime import date

from finapp.finance.importer import import_report, make_onec_key, resolve_uids
from finapp.models import Barcode, SalesReport, SalesReportLine
from tests.finance_factories import make_account, make_product, wb_file, wb_row


def _import(db, data, **kw):
    params = {"platform": "wb", "file_bytes": data, "filename": "r.xlsx", "user": "admin"}
    params.update(kw)
    return import_report(db, **params)


def test_import_saves_report_with_period_from_dates_and_uid(db):
    make_product(db)
    outcome = _import(db, wb_file([wb_row(**{"Дата продажи": "2026-09-03"}),
                                   wb_row(**{"Дата продажи": "2026-09-06", "Srid": "s2"})]))
    report = outcome.report
    assert outcome.errors == []
    assert (report.period_from, report.period_to) == (date(2026, 9, 3), date(2026, 9, 6))
    assert report.lines_count == 2 and report.unmatched_lines == 0
    assert report.source_format == "wb_detail"
    assert {line.uid_1c for line in report.lines} == {"u1"}


def test_import_is_all_or_nothing(db):
    """Одна битая строка — отчёт не сохраняется целиком: половина отчёта —
    это неверная выручка, которая выглядит правдоподобно."""
    outcome = _import(db, wb_file([wb_row(), wb_row(**{"Хранение": "много"})]))
    assert outcome.report is None
    assert any("строка 3" in e for e in outcome.errors)
    assert db.query(SalesReport).count() == 0
    assert db.query(SalesReportLine).count() == 0


def test_same_file_twice_is_refused(db):
    data = wb_file([wb_row()])
    assert _import(db, data).report is not None
    second = _import(db, data)
    assert second.report is None and "уже загружен" in second.errors[0]


def test_same_report_number_is_refused_even_from_another_file(db):
    assert _import(db, wb_file([wb_row()]), report_number="777").report is not None
    second = _import(db, wb_file([wb_row(**{"Srid": "x"})]), report_number="777")
    assert second.report is None and "номером" in second.errors[0]


def test_account_of_another_platform_is_refused(db):
    ozon = make_account(db, platform="ozon", name="Ozon")
    outcome = _import(db, wb_file([wb_row()]), account_id=ozon.id)
    assert outcome.report is None and "Ozon" in outcome.errors[0]


def test_explicit_period_overrides_and_out_of_period_lines_are_warned(db):
    outcome = _import(db, wb_file([wb_row(**{"Дата продажи": "2026-08-20"})]),
                      period_from=date(2026, 9, 1), period_to=date(2026, 9, 7))
    assert outcome.report.period_from == date(2026, 9, 1)
    assert any("вне периода" in w for w in outcome.warnings)


def test_unmatched_barcodes_are_counted_and_resolved_after_mapping(db):
    outcome = _import(db, wb_file([wb_row(**{"Баркод": "999"})]))
    report = outcome.report
    assert report.unmatched_lines == 1
    assert any("без товара 1С" in w for w in outcome.warnings)

    make_product(db, barcodes=())
    db.add(Barcode(barcode="999", uid_1c="u1"))
    db.commit()
    assert resolve_uids(db, report) == 0
    assert report.lines[0].uid_1c == "u1"


def test_expense_lines_without_barcode_do_not_count_as_unmatched(db):
    storage = wb_row(**{"Тип документа": "", "Обоснование для оплаты": "Хранение", "Кол-во": 0,
                        "Баркод": "", "Вайлдберриз реализовал Товар (Пр)": 0,
                        "К перечислению Продавцу за реализованный Товар": 0, "Хранение": 300})
    outcome = _import(db, wb_file([storage]))
    assert outcome.report.unmatched_lines == 0


def test_overlapping_period_is_warned(db):
    account = make_account(db)
    _import(db, wb_file([wb_row()]), account_id=account.id, report_number="1")
    outcome = _import(db, wb_file([wb_row(**{"Srid": "z"})]), account_id=account.id,
                      report_number="2")
    assert any("пересекается" in w for w in outcome.warnings)


def test_onec_key_is_stable_and_safe_for_the_exchange_file():
    assert make_onec_key("wb", 3, "12345", date(2026, 9, 1), date(2026, 9, 7)) == "SR-wb-12345"
    assert make_onec_key("wb", 3, "№ 12|34", date(2026, 9, 1), date(2026, 9, 7)) == "SR-wb-1234"
    assert make_onec_key("lamoda", None, "", date(2026, 9, 1), date(2026, 9, 30)) == \
        "SR-lamoda-0-20260901-20260930"

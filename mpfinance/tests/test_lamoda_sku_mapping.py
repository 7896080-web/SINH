"""Lamoda без EAN — по артикулу Lamoda в «Мэппинге» Sync Admin.

В отчёте комиссионера EAN почти у всех строк; у остальных есть артикул Lamoda
(«MP002XM0WFWSIN3XL»), и он записан в «Мэппинг». В еженедельном отчёте EAN нет
вовсе — только короткий артикул модели и размер партнёра."""
from datetime import date

from finapp.finance import values as v
from finapp.finance.importer import lamoda_sku_keys, remap_unmatched, resolve_uids
from finapp.models import OneCUploadStatus
from tests.finance_factories import make_report


def _lamoda(db, key, lines, period=(date(2026, 10, 5), date(2026, 10, 11))):
    return make_report(db, platform="lamoda", period=period, key=key, lines=lines)


def test_keys_full_article_as_is_short_one_only_with_size():
    assert lamoda_sku_keys("MP002XM0WFWSIN3XL", None) == ["MP002XM0WFWSIN3XL"]
    assert lamoda_sku_keys("MP002XM22QRZ", "4XL") == ["MP002XM22QRZIN4XL"]
    assert lamoda_sku_keys("MP002XM22QRZ", "") == []          # модель целиком — не тот размер
    assert lamoda_sku_keys("", "XL") == []


def test_commissioner_line_without_ean_is_found_by_lamoda_article(db, sync_db):
    sync_db.product("u-jacket", article="C24-2317CQ", name="Куртка", size="56")
    sync_db.barcode("MP002XM0WFWSIN3XL", "u-jacket", source="excel")
    report = _lamoda(db, "SR-l1", [
        {"operation": v.SALE, "revenue": 1460000, "payout": 846800, "barcode": None,
         "platform_sku": "MP002XM0WFWSIN3XL", "name": "C24-2317CQ Авер Куртка 56 темно-синий"}])
    notes = []
    assert resolve_uids(db, report, notes) == 0
    assert report.lines[0].uid_1c == "u-jacket"
    assert any("По артикулу Lamoda" in n for n in notes)


def test_weekly_line_is_found_by_short_article_and_partner_size(db, sync_db):
    sync_db.product("u-shirt", article="32481", name="Рубашка", size="4XL")
    sync_db.barcode("MP002XM22QRZIN4XL", "u-shirt", source="excel")
    report = _lamoda(db, "SR-l2", [
        {"operation": v.SALE, "revenue": 530000, "payout": 307400, "barcode": None,
         "platform_sku": "MP002XM22QRZ", "size": "4XL", "name": "32481MINT 4XL Рубашка AWER"}])
    assert resolve_uids(db, report) == 0 and report.lines[0].uid_1c == "u-shirt"


def test_hourly_remap_picks_up_mapping_added_later_but_leaves_sent_reports(db, sync_db):
    line = {"operation": v.SALE, "revenue": 530000, "payout": 307400, "barcode": None,
            "platform_sku": "MP002XM22QRZ", "size": "4XL"}
    open_report = _lamoda(db, "SR-l3", [dict(line)])
    sent = _lamoda(db, "SR-l4", [dict(line)], period=(date(2026, 9, 28), date(2026, 10, 4)))
    for report in (open_report, sent):
        resolve_uids(db, report)
    sent.onec_status = OneCUploadStatus.done
    db.commit()
    assert open_report.unmatched_lines == 1
    sync_db.product("u-shirt", article="32481", name="Рубашка", size="4XL")
    sync_db.barcode("MP002XM22QRZIN4XL", "u-shirt", source="excel")
    assert remap_unmatched(db) == (1, 1)
    db.commit()
    assert open_report.unmatched_lines == 0 and sent.unmatched_lines == 1


def test_daily_job_runs_the_remap(db, sync_db):
    from finapp.workers import jobs
    report = _lamoda(db, "SR-l5", [{"operation": v.SALE, "revenue": 1, "payout": 1, "barcode": None,
                                    "platform_sku": "MP002XM0WFWSIN3XL"}])
    resolve_uids(db, report)
    db.commit()
    sync_db.product("u-jacket", article="C24-2317CQ", name="Куртка", size="56")
    sync_db.barcode("MP002XM0WFWSIN3XL", "u-jacket", source="excel")
    jobs.job_daily()
    db.expire_all()
    assert report.unmatched_lines == 0

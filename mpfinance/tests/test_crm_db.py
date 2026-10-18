"""Продажи сайта на KIT прямо из базы AWER CRM (`finance/sources/crm.py`,
заявка `crm_db` в `fetch.py`).

Правило восстановлено по настоящей базе CRM и сверено с её выгрузками за
июль–сентябрь 2026 года до строки и рубля. Здесь оно закреплено на поддельной
базе той же формы (`FakeCrm` в conftest): что считается продажей, какой датой,
куда ложится неделя, что делается с уже отправленным в 1С — и что загрузка
не трогает доставку KIT и не задваивается с продажами из API KIT."""
import hashlib
from datetime import date, datetime

import pytest

from finapp import config
from finapp.finance import fetch, values as v
from finapp.finance.formats import ParseResult
from finapp.finance.importer import store_api_report
from finapp.finance.settings import save_setting
from finapp.finance.sources import crm
from finapp.models import KitVariant, OneCUploadStatus, ReportFetch, SalesReport, SalesReportLine
from finapp.timeutils import local_date_of
from tests.finance_factories import make_account, make_product
from tests.test_finance_api_fetch import _cred, _kit_order, _kit_run

WEEK_A = (date(2026, 9, 7), date(2026, 9, 13))
WEEK_B = (date(2026, 9, 14), date(2026, 9, 20))


def day(moment: str) -> date:
    """Местное число момента — тем же правилом, что и код (сервер в Москве,
    машина разработки в UTC)."""
    return local_date_of(datetime.fromisoformat(moment))


def seed(crm_db):
    # Обычная продажа: дата — вручение.
    crm_db.order(100608, [("2026-09-08T12:14:37+03:00", [("v1", 4600, 1, 0, "39615MAVIMEL", "2000932091174")])])
    # Две штуки, одна отказана при получении; вторая позиция отказана целиком.
    crm_db.order(100620, [("2026-09-09T19:06:30+03:00", [("v2", 3336, 2, 1, "36438 MINT", "2000932170695"),
                                                          ("v3", 3500, 1, 1, "2101 лонгслив", "2000932309040")])])
    # Отменённый заказ — не продажа, даже с датой вручения.
    crm_db.order(100700, [("2026-09-09T12:00:00+03:00", [("v1", 4600, 1, 0, "39615MAVIMEL", "2000932091174")])],
                 status="CANCELLED")
    # Вручения нет — дата выполнения по истории CRM (UTC), а не время изменения в KIT.
    crm_db.order(100589, [(None, [("v5", 7340, 1, 0, "86321 Даунтлесс", "2000932261188")])],
                 kit_updated_at="2026-09-02T09:26:09+03:00", completed_at="2026-09-15 10:11:13.562000")
    # Ни вручения, ни истории — время изменения в KIT; время записи CRM
    # (пересинхронизация в октябре) не в счёт.
    crm_db.order(100079, [(None, [("v6", 2843, 1, 0, "8419", "2000931919769")])],
                 kit_updated_at="2026-09-10T10:47:30+03:00", crm_updated_at="2026-10-15 15:00:33")
    # Баркода в позиции заказа нет — берётся из кэша товаров CRM.
    crm_db.cached("v4", "55045 6089", "2000932206462")
    crm_db.order(100716, [("2026-09-16T14:00:00+03:00", [("v4", 4100, 1, 0, "55045 6089", None)])])


def test_sales_rule(crm_db):
    seed(crm_db)
    sales, stats = crm.sales_between(WEEK_A[0], WEEK_B[1])
    got = {(s.order_number, s.barcode, s.amount_kop, s.day, s.dated_by) for s in sales}
    assert got == {
        ("100608", "2000932091174", 460000, day("2026-09-08T12:14:37+03:00"), crm.DELIVERED),
        ("100620", "2000932170695", 333600, day("2026-09-09T19:06:30+03:00"), crm.DELIVERED),
        ("100589", "2000932261188", 734000, local_date_of(datetime(2026, 9, 15, 10, 11, 13)), crm.HISTORY),
        ("100079", "2000931919769", 284300, day("2026-09-10T10:47:30+03:00"), crm.KIT),
        ("100716", "2000932206462", 410000, day("2026-09-16T14:00:00+03:00"), crm.DELIVERED),
    }
    assert len(sales) == 5                                    # по строке на штуку
    assert (stats["pieces"], stats["refused"], stats[crm.HISTORY], stats[crm.KIT]) == (5, 2, 1, 1)
    assert {s.article for s in sales if s.order_number == "100716"} == {"55045 6089"}


def test_resync_of_crm_does_not_move_a_sale_to_another_month(crm_db):
    """CRM датирует заказ без вручения и истории временем СВОЕЙ записи — оно
    меняется при каждой пересинхронизации, и продажа переезжает в другой месяц
    (сентябрь от 30.09 — 367 714 ₽, тот же сентябрь от 18.10 — 309 950 ₽)."""
    seed(crm_db)
    assert crm.sales_between(date(2026, 10, 1), date(2026, 10, 31))[0] == []
    sold = [s for s in crm.sales_between(WEEK_A[0], WEEK_A[1])[0] if s.order_number == "100079"]
    assert len(sold) == 1


def test_crm_base_is_opened_read_only(crm_db):
    seed(crm_db)
    with pytest.raises(crm.CrmDbError):
        with crm._connect() as conn:
            conn.execute("DELETE FROM orders")


def test_changed_crm_schema_is_named_not_silently_empty(crm_db):
    import sqlite3
    conn = sqlite3.connect(config.CRM_DB_PATH)
    conn.execute("ALTER TABLE orders RENAME COLUMN raw_payload TO payload")
    conn.commit()
    conn.close()
    with pytest.raises(crm.CrmDbError, match="raw_payload"):
        crm.sales_between(*WEEK_A)


# ------------------------------------------------------------ загрузка

def _kit(db):
    return make_account(db, platform="kit", name="Сайт AWER")


def _run(db, account, period=(WEEK_A[0], WEEK_B[1])) -> ReportFetch:
    job, error = fetch.request_fetch(db, "crm_db", "admin", account.id, *period)
    assert job is not None, error
    db.commit()
    fetch.run_pending(db)
    db.refresh(job)
    return job


def _crm_reports(db):
    return db.query(SalesReport).filter(SalesReport.source == fetch.CRM_SOURCE) \
        .order_by(SalesReport.period_from).all()


def test_fetch_lays_weekly_reports_with_commission(db, crm_db):
    seed(crm_db)
    make_product(db, barcodes=("2000932091174",))
    account = _kit(db)
    job = _run(db, account)
    assert job.status == "done", job.message
    reports = _crm_reports(db)
    assert [(r.period_from, r.period_to) for r in reports] == [WEEK_A, WEEK_B]
    assert {r.source_format for r in reports} == {fetch.CRM_DB_FORMAT.code}
    week_a = db.query(SalesReportLine).filter_by(report_id=reports[0].id).order_by(SalesReportLine.revenue_kop).all()
    # 3,5%: 2843 → 99,51 (половина вверх); 3336 → 116,76; 4600 → 161,00.
    assert [(l.revenue_kop, l.commission_kop, l.payout_kop) for l in week_a] == \
        [(284300, 9951, 274349), (333600, 11676, 321924), (460000, 16100, 443900)]
    assert {l.srid for l in week_a} == {"100608", "100620", "100079"}
    assert "комиссия 3,5%" in job.message and "без даты вручения в KIT — 2 шт" in job.message
    assert job.imported == 2


def test_refetch_replaces_unsent_week_and_names_drift_of_sent_one(db, crm_db):
    seed(crm_db)
    account = _kit(db)
    _run(db, account)
    week_a, week_b = _crm_reports(db)
    week_a.onec_status = OneCUploadStatus.done
    db.commit()
    # CRM узнала о вручениях позже: по неделе в 1С и по неотправленной.
    crm_db.order(100750, [("2026-09-11T12:00:00+03:00", [("v7", 5000, 1, 0, "X", "2000000000777")])])
    crm_db.order(100751, [("2026-09-17T12:00:00+03:00", [("v8", 6000, 1, 0, "Y", "2000000000888")])])
    job = _run(db, account)
    week_a2, week_b2 = _crm_reports(db)
    assert week_a2.id == week_a.id and week_a2.lines_count == 3           # отправленный не тронут
    assert "уже в 1С (3 шт" in job.message and "в CRM теперь 4 шт" in job.message
    assert week_b2.lines_count == 3                                        # неотправленный заменён


def test_week_whose_sales_fell_away_loses_its_unsent_report(db, crm_db):
    seed(crm_db)
    account = _kit(db)
    _run(db, account)
    crm_db.status(100589, "CANCELLED")
    crm_db.status(100716, "CANCELLED")
    job = _run(db, account)
    assert [(r.period_from, r.period_to) for r in _crm_reports(db)] == [WEEK_A]
    assert "продаж в CRM больше нет" in job.message


def test_crm_run_keeps_kit_delivery_and_drops_unsent_api_sales(db, crm_db):
    """Доставку KIT по API продажи из CRM не заменяют. А продажи из API KIT за те
    же недели (переключили API → CRM) — те же продажи, второй раз им не место."""
    seed(crm_db)
    account = _kit(db)
    logistics = dict(v.empty_line(), operation=v.LOGISTICS, logistics=28670, operation_date=date(2026, 9, 8))
    store_api_report(db, platform="kit", account_id=account.id,
                     parsed=ParseResult(format=fetch.KIT_API_FORMAT, lines=[logistics]),
                     period_from=WEEK_A[0], period_to=WEEK_A[1], user="api", replace_unsent=True,
                     report_kind=fetch.KIT_DELIVERY_KIND, kind=fetch.KIT_DELIVERY_KEY)
    sale = dict(v.empty_line(), operation=v.SALE, quantity=1, revenue=460000, payout=460000,
                barcode="2000932091174", operation_date=date(2026, 9, 8))
    store_api_report(db, platform="kit", account_id=account.id,
                     parsed=ParseResult(format=fetch.KIT_API_FORMAT, lines=[sale]),
                     period_from=WEEK_A[0], period_to=WEEK_A[1], user="api", replace_unsent=True,
                     kind=fetch.KIT_SALES_KEY)
    job = _run(db, account)
    api = db.query(SalesReport).filter(SalesReport.source == "api").all()
    assert [r.report_kind for r in api] == [fetch.KIT_DELIVERY_KIND]
    assert "убраны неотправленные продажи из API KIT за эти недели: 1" in job.message


def test_kit_delivery_fetch_keeps_crm_sales_and_api_sales_mode_replaces_them(db, crm_db):
    seed(crm_db)
    account = _kit(db)
    _cred(db, account, "token", "t")
    _run(db, account)
    _kit_run(db, account, [_kit_order()])                     # продажи из CRM: KIT — только доставка
    assert len(_crm_reports(db)) == 2
    save_setting(db, "FIN_KIT_SALES_SOURCE", "api")
    db.add(KitVariant(account_id=account.id, variant_id="v-1", barcode="2000000000012"))
    db.commit()
    _kit_run(db, account, [_kit_order()])                     # продажи из API за неделю 07–13.09
    assert [(r.period_from, r.period_to) for r in _crm_reports(db)] == [WEEK_B]


def test_request_is_refused_while_sales_come_from_kit_api(db, crm_db):
    account = _kit(db)
    save_setting(db, "FIN_KIT_SALES_SOURCE", "api")
    db.commit()
    job, error = fetch.request_fetch(db, "crm_db", "admin", account.id, *WEEK_A)
    assert job is None and "легли бы поверх них второй раз" in error


def test_request_without_crm_base_names_the_file(db):
    account = _kit(db)
    job, error = fetch.request_fetch(db, "crm_db", "admin", account.id, *WEEK_A)
    assert job is None and "kitcrm.db" in error


def test_crm_base_unreadable_fails_the_job_with_a_reason(db, crm_db):
    seed(crm_db)
    account = _kit(db)
    job, _ = fetch.request_fetch(db, "crm_db", "admin", account.id, *WEEK_A)
    db.commit()
    open(config.CRM_DB_PATH, "wb").write(b"not a database")
    fetch.run_pending(db)
    db.refresh(job)
    assert job.status == "failed" and "AWER CRM" in job.message


def test_fetch_does_not_write_to_crm_base(db, crm_db):
    seed(crm_db)
    before = hashlib.sha256(open(config.CRM_DB_PATH, "rb").read()).hexdigest()
    _run(db, _kit(db))
    assert hashlib.sha256(open(config.CRM_DB_PATH, "rb").read()).hexdigest() == before


# ------------------------------------------------------------ автозагрузка и видимость

def test_daily_plan_takes_four_full_weeks_from_crm(db, crm_db):
    account = _kit(db)
    fetch.plan_regular(db)
    job = db.query(ReportFetch).filter(ReportFetch.kind == "crm_db").one()
    assert job.account_id == account.id
    assert job.date_from.weekday() == 0 and job.date_to.weekday() == 6
    assert (job.date_to - job.date_from).days == 7 * fetch.CRM_AUTO_WEEKS - 1
    fetch.plan_regular(db)
    assert db.query(ReportFetch).filter(ReportFetch.kind == "crm_db").count() == 1   # раз в сутки


def test_daily_plan_does_not_guess_between_two_kit_accounts(db, crm_db):
    _kit(db)
    make_account(db, platform="kit", name="Второй магазин")
    fetch.plan_regular(db)
    assert db.query(ReportFetch).filter(ReportFetch.kind == "crm_db").count() == 0
    crm_row = [f for f in fetch.account_freshness(db) if f["source"] == "crm"]
    assert crm_row and "кабинетов KIT несколько" in crm_row[0]["problem"]


def test_daily_plan_skips_crm_without_base_or_with_api_sales(db):
    _kit(db)
    fetch.plan_regular(db)
    assert db.query(ReportFetch).filter(ReportFetch.kind == "crm_db").count() == 0
    crm_row = [f for f in fetch.account_freshness(db) if f["source"] == "crm"]
    assert crm_row and crm_row[0]["stale"] and "kitcrm.db" in crm_row[0]["problem"]
    save_setting(db, "FIN_KIT_SALES_SOURCE", "api")
    db.commit()
    assert not [f for f in fetch.account_freshness(db) if f["source"] == "crm"]


def test_freshness_shows_last_crm_load(db, crm_db):
    seed(crm_db)
    _run(db, _kit(db))
    row = [f for f in fetch.account_freshness(db) if f["source"] == "crm"][0]
    assert row["last_ok"] is not None and not row["stale"] and row["label"] == "продажи сайта, база AWER CRM"


def test_pages_offer_and_describe_crm_load(client, db, crm_db):
    account = _kit(db)
    page = client.get("/finance/reports").text
    assert "Забрать из CRM" in page and 'value="crm_db"' in page
    assert "База AWER CRM (продажи сайта)" in client.get("/diagnostics").text
    response = client.post("/finance/fetch", data={"kind": "crm_db", "account_id": str(account.id),
                                                   "date_from": "2026-09-07", "date_to": "2026-09-20"},
                           follow_redirects=True)
    assert "AWER CRM — продажи сайта (база CRM)" in response.text


def test_fixtures_refuse_to_delete_anything_outside_the_test_folder(tmp_path):
    """Тесты идут на боевом сервере при каждом накате, рядом с живыми базами
    sync_admin и AWER CRM: удалить фикстура вправе только свой временный файл."""
    from tests.conftest import _test_file
    for path in (tmp_path / "kitcrm.db", r"C:\awer_crm_data\kitcrm.db", r"C:\sync_admin\sync_admin.db"):
        with pytest.raises(RuntimeError, match="ничего не тронуто"):
            _test_file(str(path))
    assert _test_file(config.CRM_DB_PATH).name == "kitcrm.db"

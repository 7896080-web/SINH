"""Сверка отчёта: деньги, заказы Sync Admin (его база, только чтение), товары."""
from datetime import date

from finapp.finance import values as v
from finapp.finance.reconcile import breakdown, money_check, order_check, unmatched_barcodes
from finapp.timeutils import local_day_start_utc
from tests.finance_factories import make_account, make_report

PERIOD = (date(2026, 9, 1), date(2026, 9, 7))


def _sale(order_id, **kw):
    return {"operation": v.SALE, "order_id": order_id, "barcode": "111", "uid_1c": "u1",
            "quantity": 1, "revenue": 100000, "payout": 80000, "commission": 20000, **kw}


SYNC_ID = 7      # тот же кабинет в Sync Admin


def test_breakdown_and_money_check(db):
    report = make_report(db, period=PERIOD, lines=[
        _sale("1"), {"operation": v.STORAGE, "quantity": 0, "storage": 5000}])
    rows, total = breakdown(db, report)
    assert [r.operation for r in rows] == [v.SALE, v.STORAGE]
    assert total.total_payout == 75000

    report.declared_payout_kop = 75000
    report.bank_payout_kop = 70000
    check = money_check(report, total)
    assert check.declared_diff == 0
    assert check.bank_diff == -5000          # банк считается от суммы площадки


def test_order_check_needs_an_account(db):
    report = make_report(db, period=PERIOD, lines=[_sale("1")])
    assert order_check(db, report).applicable is False


def test_order_check_needs_the_sync_admin_account(db):
    account = make_account(db)
    report = make_report(db, account_id=account.id, period=PERIOD, lines=[_sale("1")])
    check = order_check(db, report)
    assert check.applicable is False and "Sync Admin" in check.reason


def test_order_check_without_sync_admin_database_says_so(db):
    account = make_account(db, sync_account_id=SYNC_ID)
    report = make_report(db, account_id=account.id, period=PERIOD, lines=[_sale("1")])
    check = order_check(db, report)
    assert check.applicable is False and "недоступна" in check.reason


def test_order_check_finds_every_kind_of_mismatch(db, sync_db):
    account = make_account(db, sync_account_id=SYNC_ID)
    sync_db.order(SYNC_ID, "1")                                         # совпал
    sync_db.order(SYNC_ID, "2", status="cancelled")                     # отменён, а продан
    sync_db.order(SYNC_ID, "5")                                         # нет ни в одном отчёте
    sync_db.order(SYNC_ID, "6")                                         # есть в соседнем отчёте
    sync_db.order(SYNC_ID, "TEST-7")                                    # тестовый — не в счёт
    sync_db.order(SYNC_ID, "8", at="2026-08-01 12:00:00")               # вне периода
    sync_db.order(99, "9")                                              # другой кабинет
    make_report(db, account_id=account.id, period=(date(2026, 9, 8), date(2026, 9, 14)),
                key="SR-next", lines=[_sale("6")])
    report = make_report(db, account_id=account.id, period=PERIOD, lines=[
        _sale("1"), _sale("2"), _sale("3"), _sale(""),                 # «3» — нет в Sync Admin, «» — FBO
    ])

    check = order_check(db, report)
    assert check.applicable, check.reason
    assert check.report_orders == 3 and check.matched == 2
    assert check.without_order_id == 1
    assert [line.order_id for line in check.not_in_system] == ["3"]
    assert [order.order_id for order, _ in check.cancelled_in_system] == ["2"]
    assert [o.order_id for o in check.missing_in_reports] == ["5"]


def test_order_period_is_local_days(db, sync_db):
    """Заказ, принятый в первые минуты местных суток 1 сентября, — в периоде
    1–7.09, хотя по UTC это ещё 31 августа (на сервере в Москве)."""
    account = make_account(db, sync_account_id=SYNC_ID)
    first_minute = local_day_start_utc(date(2026, 9, 1)).replace(tzinfo=None)
    sync_db.order(SYNC_ID, "early", at=first_minute.isoformat(sep=" "))
    report = make_report(db, account_id=account.id, period=PERIOD, lines=[_sale("1")])
    assert [o.order_id for o in order_check(db, report).missing_in_reports] == ["early"]


def test_unmatched_barcodes_are_rolled_up(db):
    report = make_report(db, period=PERIOD, lines=[
        _sale("1", uid_1c=None, barcode="999"), _sale("2", uid_1c=None, barcode="999"),
        {"operation": v.RETURN, "uid_1c": None, "barcode": "999", "quantity": 1, "revenue": -100000},
        _sale("3"),
    ])
    rows = unmatched_barcodes(db, report)
    assert len(rows) == 1
    assert rows[0]["barcode"] == "999"
    assert (rows[0]["lines"], rows[0]["quantity"], rows[0]["revenue"]) == (3, 1, 100000)

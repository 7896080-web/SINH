"""Официальные отчёты площадок по API: WB finance-api и заказы KIT.

Сеть подменяется фальшивой сессией, которая отвечает ровно в форме
официальной спецификации (WB `13-finances.yaml`, OpenAPI KIT). Проверяется
наша сторона: какие запросы уходят, как соблюдается лимит WB, как ответ
ложится в отчёт и сверку, и что повторная загрузка не задваивает продажи.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from finapp.crypto import encrypt_value
from finapp.finance import fetch, values as v
from finapp.finance.sources import kit as kit_source
from finapp.finance.sources import wb as wb_source
from finapp.models import ApiCredential, KitVariant, OneCUploadStatus, ReportFetch, SalesReport, SalesReportLine
from finapp.timeutils import local_date_of, local_day_start_utc, now_utc
from tests.finance_factories import make_account, make_product


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload
        self.content = b"" if payload is None else b"x"
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeSession:
    """Отвечает по пути запроса; запоминает, что спрашивали."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def _answer(self, method, url, payload):
        path = url.split(".ru", 1)[-1].split(".net", 1)[-1]
        self.calls.append((method, path, payload))
        handler = self.routes[path] if path in self.routes else self.routes.get("*")
        result = handler(payload) if callable(handler) else handler
        return result if isinstance(result, FakeResponse) else FakeResponse(200, result)

    def post(self, url, json=None, timeout=None):
        return self._answer("POST", url, json)

    def get(self, url, params=None, timeout=None):
        return self._answer("GET", url, params)


# ------------------------------------------------------------ WB

WB_HEADER = {"reportId": 307401554, "dateFrom": "2026-09-07", "dateTo": "2026-09-13",
             "createDate": "2026-09-14", "currency": "RUB", "reportType": 1,
             "retailAmountSum": "2500", "forPaySum": "2100", "deliveryServiceSum": "150.5",
             "bankPaymentSum": "1949.50"}


def _wb_line(**kw):
    line = {"rrdId": 1, "reportId": 307401554, "docTypeName": "Продажа",
            "sellerOperName": "Продажа", "saleDt": "2026-09-08T12:00:00", "quantity": 1,
            "retailAmount": "2500", "forPay": "2100", "deliveryService": "0", "sku": "2000000000012",
            "vendorCode": "A-1", "nmId": 123, "title": "Джинсы", "techSize": "46", "orderId": 555,
            "srid": "s1"}
    line.update(kw)
    return line


def test_wb_client_waits_a_minute_between_calls_and_pages_by_rrd_id():
    pauses = []
    pages = [[_wb_line(rrdId=i) for i in range(1, 4)], [_wb_line(rrdId=4)]]
    session = FakeSession({
        wb_source.LIST_PATH: [WB_HEADER],
        wb_source.DETAIL_PATH.format(report_id=307401554): lambda body: pages[0] if body["rrdId"] == 0 else pages[1],
    })
    api = wb_source.WbFinanceApi("tok", session=session, sleep=pauses.append)
    assert api.list_reports(date(2026, 9, 1), date(2026, 9, 30)) == [WB_HEADER]
    original_limit = wb_source.PAGE_LIMIT
    wb_source.PAGE_LIMIT = 3
    try:
        lines = api.report_lines(307401554)
    finally:
        wb_source.PAGE_LIMIT = original_limit
    assert len(lines) == 4
    # rrdId следующей страницы — из последней строки предыдущей.
    assert [c[2].get("rrdId") for c in session.calls[1:]] == [0, 3]
    # Лимит WB — запрос в минуту на профиль: пауза перед каждым, кроме первого.
    assert pauses == [wb_source.PAUSE_SECONDS] * 2
    assert session.headers["Authorization"] == "tok"            # без «Bearer»


def test_wb_token_without_finance_category_is_explained():
    session = FakeSession({"*": FakeResponse(403, {"detail": "scope is not allowed for this resource"})})
    api = wb_source.WbFinanceApi("tok", session=session, sleep=lambda s: None)
    with pytest.raises(wb_source.WbFinanceError, match="Финансы"):
        api.list_reports(date(2026, 9, 1), date(2026, 9, 30))


def _cred(db, account, field, value):
    db.add(ApiCredential(account_id=account.id, field_name=field, encrypted_value=encrypt_value(value)))
    db.commit()


def _wb_session(lines):
    return FakeSession({
        wb_source.LIST_PATH: [WB_HEADER],
        wb_source.DETAIL_PATH.format(report_id=307401554): lines,
    })


def test_wb_fetch_imports_official_report_with_bank_payment_for_reconciliation(db):
    make_product(db)
    account = make_account(db)
    _cred(db, account, "finance_token", "fin")
    job, error = fetch.request_fetch(db, "wb_api", "admin", account.id,
                                     date(2026, 9, 1), date(2026, 9, 30))
    db.commit()
    session = _wb_session([_wb_line(), _wb_line(rrdId=2, sellerOperName="Логистика",
                                                docTypeName="", quantity=0, retailAmount="0",
                                                forPay="0", deliveryService="150.5")])
    stats = fetch.run_pending(db, session=session, sleep=lambda s: None)

    assert stats == {"jobs": 1, "imported": 1, "failed": 0}
    db.refresh(job)
    assert job.status == "done" and job.found == 1 and job.imported == 1
    assert session.headers["Authorization"] == "fin"              # токен «Финансы», без «Bearer»
    report = db.query(SalesReport).one()
    assert (report.source, report.report_number, report.report_kind) == ("api", "307401554", "основной")
    assert (report.period_from, report.period_to) == (date(2026, 9, 7), date(2026, 9, 13))
    assert report.onec_key == "SR-wb-307401554"
    # «Итого к оплате» WB ложится суммой площадки — и совпадает с итогом строк.
    assert report.declared_payout_kop == 194950
    assert sum(line.total_kop for line in report.lines) == 194950
    assert report.unmatched_lines == 0


def test_wb_already_imported_report_is_not_requested_again(db):
    account = make_account(db)
    _cred(db, account, "finance_token", "main")
    for _ in range(2):
        fetch.request_fetch(db, "wb_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 30))
        db.commit()
        session = _wb_session([_wb_line()])
        fetch.run_pending(db, session=session, sleep=lambda s: None)
    assert db.query(SalesReport).count() == 1
    # Второй проход спросил только список — детализация стоит минуту.
    assert [c[1] for c in session.calls] == [wb_source.LIST_PATH]
    last = db.query(ReportFetch).order_by(ReportFetch.id.desc()).first()
    assert "уже были: 1" in last.message


def test_wb_failure_is_recorded_on_the_job_not_raised(db):
    account = make_account(db)
    _cred(db, account, "finance_token", "main")
    fetch.request_fetch(db, "wb_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 30))
    db.commit()
    session = FakeSession({"*": FakeResponse(401, {})})
    stats = fetch.run_pending(db, session=session, sleep=lambda s: None)
    job = db.query(ReportFetch).one()
    assert stats["failed"] == 1 and job.status == "failed" and "401" in job.message


def test_second_request_while_one_is_running_is_refused(db):
    account = make_account(db)
    first, _ = fetch.request_fetch(db, "wb_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 7))
    second, error = fetch.request_fetch(db, "wb_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 7))
    assert first is not None and second is None and "уже идёт" in error


def test_wb_period_is_clipped_to_api_availability(db):
    account = make_account(db)
    job, _ = fetch.request_fetch(db, "wb_api", "admin", account.id, date(2023, 1, 1), date(2024, 3, 1))
    assert job.date_from == wb_source.FIRST_AVAILABLE


def test_stale_running_job_is_picked_up_again(db):
    account = make_account(db)
    _cred(db, account, "finance_token", "main")
    job, _ = fetch.request_fetch(db, "wb_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 30))
    job.status = "running"
    job.progress_at = now_utc() - timedelta(minutes=fetch.STALE_MINUTES + 1)
    db.commit()
    fetch.run_pending(db, session=_wb_session([_wb_line()]), sleep=lambda s: None)
    db.refresh(job)
    assert job.status == "done"


# ------------------------------------------------------------ KIT

def _kit_order(status="DELIVERED", delivered="2026-09-08T10:00:00+03:00", refused=0, quantity=2,
               seller_costs="250.00", customer_costs="199.00", order_id="o-1"):
    return {
        "id": order_id, "order_number": 1001, "status": status,
        "created_at": "2026-09-05T09:00:00+03:00", "updated_at": "2026-09-09T09:00:00+03:00",
        "delivery_chunks": [{
            "id": 1, "total_price": "6000", "total_final_price": "5000", "purchased_price": "5000",
            "delivery_info": {"delivered_at": delivered, "seller_costs": seller_costs,
                              "customer_costs": customer_costs, "raw_status": "x", "human_status": "x"},
            "items": [{"id": "i-1", "product_variant_id": "v-1", "name": "Джинсы, 46",
                       "quantity": quantity, "price": "3000", "final_price": "2500",
                       "refused_count": refused}],
        }],
    }


def test_kit_lines_count_delivered_minus_refused_and_delivery_costs():
    orders = [
        _kit_order(refused=1),                                            # продано 1 из 2
        _kit_order(order_id="o-2", status="FULL_REFUND", delivered=None),  # полный отказ
        _kit_order(order_id="o-3", status="CANCELLED"),                    # отмена — мимо
        _kit_order(order_id="o-4", delivered="2026-10-02T10:00:00+03:00"), # вне периода
        _kit_order(order_id="o-5", status="WAIT_FOR_DELIVERY", delivered=None),
    ]
    lines, stats = kit_source.build_lines(orders, date(2026, 9, 1), date(2026, 9, 30),
                                          lambda variant: "2000000000012")
    sales = [l for l in lines if l["operation"] == v.SALE]
    assert len(sales) == 1
    sale = sales[0]
    assert (sale["quantity"], sale["revenue"], sale["payout"], sale["commission"]) == (1, 250000, 250000, 0)
    # Номер — тот же, что записывает опрос заказов KIT.
    assert sale["order_id"] == "o-1:1:i-1"
    assert sale["operation_date"] == date(2026, 9, 8)
    delivery = [l for l in lines if l["operation"] == v.LOGISTICS]
    assert [(l["logistics"], l["compensation"]) for l in delivery] == [(25000, 19900), (25000, 19900)]
    assert stats["refused_qty"] == 1 + 2
    assert stats["sold_qty"] == 1


def test_kit_delivery_date_is_the_local_day():
    """Доставка 30.09 в 22:30 UTC — по Москве это уже 1 октября. День считает
    то же правило, что и код (`local_date_of`), а не тест сам: на сервере пояс
    московский, здесь — UTC (см. CLAUDE.md)."""
    moment = datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)
    lines, _ = kit_source.build_lines([_kit_order(delivered="2026-09-30T22:30:00Z")],
                                      date(2026, 9, 1), date(2026, 9, 30), lambda v: "1")
    sales = [l["operation_date"] for l in lines if l["operation"] == v.SALE]
    expected = local_date_of(moment)
    assert sales == ([expected] if expected.month == 9 else [])


@pytest.fixture
def kit_sales_from_api(db):
    """Продажи KIT — из API (по умолчанию они из AWER CRM, а по API — доставка)."""
    from finapp.finance.settings import save_setting
    save_setting(db, "FIN_KIT_SALES_SOURCE", "api")
    db.commit()


def test_kit_fetch_builds_report_and_refreshes_it_until_sent_to_1c(db, kit_sales_from_api):
    make_product(db)
    account = make_account(db, platform="kit", name="Сайт")
    _cred(db, account, "token", "kit-token")
    db.add(KitVariant(account_id=account.id, variant_id="v-1", barcode="2000000000012"))
    db.commit()

    def run(orders):
        fetch.request_fetch(db, "kit_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 30))
        db.commit()
        session = FakeSession({"/v1/orders": {"orders": orders, "total_count": len(orders)}})
        fetch.run_pending(db, session=session, sleep=lambda s: None)
        return session

    session = run([_kit_order()])
    report = db.query(SalesReport).one()
    assert (report.platform, report.source, report.unmatched_lines) == ("kit", "api", 0)
    # Отчёт — на календарную неделю доставки (08.09 — неделя 07–13.09).
    assert (report.period_from, report.period_to) == (date(2026, 9, 7), date(2026, 9, 13))
    assert session.headers["Authorization"] == "Bearer kit-token"
    # Запрос — по дате изменения от понедельника первой недели, по возрастанию.
    params = session.calls[0][2]
    start = local_day_start_utc(date(2026, 8, 31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert (params["sort_by"], params["sort_direction"], params["updated_from"]) == \
        ("updated_at", "asc", start)

    # Отказ доехал позже — отчёт перезабирается и заменяется, пока не ушёл в 1С.
    run([_kit_order(refused=1)])
    report = db.query(SalesReport).one()
    assert sum(l.quantity for l in report.lines if l.operation == v.SALE) == 1

    report.onec_status = OneCUploadStatus.done
    db.commit()
    run([_kit_order(refused=2)])
    assert db.query(SalesReport).one().id == report.id
    last = db.query(ReportFetch).order_by(ReportFetch.id.desc()).first()
    assert "не перезаписываю" in last.message


def test_kit_unknown_variant_is_asked_from_api_once_and_cached(db, kit_sales_from_api):
    account = make_account(db, platform="kit", name="Сайт")
    _cred(db, account, "token", "t")
    fetch.request_fetch(db, "kit_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 30))
    db.commit()
    session = FakeSession({
        "/v1/orders": {"orders": [_kit_order(), _kit_order(order_id="o-9")], "total_count": 2},
        "/v1/variants/v-1": {"barcode": "4600000000001"},
    })
    fetch.run_pending(db, session=session, sleep=lambda s: None)
    assert [c[1] for c in session.calls].count("/v1/variants/v-1") == 1
    report = db.query(SalesReport).one()
    assert {l.barcode for l in report.lines if l.operation == v.SALE} == {"4600000000001"}
    assert db.get(KitVariant, (account.id, "v-1")).barcode == "4600000000001"


def _kit_run(db, account, orders):
    fetch.request_fetch(db, "kit_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 30))
    db.commit()
    session = FakeSession({"/v1/orders": {"orders": orders, "total_count": len(orders)}})
    fetch.run_pending(db, session=session, sleep=lambda s: None)
    return session


def test_kit_fetch_takes_only_delivery_when_sales_come_from_crm(db):
    """Продажи сайта — из AWER CRM: из заказов KIT берётся только доставка,
    иначе продажи посчитались бы дважды; баркоды вариантов не запрашиваются."""
    account = make_account(db, platform="kit", name="Сайт")
    _cred(db, account, "token", "t")
    session = _kit_run(db, account, [_kit_order(), _kit_order(order_id="o-9")])
    assert not [c for c in session.calls if c[1].startswith("/v1/variants")]
    report = db.query(SalesReport).one()
    assert {l.operation for l in report.lines} == {v.LOGISTICS}
    assert report.report_kind == fetch.KIT_DELIVERY_KIND and "-kitdlv-" in report.onec_key
    assert sum(l.logistics_kop for l in report.lines) == 2 * 25000
    job = db.query(ReportFetch).one()
    assert "только доставка" in job.message and "500,00" in job.message


def test_delivery_report_does_not_collide_with_crm_report_of_same_period(db):
    """Отчёт AWER CRM за сентябрь и доставка по API за тот же сентябрь — разные
    отчёты: доставка не должна молча «уже лечь» под ключом CRM."""
    from finapp.finance.importer import make_onec_key
    from tests.finance_factories import make_report
    account = make_account(db, platform="kit", name="Сайт")
    _cred(db, account, "token", "t")
    make_report(db, platform="kit", account_id=account.id, period=(date(2026, 9, 1), date(2026, 9, 30)),
                key=make_onec_key("kit", account.id, "", date(2026, 9, 1), date(2026, 9, 30)),
                lines=[{"operation": v.SALE, "quantity": 1, "revenue": 460000, "payout": 443900}]).source = "file"
    db.commit()
    _kit_run(db, account, [_kit_order()])
    assert db.query(SalesReport).count() == 2


def test_switching_to_crm_replaces_unsent_api_sales_of_the_period(db, kit_sales_from_api):
    from finapp.finance.settings import save_setting
    account = make_account(db, platform="kit", name="Сайт")
    _cred(db, account, "token", "t")
    db.add(KitVariant(account_id=account.id, variant_id="v-1", barcode="2000000000012"))
    db.commit()
    _kit_run(db, account, [_kit_order()])
    assert any(l.operation == v.SALE for l in db.query(SalesReport).one().lines)
    save_setting(db, "FIN_KIT_SALES_SOURCE", "crm")
    db.commit()
    _kit_run(db, account, [_kit_order()])
    report = db.query(SalesReport).one()                       # прежний с продажами удалён
    assert {l.operation for l in report.lines} == {v.LOGISTICS}
    assert "прежнего режима продаж KIT" in db.query(ReportFetch).order_by(ReportFetch.id.desc()).first().message


def test_two_mondays_in_a_row_count_a_week_once(db):
    """Окно автоплана «две последние недели» каждый понедельник сдвигается на
    неделю. Отчёт на календарную неделю: та же неделя — тот же отчёт, и доставка
    08.10 учтена один раз, а не в двух пересекающихся окнах."""
    account = make_account(db, platform="kit", name="Сайт")
    _cred(db, account, "token", "t")
    order = _kit_order(delivered="2026-10-08T10:00:00+03:00")
    for start, end in ((date(2026, 9, 28), date(2026, 10, 11)), (date(2026, 10, 5), date(2026, 10, 18))):
        fetch.request_fetch(db, "kit_api", "system", account.id, start, end)
        db.commit()
        fetch.run_pending(db, session=FakeSession({"/v1/orders": {"orders": [order], "total_count": 1}}),
                          sleep=lambda s: None)
    lines = db.query(SalesReportLine).join(SalesReport).filter(SalesReport.platform == "kit").all()
    assert sum(l.logistics_kop for l in lines) == 25000
    assert [(r.period_from, r.period_to) for r in db.query(SalesReport)] == [(date(2026, 10, 5), date(2026, 10, 11))]


def test_old_window_report_is_superseded_by_weeks_it_covers(db):
    """Отчёт прежнего окна (две недели одним отчётом), целиком покрытый
    забранными заново неделями, убирается; захватывающий незабранную неделю —
    остаётся (иначе та неделя пропала бы)."""
    from finapp.finance.importer import make_onec_key
    from tests.finance_factories import make_report
    account = make_account(db, platform="kit", name="Сайт")
    _cred(db, account, "token", "t")
    for start, end in ((date(2026, 10, 5), date(2026, 10, 18)), (date(2026, 9, 28), date(2026, 10, 11))):
        old = make_report(db, platform="kit", account_id=account.id, period=(start, end),
                          key=make_onec_key("kit", account.id, "", start, end),
                          lines=[{"operation": v.LOGISTICS, "quantity": 0, "logistics": 25000}])
        old.source = "api"
    db.commit()
    orders = [_kit_order(delivered="2026-10-08T10:00:00+03:00"),
              _kit_order(order_id="o-2", delivered="2026-10-15T10:00:00+03:00")]
    fetch.request_fetch(db, "kit_api", "system", account.id, date(2026, 10, 5), date(2026, 10, 18))
    db.commit()
    fetch.run_pending(db, session=FakeSession({"/v1/orders": {"orders": orders, "total_count": 2}}),
                      sleep=lambda s: None)
    periods = sorted((r.period_from, r.period_to) for r in db.query(SalesReport))
    assert periods == [(date(2026, 9, 28), date(2026, 10, 11)),          # захватывает 28.09–04.10
                       (date(2026, 10, 5), date(2026, 10, 11)), (date(2026, 10, 12), date(2026, 10, 18))]


def test_kit_weeks_cover_the_period_and_skip_the_future():
    assert fetch.kit_weeks(date(2026, 9, 1), date(2026, 9, 30))[0] == (date(2026, 8, 31), date(2026, 9, 6))
    assert fetch.kit_weeks(date(2026, 9, 1), date(2026, 9, 30))[-1] == (date(2026, 9, 28), date(2026, 10, 4))
    assert fetch.kit_weeks(date(2099, 1, 1), date(2099, 1, 7)) == []


# ------------------------------------------------------------ автоплан

def test_regular_plan_creates_daily_jobs_once(db):
    wb = make_account(db)
    kit = make_account(db, platform="kit", name="Сайт")
    no_token = make_account(db, name="Без ключей")
    _cred(db, wb, "finance_token", "t")
    _cred(db, kit, "token", "t")
    assert fetch.plan_regular(db) == 2                  # WB и KIT
    assert fetch.plan_regular(db) == 0                  # второй раз за сутки — ничего
    kinds = sorted((j.kind, j.account_id) for j in db.query(ReportFetch))
    assert kinds == sorted([("wb_api", wb.id), ("kit_api", kit.id)])
    kit_job = db.query(ReportFetch).filter(ReportFetch.kind == "kit_api").one()
    assert kit_job.date_to.weekday() == 6 and (kit_job.date_to - kit_job.date_from).days == 13
    assert no_token.id not in {j.account_id for j in db.query(ReportFetch)}


def test_regular_plan_can_be_switched_off(db):
    from finapp.finance.settings import save_setting
    save_setting(db, "FIN_AUTO_FETCH", "0")
    db.commit()
    wb = make_account(db)
    _cred(db, wb, "finance_token", "t")
    assert fetch.plan_regular(db) == 0

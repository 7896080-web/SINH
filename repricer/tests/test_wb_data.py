"""Сбор данных WB (`priceapp/wb_data.py`, страница «Данные WB»): заказы и продажи по курсору `lastChangeDate`,
воронка по дням (вчера, перечитка свежих, добор назад до года), снимок остатков, детализация реализации частями
по `rrdId`, цены по дням без запросов; паузы под лимиты WB, отказ «нет категории токена», сроки хранения.
Сутки — московские (так их считает WB), и тест считает их тем же правилом, что код (`wb_data.msk_date`)."""
from datetime import datetime, timedelta
from decimal import Decimal

import requests

from priceapp import accounts as acc_mod, wb_data as wdt
from priceapp.models import (WbDataState, WbFinanceRow, WbFunnelDay, WbOrder, WbPriceDay, WbSale, WbStockDay,
                             PlatformItem, WorkerHeartbeat)
from priceapp.timeutils import now_utc
from tests import factories as f

NOW = datetime(2026, 10, 18, 9, 0, 0)          # UTC; в Москве 12:00 того же дня


def _acc(db, name="ИП 1", nms=(5, 6)):
    a = f.account(db, "wb", name)
    acc_mod.set_credential(db, a, "token", "t")
    for nm in nms:
        f.item(db, a, f"b{nm}-{a.id}", f"A{nm}", external_id=f"{nm}:{nm}0", size="S")
    from priceapp.models import ApiCredential
    for c in db.query(ApiCredential).filter_by(account_id=a.id):
        c.updated_at = NOW - timedelta(days=1)             # ключ задан заранее, а не «только что сменён»
    db.commit()
    return a


def _http(code):
    r = requests.Response()
    r.status_code = code
    r._content = b'{"title":"unauthorized"}'
    return requests.HTTPError(f"{code} Client Error", response=r)


class FakeWb:
    """Поддельный WB для сбора данных: отвечает заготовками и запоминает, о чём спросили."""
    def __init__(self, orders=(), sales=(), funnel=None, stocks=(), finance=(), fail=None):
        self.orders, self.sales, self.funnel = list(orders), list(sales), funnel or {}
        self.stocks, self.finance, self.fail = list(stocks), list(finance), fail or {}
        self.calls = []

    def _check(self, what):
        if what in self.fail:
            raise self.fail[what]

    def stat_orders(self, date_from):
        self.calls.append(("orders", date_from))
        self._check("orders")
        out, self.orders = (self.orders[0] if self.orders else []), self.orders[1:]
        return out

    def stat_sales(self, date_from):
        self.calls.append(("sales", date_from))
        self._check("sales")
        out, self.sales = (self.sales[0] if self.sales else []), self.sales[1:]
        return out

    def funnel_day(self, day, nm_ids):
        self.calls.append(("funnel", day, tuple(nm_ids)))
        self._check("funnel")
        return [p for p in self.funnel.get(day, []) if p["product"]["nmId"] in nm_ids]

    def stocks_wb(self, offset=0):
        self.calls.append(("stocks", offset))
        self._check("stocks")
        return self.stocks

    def finance_detailed(self, date_from, date_to, rrd_id, limit, fields):
        self.calls.append(("finance", date_from, date_to, rrd_id))
        self._check("finance")
        out, self.finance = (self.finance[0] if self.finance else None), self.finance[1:]
        return out


def _run(db, wb, now):
    return wdt.run(db, client_factory=lambda p, c: wb, now=now)


def _order(srid, date, change, nm=5, spp=31, cancel=False):
    return {"srid": srid, "date": date, "lastChangeDate": change, "nmId": nm, "barcode": f"b{nm}",
            "supplierArticle": f"A{nm}", "techSize": "S", "totalPrice": 4000, "discountPercent": 20, "spp": spp,
            "priceWithDisc": 3200, "finishedPrice": 2208, "isCancel": cancel, "cancelDate": "0001-01-01T00:00:00",
            "warehouseName": "Склад WB", "regionName": "Москва"}


def test_moscow_time_and_day_parsing():
    assert wdt._utc("2026-10-17T23:30:00") == datetime(2026, 10, 17, 20, 30)       # без пояса — Москва
    assert wdt._msk_day("2026-10-17T23:30:00").isoformat() == "2026-10-17"
    assert wdt._msk_day("2026-10-17T22:00:00Z").isoformat() == "2026-10-18"        # 01:00 по Москве
    assert wdt._utc("2026-10-17T10:00:00.1234567") == datetime(2026, 10, 17, 7, 0, 0, 123456)
    assert wdt._utc("0001-01-01T00:00:00") is None
    assert wdt.msk_date(datetime(2026, 10, 17, 21, 30)).isoformat() == "2026-10-18"


def test_orders_first_pass_89_days_back_then_by_cursor_and_update_in_place(db):
    wdt.reset()
    a = _acc(db)
    wb = FakeWb(orders=[[_order("s1", "2026-10-17T23:30:00", "2026-10-17T23:31:00"),
                         _order("s2", "2026-10-18T10:00:00", "2026-10-18T10:05:00.5")],
                        [_order("s1", "2026-10-17T23:30:00", "2026-10-18T11:00:00", cancel=True)]])
    _run(db, wb, NOW)
    first = (wdt.msk_date(NOW) - timedelta(days=89)).isoformat()
    assert ("orders", first) in wb.calls
    o = db.query(WbOrder).filter_by(srid="s1").one()
    assert o.day.isoformat() == "2026-10-17" and o.ordered_at == datetime(2026, 10, 17, 20, 30)
    assert o.spp == Decimal(31) and o.finished_price == Decimal(2208) and o.nm_id == "5" and not o.is_cancel
    st = db.query(WbDataState).filter_by(account_id=a.id, dataset="orders", key="").one()
    assert st.cursor == "2026-10-18T10:05:00.5" and st.error == ""
    # раньше 30 минут «Статистика» обновиться не успевает — не спрашиваем
    _run(db, wb, NOW + timedelta(minutes=10))
    assert sum(1 for c in wb.calls if c[0] == "orders") == 1
    _run(db, wb, NOW + timedelta(minutes=31))
    assert wb.calls[-1][0] == "sales" or ("orders", "2026-10-18T10:05:00.5") in wb.calls
    _run(db, wb, NOW + timedelta(minutes=33))
    assert ("orders", "2026-10-18T10:05:00.5") in wb.calls
    db.expire_all()
    assert db.query(WbOrder).count() == 2 and db.query(WbOrder).filter_by(srid="s1").one().is_cancel


def test_full_answer_means_more_and_sales_are_kept_by_sale_id(db, monkeypatch):
    wdt.reset()
    monkeypatch.setattr(wdt, "STAT_CAP", 2)
    a = _acc(db)
    sale = {"saleID": "S1", "srid": "s1", "date": "2026-10-18T09:00:00", "lastChangeDate": "2026-10-18T09:01:00",
            "nmId": 5, "forPay": "2150.5", "finishedPrice": 2208, "spp": 31}
    ret = dict(sale, saleID="R1", forPay="-2150.5")
    wb = FakeWb(orders=[[_order("s1", "2026-10-18T08:00:00", "2026-10-18T08:00:01"),
                         _order("s2", "2026-10-18T08:10:00", "2026-10-18T08:10:01")], []],
                sales=[[sale, ret]])
    _run(db, wb, NOW)
    st = db.query(WbDataState).filter_by(account_id=a.id, dataset="orders", key="").one()
    assert st.next_at == NOW                                   # пришло «полное» — следующую часть сразу
    _run(db, wb, NOW + timedelta(seconds=70))                  # продажи — к ним дольше не обращались
    _run(db, wb, NOW + timedelta(seconds=140))
    assert [c[0] for c in wb.calls if c[0] in ("orders", "sales")] == ["orders", "sales", "orders"]
    assert [c for c in wb.calls if c[0] == "orders"][-1] == ("orders", "2026-10-18T08:10:01")
    sales = {s.sale_id: s for s in db.query(WbSale)}
    assert sales["S1"].for_pay == Decimal("2150.5") and sales["R1"].for_pay == Decimal("-2150.5")


def test_one_request_per_domain_and_pause_between(db):
    wdt.reset()
    _acc(db)
    wb = FakeWb()
    _run(db, wb, NOW)
    kinds = [c[0] for c in wb.calls]
    assert len([k for k in kinds if k in ("orders", "sales")]) == 1          # «Статистика» — один запрос
    assert len([k for k in kinds if k in ("funnel", "stocks")]) == 1         # «Аналитика» — один
    assert kinds.count("finance") == 1
    n = len(wb.calls)
    _run(db, wb, NOW + timedelta(seconds=10))                                # пауза домена не прошла
    assert len(wb.calls) == n
    _run(db, wb, NOW + timedelta(seconds=25))                                # «Аналитике» — уже можно
    assert len(wb.calls) == n + 1 and wb.calls[-1][0] in ("funnel", "stocks")


def test_missing_token_category_is_named_waits_and_retries_after_key_change(db, client):
    wdt.reset()
    a = _acc(db)
    wb = FakeWb(fail={"orders": _http(401), "sales": _http(401)})
    _run(db, wb, NOW)
    st = db.query(WbDataState).filter_by(account_id=a.id, dataset="orders", key="").one()
    assert "нет категории «Статистика»" in st.error and st.next_at == NOW + wdt.AUTH_RETRY
    _run(db, wb, NOW + timedelta(seconds=70))                                # продажи — тоже отказ
    assert any("«Статистика»" in p and "заказы" in p and "продажи" in p for p in wdt.problems(db))
    calls = len([c for c in wb.calls if c[0] in ("orders", "sales")])
    _run(db, wb, NOW + timedelta(minutes=5))
    assert len([c for c in wb.calls if c[0] in ("orders", "sales")]) == calls    # ждём, а не долбим
    # ключ сменили — пробуем сразу, не дожидаясь шести часов
    wb.fail = {}
    acc_mod.set_credential(db, a, "token", "t2")
    from priceapp.models import ApiCredential
    for c in db.query(ApiCredential).filter_by(account_id=a.id):
        c.updated_at = NOW + timedelta(minutes=6)
    db.commit()
    _run(db, wb, NOW + timedelta(minutes=7))
    assert len([c for c in wb.calls if c[0] in ("orders", "sales")]) == calls + 1
    # страница называет отказ, кнопка снимает паузу
    st.error, st.next_at = "нет доступа — у токена нет категории «Статистика»", now_utc() + timedelta(hours=5)
    db.commit()
    html = client.get("/wb-data").text
    assert "Данные WB" in html and "нет категории «Статистика»" in html and a.name in html
    r = client.post("/wb-data/retry", data={})
    assert "Пауза снята" in r.text
    db.expire_all()
    assert db.query(WbDataState).filter_by(account_id=a.id, dataset="orders", key="").one().next_at is None


def _funnel(nm, opens, orders=0):
    return {"product": {"nmId": nm}, "statistic": {"selected": {
        "openCount": opens, "cartCount": 0, "orderCount": orders, "orderSum": orders * 2000, "buyoutCount": 0,
        "buyoutSum": 0, "cancelCount": 0, "cancelSum": 0, "avgPrice": 2000 if orders else 0, "addToWishlist": 0}}}


def test_funnel_yesterday_first_keeps_only_rows_with_movement_then_goes_back(db):
    wdt.reset()
    a = _acc(db)
    today = wdt.msk_date(NOW)
    y = today - timedelta(days=1)
    wb = FakeWb(funnel={y: [_funnel(5, 10, 1), _funnel(6, 0)]})
    _run(db, wb, NOW)                       # первым на «Аналитике» — снимок остатков (суточный)
    _run(db, wb, NOW + timedelta(seconds=25))
    assert ("funnel", y, (5, 6)) in wb.calls
    rows = db.query(WbFunnelDay).filter_by(account_id=a.id, day=y).all()
    assert [(r.nm_id, r.open_count, r.order_count, r.order_sum) for r in rows] == [("5", 10, 1, 2000)]
    assert wdt.funnel_next_day(db, a.id, NOW + timedelta(minutes=1))[0] == today - timedelta(days=2)
    # через 20 часов вчерашний день (уже позавчерашний) перечитывается: WB дописывает выкупы к дню заказа
    states = {s.key: s for s in db.query(WbDataState).filter_by(account_id=a.id, dataset="funnel")}
    for back in range(2, 366):
        d = (today - timedelta(days=back)).isoformat()
        s = states.get(d) or WbDataState(account_id=a.id, dataset="funnel", key=d, cursor="", rows=0, error="",
                                         updated_at=NOW)
        s.done_at = NOW
        db.add(s)
    db.commit()
    later = NOW + timedelta(hours=21)
    assert wdt.funnel_next_day(db, a.id, later)[0] == wdt.msk_date(later) - timedelta(days=1)


def test_funnel_day_in_parts_when_more_cards_than_a_request_takes(db, monkeypatch):
    wdt.reset()
    from priceapp import platforms
    monkeypatch.setattr(platforms, "WB_FUNNEL_CHUNK", 1)
    a = _acc(db)
    y = wdt.msk_date(NOW) - timedelta(days=1)
    wb = FakeWb(funnel={y: [_funnel(5, 3), _funnel(6, 4)]})
    for i in range(4):
        _run(db, wb, NOW + timedelta(seconds=25 * i))
    parts = [c[2] for c in wb.calls if c[0] == "funnel" and c[1] == y]
    assert parts == [(5,), (6,)]
    st = db.query(WbDataState).filter_by(account_id=a.id, dataset="funnel", key=y.isoformat()).one()
    assert st.cursor == "" and st.done_at is not None and st.rows == 2


def test_stocks_snapshot_once_a_day_by_size(db):
    wdt.reset()
    a = _acc(db)
    wb = FakeWb(stocks=[{"nmId": 5, "chrtId": 50, "warehouseId": -999999, "quantity": 7, "inWayToClient": 2,
                         "inWayFromClient": 1},
                        {"nmId": 5, "chrtId": 50, "warehouseId": 1, "quantity": 3, "inWayToClient": 0,
                         "inWayFromClient": 0},
                        {"nmId": 6, "chrtId": 60, "warehouseId": -999999, "quantity": 0, "inWayToClient": 0,
                         "inWayFromClient": 0}])
    for i in range(3):
        _run(db, wb, NOW + timedelta(seconds=25 * i))
    assert [c for c in wb.calls if c[0] == "stocks"] == [("stocks", 0)]          # раз в сутки
    got = {(r.nm_id, r.chrt_id): (r.quantity, r.to_client, r.from_client) for r in db.query(WbStockDay)}
    assert got == {("5", "50"): (10, 2, 1), ("6", "60"): (0, 0, 0)}
    assert all(r.day == wdt.msk_date(NOW) for r in db.query(WbStockDay))
    tomorrow = NOW + timedelta(days=1)
    _run(db, wb, tomorrow)
    assert [c for c in wb.calls if c[0] == "stocks"] == [("stocks", 0), ("stocks", 0)]


def _fin(rrd, nm=5, oper="Продажа", logistics="0"):
    return {"reportId": 900, "dateFrom": "2026-10-06", "dateTo": "2026-10-12", "rrdId": rrd, "nmId": nm,
            "vendorCode": "A5", "techSize": "S", "sku": "b5", "docTypeName": "Продажа", "sellerOperName": oper,
            "srid": f"s{rrd}", "orderDt": "2026-10-07T10:00:00Z", "saleDt": "2026-10-09T12:00:00Z",
            "rrDate": "2026-10-09", "quantity": 1, "retailPrice": "4000", "retailAmount": "3200",
            "retailPriceWithDisc": "3200", "salePercent": 20, "commissionPercent": 43.5, "kvwBase": 36.25,
            "kvw": 33.1, "isKgvpV2": 3.15, "spp": 31, "forPay": "1700.20", "ppvzSalesCommission": "1100",
            "acquiringFee": "86.4", "acquiringPercent": 2.7, "deliveryAmount": 0, "returnAmount": 0,
            "deliveryService": logistics, "penalty": "0", "additionalPayment": "0", "paidStorage": "0",
            "deduction": "0", "paidAcceptance": "0", "rebillLogisticCost": "0", "sellerPromoId": 0,
            "sellerPromoDiscount": 0}


def test_finance_parts_by_rrd_until_204_then_rereads_three_weeks_daily(db):
    wdt.reset()
    a = _acc(db)
    wb = FakeWb(finance=[[_fin(1), _fin(2, oper="Логистика", logistics="126")], [_fin(2)], None])
    today = wdt.msk_date(NOW)
    for i in range(4):
        _run(db, wb, NOW + timedelta(seconds=70 * i))
    fin = [c for c in wb.calls if c[0] == "finance"]
    start, end = (today - timedelta(days=120)).isoformat(), (today - timedelta(days=1)).isoformat()
    assert fin[:3] == [("finance", start, end, 0), ("finance", start, end, 2), ("finance", start, end, 2)]
    rows = {r.rrd_id: r for r in db.query(WbFinanceRow)}
    assert set(rows) == {"1", "2"}                                       # повтор строки не задвоил
    assert rows["1"].kgvp_v2 == Decimal("3.15") and rows["1"].for_pay == Decimal("1700.20")
    assert rows["2"].logistics == Decimal("126") and rows["2"].oper == "Логистика"
    assert rows["1"].day.isoformat() == "2026-10-09"
    st = db.query(WbDataState).filter_by(account_id=a.id, dataset="finance", key="").one()
    assert len(fin) == 3 and st.next_at == NOW + timedelta(seconds=140) + wdt.FINANCE_EVERY
    wb.finance = [None]
    _run(db, wb, st.next_at + timedelta(seconds=1))                   # через сутки — последние 3 недели
    again = [c for c in wb.calls if c[0] == "finance"][-1]
    assert again[1] == (today - timedelta(days=21)).isoformat() and again[3] == 0


def test_prices_by_day_from_loaded_prices_without_requests(db):
    wdt.reset()
    a = _acc(db)
    for it in db.query(PlatformItem).filter_by(account_id=a.id):
        it.current_price, it.current_sale_price = (4000, 3200) if it.external_id.startswith("5:") else (3000, 2400)
    a.prices_loaded_at = NOW - timedelta(minutes=5)
    db.commit()
    wb = FakeWb()
    assert wdt.snapshot_prices(db, a, NOW) == 2
    got = {r.nm_id: (r.price, r.sale_price) for r in db.query(WbPriceDay)}
    assert got == {"5": (4000, 3200), "6": (3000, 2400)}
    assert wdt.snapshot_prices(db, a, NOW + timedelta(minutes=1)) == 0           # новых цен не было
    a.prices_loaded_at = NOW + timedelta(hours=2)                                 # перезагрузили — перезаписать
    db.query(PlatformItem).filter_by(account_id=a.id, external_id="5:50").one().current_sale_price = 3000
    db.commit()
    assert wdt.snapshot_prices(db, a, NOW + timedelta(hours=2, minutes=1)) == 2
    assert db.query(WbPriceDay).filter_by(nm_id="5").one().sale_price == 3000
    assert db.query(WbPriceDay).count() == 2 and not wb.calls


def test_job_heartbeat_attention_and_retention(db, client):
    from priceapp.workers import jobs
    wdt.reset()
    a = _acc(db)
    jobs.job_wb_data(client_factory=lambda p, c: FakeWb(fail={"orders": _http(403), "funnel": _http(401),
                                                              "stocks": _http(401), "finance": _http(401)}))
    hb = db.get(WorkerHeartbeat, "wb_data")
    db.refresh(hb)
    assert hb.last_success and "«Статистика»" in hb.last_error and "«Финансы»" in hb.last_error
    from priceapp import overview
    from priceapp.routers.prices import product_rows
    overview.refresh_heavy(db, product_rows)
    html = client.get("/attention").text
    assert "сбор данных WB" in html and "/wb-data" in html
    # сроки хранения — по календарному дню данных
    old = wdt.msk_date(now_utc()) - timedelta(days=wdt.KEEP_DAYS + 1)
    db.add(WbOrder(account_id=a.id, srid="old", day=old, fetched_at=now_utc()))
    db.add(WbOrder(account_id=a.id, srid="new", day=wdt.msk_date(now_utc()), fetched_at=now_utc()))
    db.add(WbFunnelDay(account_id=a.id, day=old, nm_id="5", fetched_at=now_utc()))
    db.commit()
    from priceapp import retention
    assert retention.prune_wb_data(db) == 2
    assert [o.srid for o in db.query(WbOrder)] == ["new"]


def test_client_requests_follow_the_spec():
    """Тела и адреса запросов — как в спеке WB (11-analytics, 12-reports, 13-finances); 204 — пусто."""
    from datetime import date

    from priceapp.platforms import WbClient

    class Resp:
        def __init__(self, code=200, body=b"{}"):
            self.status_code, self.content = code, body

        def raise_for_status(self):
            pass

        def json(self):
            import json
            return json.loads(self.content)

    class Session:
        headers = {}

        def __init__(self):
            self.sent = []

        def request(self, method, url, params=None, json=None, timeout=None):
            self.sent.append((method, url, params, json))
            if "detailed" in url:
                return Resp(204, b"")
            if "sales-funnel" in url:
                return Resp(body=b'{"data":{"products":[{"product":{"nmId":5},"statistic":{"selected":{}}}]}}')
            if "wb-warehouses" in url:
                return Resp(body=b'{"data":{"items":[{"nmId":5}]}}')
            return Resp(body=b"[]")

    s = Session()
    c = WbClient("t", session=s)
    assert c.stat_orders("2026-07-21") == [] and c.stat_sales("2026-07-21T10:00:00") == []
    assert c.funnel_day(date(2026, 10, 17), [5, 6])[0]["product"]["nmId"] == 5
    assert c.stocks_wb(0) == [{"nmId": 5}]
    assert c.finance_detailed("2026-06-20", "2026-10-17", 0, 20000, wdt.FINANCE_FIELDS) is None
    (m1, u1, p1, _), (_, u2, p2, _), (m3, u3, _, b3), (_, u4, _, b4), (_, u5, _, b5) = s.sent
    assert (m1, u1, p1) == ("GET", "https://statistics-api.wildberries.ru/api/v1/supplier/orders",
                            {"dateFrom": "2026-07-21", "flag": 0})
    assert u2.endswith("/api/v1/supplier/sales") and p2["dateFrom"] == "2026-07-21T10:00:00"
    assert (m3, u3) == ("POST", "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products")
    assert b3["selectedPeriod"] == {"start": "2026-10-17", "end": "2026-10-17"} and b3["nmIds"] == [5, 6]
    assert b3["limit"] == 2
    assert u4.endswith("/api/analytics/v1/stocks-report/wb-warehouses") and b4 == {"limit": 250000, "offset": 0}
    assert u5 == "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"
    assert b5["period"] == "weekly" and b5["rrdId"] == 0 and "isKgvpV2" in b5["fields"]

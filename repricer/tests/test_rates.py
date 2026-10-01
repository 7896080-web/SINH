"""Курс ЦБ: разбор настоящего формата, запрос, ручной режим."""
from datetime import date
from decimal import Decimal

import pytest

from priceapp import rates, settings
from priceapp.models import ExchangeRate

CBR = ('<?xml version="1.0" encoding="windows-1251"?><ValCurs Date="02.10.2026" name="Foreign Currency Market">'
       '<Valute ID="R01239"><NumCode>978</NumCode><CharCode>EUR</CharCode><Nominal>1</Nominal><Name>Евро</Name><Value>95,1000</Value></Valute>'
       '<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal><Name>Доллар США</Name><Value>81,5432</Value></Valute>'
       '</ValCurs>').encode("cp1251")


class _Resp:
    def __init__(self, content, code=200):
        self.content, self.status_code = content, code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class _Http:
    def __init__(self, content=CBR, code=200):
        self.content, self.code, self.calls = content, code, 0

    def get(self, url, timeout=0):
        self.calls += 1
        return _Resp(self.content, self.code)


def test_parse_cbr_xml_takes_usd_not_first_currency():
    d, v = rates.parse_cbr_xml(CBR)
    assert d == date(2026, 10, 2) and v == Decimal("81.5432")


def test_parse_handles_nominal():
    xml = CBR.replace(b"<Nominal>1</Nominal><Name>\xc4", b"<Nominal>10</Nominal><Name>\xc4")
    assert rates.parse_cbr_xml(xml)[1] == Decimal("8.1543")


def test_parse_rejects_garbage():
    with pytest.raises(rates.RateError):
        rates.parse_cbr_xml(b"<html>maintenance</html>")


def test_fetch_stores_and_updates_same_date(db):
    rates.fetch_cbr(db, _Http())
    rates.fetch_cbr(db, _Http())
    assert db.query(ExchangeRate).count() == 1
    cur = rates.current(db)
    assert cur.usd_rub == Decimal("81.5432") and cur.source == "cbr" and "02.10.2026" in cur.label


def test_fetch_error_is_rate_error(db):
    with pytest.raises(rates.RateError):
        rates.fetch_cbr(db, _Http(code=503))


def test_no_rate_is_none(db):
    assert rates.current(db) is None


def test_manual_mode(db):
    rates.fetch_cbr(db, _Http())
    settings.put(db, settings.RATE_MODE, "manual")
    settings.put(db, settings.RATE_MANUAL, "90,5")
    assert rates.current(db).usd_rub == Decimal("90.5000")
    settings.put(db, settings.RATE_MANUAL, "")
    assert rates.current(db) is None               # ручной режим без курса — не подставляем ЦБ молча


def test_job_rate_fetches_once_per_day(db):
    from priceapp.workers import jobs
    http = _Http()
    jobs.job_rate(http)
    assert http.calls == 1

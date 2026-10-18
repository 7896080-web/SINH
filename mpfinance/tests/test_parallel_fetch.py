"""Кабинеты WB разбираются параллельно, по дорожке на кабинет (`fetch.run_pending`).

Лимит WB — запрос в минуту на профиль продавца, у каждого ИП свой: по очереди
три кабинета ждали друг друга. Здесь проверяется ровно это — дорожки идут
одновременно (без этого барьер на двоих не дождётся второго), запись в базу при
этом по одной, и сбой одной дорожки не задевает соседнюю."""
import threading
import time
from datetime import date

from finapp.finance import fetch, importer, values as v
from finapp.finance.sources import wb as wb_source
from finapp.models import ReportFetch, SalesReport, WorkerHeartbeat
from tests.finance_factories import make_account, make_product
from tests.test_finance_api_fetch import WB_HEADER, _cred, _wb_line


def _two_cabinets(db):
    make_product(db)
    jobs = []
    for i, name in enumerate(("ИП Первый", "ИП Второй"), start=1):
        account = make_account(db, name=name)
        _cred(db, account, "finance_token", f"tok-{i}")
        job, error = fetch.request_fetch(db, "wb_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 30))
        assert error == ""
        jobs.append(job)
    db.commit()
    return jobs


class FakeApi:
    """Клиент WB на кабинет: отчёт у каждого свой (номер — от токена)."""
    barrier: threading.Barrier | None = None

    def __init__(self, token, session=None, sleep=None):
        self.number = 307401554 + int(token.split("-")[1])

    def list_reports(self, date_from, date_to):
        if FakeApi.barrier is not None:
            FakeApi.barrier.wait()              # по очереди второй сюда не придёт никогда
        return [dict(WB_HEADER, reportId=self.number)]

    def report_lines(self, report_id, on_page=None):
        return [_wb_line(reportId=report_id, srid=f"s-{report_id}")]


def test_two_wb_cabinets_are_fetched_at_the_same_time(db, monkeypatch):
    jobs = _two_cabinets(db)
    monkeypatch.setattr(wb_source, "WbFinanceApi", FakeApi)
    monkeypatch.setattr(FakeApi, "barrier", threading.Barrier(2, timeout=5))
    stats = fetch.run_pending(db, sleep=lambda s: None)
    assert stats == {"jobs": 2, "imported": 2, "failed": 0}
    assert [j.status for j in jobs] == ["done", "done"]           # без refresh: сессия обновлена
    assert sorted(r.report_number for r in db.query(SalesReport)) == ["307401555", "307401556"]


def test_writes_go_one_at_a_time(db, monkeypatch):
    _two_cabinets(db)
    monkeypatch.setattr(wb_source, "WbFinanceApi", FakeApi)
    monkeypatch.setattr(FakeApi, "barrier", threading.Barrier(2, timeout=5))
    active, peak, lock = [0], [0], threading.Lock()
    real = importer.store_api_report

    def slow_store(db_, **kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        try:
            return real(db_, **kw)
        finally:
            with lock:
                active[0] -= 1
    monkeypatch.setattr(fetch, "store_api_report", slow_store)
    assert fetch.run_pending(db, sleep=lambda s: None)["imported"] == 2
    assert peak[0] == 1


def test_one_cabinet_failing_does_not_touch_the_other(db, monkeypatch):
    first, second = _two_cabinets(db)
    monkeypatch.setattr(wb_source, "WbFinanceApi", FakeApi)
    real = fetch._run_wb

    def broken_for_first(db_, job, *args):
        if job.account_id == first.account_id:
            raise RuntimeError("WB оборвал соединение")
        return real(db_, job, *args)
    monkeypatch.setattr(fetch, "_run_wb", broken_for_first)
    stats = fetch.run_pending(db, sleep=lambda s: None)
    assert stats == {"jobs": 2, "imported": 1, "failed": 1}
    assert (first.status, second.status) == ("failed", "done")
    assert "WB оборвал соединение" in first.message


def test_lanes_wb_by_cabinet_the_rest_together():
    assert fetch.lane_of("wb_api", 3) == ("wb", 3) != fetch.lane_of("wb_api", 4)
    assert fetch.lane_of("kit_api", 5) == fetch.lane_of("crm_db", 5) == fetch.OTHER_LANE


def test_fetch_heartbeat_has_its_own_session_and_is_throttled(db, monkeypatch):
    from finapp.workers import jobs
    writes = []
    monkeypatch.setattr(jobs, "beat", lambda s, name, ok=True, error="": writes.append(s))
    heartbeat = jobs._fetch_heartbeat()
    threads = [threading.Thread(target=heartbeat) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(writes) == 1 and writes[0] is not db
    monkeypatch.setattr(jobs, "beat", __import__("finapp.workers.heartbeat", fromlist=["beat"]).beat)
    jobs._fetch_heartbeat()()
    assert db.get(WorkerHeartbeat, "finance_fetch") is not None

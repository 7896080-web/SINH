"""График работы и норма 18 рабочих дней: «% плана месяца» к концу месяца совпадает с выполнением для ставки."""
import pytest
from fastapi.testclient import TestClient

from awer_sales_bot.jobs import Sender, daily_run
from awer_sales_bot.planning import service
from awer_sales_bot.web import create_app
from test_flow import env  # noqa: F401
from test_motivation import import_sales, oct_day

AUTH = ("admin", "pw")


def test_status_by_schedule_matches_rate_at_month_end(env, tmp_path):  # noqa: F811
    cfg, db = env
    assert cfg.norm_shifts == 18
    service.freeze(db, cfg, "2026-10")
    sid = db.seller_id("Продавец04", create=False)
    plan = service.frozen_plan(db, "2026-10", sid)
    sched = {d for d in range(1, 32) if (d - 1) % 4 < 2}  # 2 через 2 с 1-го: 16 смен
    db.set_schedule("2026-10", sid, sched)
    worked = sorted(sched)[:8]  # к 15.10 отработал 8 смен по графику
    import_sales(tmp_path / "s.csv", [(oct_day(d), "Продавец04", 35_000, 3) for d in worked], db, cfg)

    st = service.status(db, cfg, sid, oct_day(15))
    assert (st.worked, st.planned_shifts, st.remaining_shifts) == (8, 16, 8)
    assert st.plan_month == pytest.approx(plan * 16 / 18)

    # продажа 16-го (не по графику) и все смены по графику до конца месяца
    rest = sorted(d for d in sched if d > 15)
    import_sales(tmp_path / "s2.csv", [(oct_day(d), "Продавец04", 35_000, 3) for d in worked + [16] + rest],
                db, cfg)
    end = service.status(db, cfg, sid, oct_day(31))
    # решение владельца 19.10.2026: день вне графика сам не засчитывается — ждёт ответа в админке
    assert end.worked == 16 and end.planned_shifts == 16
    assert [(x["day"], x["seller_id"]) for x in db.offschedule_pending()] == [("2026-10-16", sid)]
    db.decide_offschedule("2026-10-16", sid, worked=True)  # «Работал(а) — добавить в график»
    assert db.offschedule_pending() == []
    end = service.status(db, cfg, sid, oct_day(31))
    assert end.worked == 17 and end.planned_shifts == 17 and end.remaining_shifts == 0
    assert end.sales / end.plan_month == pytest.approx(end.performance)  # одно и то же «выполнение»
    assert db.schedule_days("2026-10")[sid][16] is not None  # магазин дня — где продавал


def test_sale_off_schedule_not_worked_and_fallback_without_schedule(env, tmp_path):  # noqa: F811
    """«Не работал(а)» — покупатель забрал отложенное: продажа остаётся у продавца, день не рабочий.
    Без графика рабочий день — день с продажами; день только с возвратом — не рабочий."""
    cfg, db = env
    a, b = db.seller_id("Продавец04", create=False), db.seller_id("Продавец05", create=False)
    db.set_schedule("2026-10", a, {1, 2, 3})
    import_sales(tmp_path / "s.csv", [(oct_day(d), "Продавец04", 20_000, 2) for d in (1, 2, 3, 5)] +
                 [(oct_day(d), "Продавец05", 20_000, 2) for d in (1, 2)] + [(oct_day(4), "Продавец05", -5_000, -1)],
                 db, cfg)
    assert db.worked_shifts(a, "2026-10") == 3
    db.decide_offschedule("2026-10-05", a, worked=False)
    assert db.offschedule_pending() == [] and db.worked_shifts(a, "2026-10") == 3
    assert db.one("SELECT SUM(amount) s FROM sales_lines WHERE seller_id=?", a)["s"] == 80_000
    assert db.worked_shifts(b, "2026-10") == 2  # графика нет: дни с продажами, 4-е (только возврат) — нет
    # все магазины закрыты (отметка «закрыто», продаж из 1С за день нет) — день графика не рабочий
    with db.tx() as c:
        c.execute("DELETE FROM imported_days WHERE day='2026-10-03'")
    db.set_store_closed(oct_day(3), cfg.stores)
    assert db.worked_shifts(a, "2026-10") == 2


def test_offschedule_page_and_closed_month_recomputed(env, tmp_path):  # noqa: F811
    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    sid = db.seller_id("Продавец04", create=False)
    db.set_schedule("2026-10", sid, {1, 2})
    import_sales(tmp_path / "s.csv", [(oct_day(d), "Продавец04", 30_000, 3) for d in (1, 2, 7)], db, cfg)
    service.close_month(db, cfg, "2026-10")
    fte = db.one("SELECT fte FROM results WHERE month='2026-10' AND seller_id=?", sid)["fte"]
    assert fte == pytest.approx(2 / 18)
    c = TestClient(create_app(cfg, db))
    r = c.get("/schedule/2026-10", auth=AUTH)
    assert "Продажи в дни вне графика" in r.text and "07.10.2026" in r.text
    r = c.post("/offschedule", data={"day": "2026-10-07", "sid": sid, "worked": "1", "back": "2026-10"}, auth=AUTH)
    assert r.status_code == 200 and "Итоги 2026-10 пересчитаны" in r.text
    assert "Продажи в дни вне графика" not in r.text
    fte = db.one("SELECT fte FROM results WHERE month='2026-10' AND seller_id=?", sid)["fte"]
    assert fte == pytest.approx(3 / 18)
    assert c.post("/offschedule", data={"day": "x", "sid": sid, "worked": "1"}, auth=AUTH).status_code == 400


def test_no_schedule_falls_back_to_norm_and_owner_is_reminded(env, tmp_path):  # noqa: F811
    cfg, db = env
    cfg.max.owners = [1]
    service.freeze(db, cfg, "2026-10")
    sid = db.seller_id("Продавец04", create=False)
    import_sales(tmp_path / "s.csv", [(oct_day(d), "Продавец04", 30_000, 3) for d in range(1, 21)], db, cfg)
    st = service.status(db, cfg, sid, oct_day(20))
    assert st.planned_shifts == 20 and st.remaining_shifts == 0  # отработал больше нормы — не «20 из 18»
    sender = Sender(db, cfg, None, dry_run=True)
    daily_run(db, cfg, sender, today=oct_day(21), force=True)
    daily_run(db, cfg, sender, today=oct_day(22), force=True)
    rows = db.q("SELECT text FROM message_log WHERE kind='no_schedule'")
    assert len(rows) == 2 and rows[0]["text"].startswith("График работы на 2026-10 не заполнен")  # dry-run пишет каждый раз


def test_schedule_page(env):  # noqa: F811
    cfg, db = env
    web = TestClient(create_app(cfg, db))
    sid = db.seller_id("Продавец04", create=False)
    page = web.get("/schedule/2026-10", auth=AUTH).text
    assert "График работы 2026-10" in page and "Норма — 18 рабочих дней" in page and f'name="w{sid}_31_0"' in page and "<h2>МСЛ</h2>" in page
    r = web.post("/schedule/2026-10", data={f"w{sid}_1_0": "on", f"w{sid}_2_0": "on", f"w{sid}_5_1": "on"}, auth=AUTH)
    assert "График сохранён" in r.text
    assert set(db.schedule_days("2026-10")[sid]) == {1, 2, 5}
    web.post("/schedule/2026-10", data={f"w{sid}_3_0": "on"}, auth=AUTH)  # пересохранение заменяет месяц
    assert set(db.schedule_days("2026-10")[sid]) == {3}
    assert db.planned_shifts(sid, "2026-10") == 1 and db.planned_shifts(sid, "2026-11") is None

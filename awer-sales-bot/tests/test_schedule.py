"""График работы и норма 18 рабочих дней: «% плана месяца» к концу месяца совпадает с выполнением для ставки."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from awer_sales_bot.importers.onec_sales import import_file
from awer_sales_bot.jobs import Sender, daily_run
from awer_sales_bot.planning import service
from awer_sales_bot.web import create_app
from test_flow import env  # noqa: F401
from test_motivation import oct_day, write_sales

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
    import_file(write_sales(tmp_path / "s.csv", [(oct_day(d), "Продавец04", 35_000, 3) for d in worked]), db, cfg)

    st = service.status(db, cfg, sid, oct_day(15))
    assert (st.worked, st.planned_shifts, st.remaining_shifts) == (8, 16, 8)
    assert st.plan_month == pytest.approx(plan * 16 / 18)

    # вышел на замену 16-го (не по графику) и отработал все смены по графику до конца месяца
    rest = sorted(d for d in sched if d > 15)
    import_file(write_sales(tmp_path / "s2.csv", [(oct_day(d), "Продавец04", 35_000, 3) for d in worked + [16] + rest]),
                db, cfg)
    end = service.status(db, cfg, sid, oct_day(31))
    assert end.worked == 17 and end.planned_shifts == 17 and end.remaining_shifts == 0
    assert end.sales / end.plan_month == pytest.approx(end.performance)  # одно и то же «выполнение»


def test_no_schedule_falls_back_to_norm_and_owner_is_reminded(env, tmp_path):  # noqa: F811
    cfg, db = env
    cfg.max.owners = [1]
    service.freeze(db, cfg, "2026-10")
    sid = db.seller_id("Продавец04", create=False)
    import_file(write_sales(tmp_path / "s.csv", [(oct_day(d), "Продавец04", 30_000, 3) for d in range(1, 21)]), db, cfg)
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
    assert "График работы 2026-10" in page and "Норма — 18 рабочих дней" in page and f'name="s{sid}_31"' in page
    r = web.post("/schedule/2026-10", data={f"s{sid}_1": "on", f"s{sid}_2": "on", f"s{sid}_5": "on"}, auth=AUTH)
    assert "График сохранён" in r.text
    assert db.schedule_days("2026-10")[sid] == {1, 2, 5}
    web.post("/schedule/2026-10", data={f"s{sid}_3": "on"}, auth=AUTH)  # пересохранение заменяет месяц
    assert db.schedule_days("2026-10")[sid] == {3}
    assert db.planned_shifts(sid, "2026-10") == 1 and db.planned_shifts(sid, "2026-11") is None

"""Смены: ротация магазинов по средам, замена, минимум продавцов в магазине, сильные продавцы на выходных."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from awer_sales_bot import crews
from awer_sales_bot.jobs import Sender, daily_run
from awer_sales_bot.web import create_app
from test_flow import env  # noqa: F401

AUTH = ("admin", "pw")
WED = date(2026, 10, 14)  # среда


def setup_crews(db, cfg, a: list[str], b: list[str]):
    crews.ensure_crews(db, cfg)
    c1, c2 = crews.crews(db)
    with db.tx() as c:
        c.execute("UPDATE crews SET store='Город', anchor=? WHERE id=?", (WED.isoformat(), c1["id"]))
        c.execute("UPDATE crews SET store='МСЛ', anchor=? WHERE id=?", (WED.isoformat(), c2["id"]))
        for names, cid in ((a, c1["id"]), (b, c2["id"])):
            for n in names:
                c.execute("UPDATE sellers SET crew_id=? WHERE id=?", (cid, db.seller_id(n, create=False)))
    return crews.crews(db)


def test_rotation_every_wednesday(env):  # noqa: F811
    cfg, db = env
    c1, c2 = setup_crews(db, cfg, ["Продавец04"], ["Продавец05"])
    assert WED.weekday() == 2 and crews.wednesday(WED + timedelta(days=6)) == WED
    assert [crews.crew_store(c1, WED + timedelta(days=k), cfg.stores) for k in (0, 6, 7, 13, 14)] == \
        ["Город", "Город", "МСЛ", "МСЛ", "Город"]
    assert crews.crew_store(c2, WED + timedelta(days=7), cfg.stores) == "Город"
    assert crews.crew_store(c1, WED - timedelta(days=1), cfg.stores) == "МСЛ"  # вторник до якоря — прошлая неделя
    sid = db.seller_id("Продавец04", create=False)
    assert crews.store_on(db, cfg, sid, WED + timedelta(days=8)) == "МСЛ"
    db.set_schedule("2026-10", sid, {23: "Город"})  # 23.10 вышел на замену в Город
    assert crews.store_on(db, cfg, sid, date(2026, 10, 23)) == "Город"


def test_staffing_minimum_and_strong_sellers_on_weekend(env):  # noqa: F811
    cfg, db = env
    a = ["Продавец04", "Продавец05", "Продавец06"]
    b = ["Продавец07", "Продавец08", "Админ1"]
    setup_crews(db, cfg, a, b)
    sat, sun, mon = date(2026, 10, 17), date(2026, 10, 18), date(2026, 10, 19)
    for n in a:  # смена 1 (Город): все трое в сб, вс и пн
        db.set_schedule("2026-10", db.seller_id(n, create=False), {17, 18, 19})
    for n in b[:2]:  # смена 2 (МСЛ): только двое в сб и вс, в пн — никого
        db.set_schedule("2026-10", db.seller_id(n, create=False), {17, 18})
    with db.tx() as c:
        c.execute("UPDATE sellers SET weekend_key=1 WHERE name IN ('Продавец04', 'Админ1')")
    probs = crews.problems(db, cfg, sat, mon)
    assert "сб 17.10, МСЛ: 2 из 3" in probs and "вс 18.10, МСЛ: 2 из 3" in probs and "пн 19.10, МСЛ: 0 из 2" in probs
    assert not any("Город" in p for p in probs)
    assert "сб 17.10: не работает Админ1 (сильные продавцы — на выходные)" in probs

    db.set_schedule("2026-10", db.seller_id("Админ1", create=False), {17, 18, 19})  # Админ1 вышла на выходные
    db.set_schedule("2026-10", db.seller_id("Продавец06", create=False), {17: None, 18: None, 19: "МСЛ"})  # пн — замена в МСЛ
    assert crews.problems(db, cfg, sat, mon) == []  # МСЛ: 3, 3 и 2 (с заменой), Город в пн — двое


def test_crews_and_schedule_pages(env):  # noqa: F811
    cfg, db = env
    web = TestClient(create_app(cfg, db))
    page = web.get("/crews", auth=AUTH).text
    assert "Смена 1" in page and "Смена 2" in page and "Сильный продавец" in page
    c1, c2 = crews.crews(db)
    sid = db.seller_id("Продавец04", create=False)
    r = web.post("/crews", data={f"name{c1['id']}": "Смена Эли", f"store{c1['id']}": "МСЛ",
                                 f"name{c2['id']}": "Смена Юли", f"store{c2['id']}": "Город",
                                 f"crew{sid}": str(c1["id"]), f"key{sid}": "on"}, auth=AUTH)
    assert "Сохранено" in r.text and "Смена Эли" in r.text
    s = db.seller(sid)
    assert s["crew_id"] == c1["id"] and s["weekend_key"] == 1
    today = date.today()
    assert crews.store_on(db, cfg, sid, today) == "МСЛ"

    month = today.strftime("%Y-%m")
    web.post(f"/schedule/{month}", data={f"s{sid}_1": "МСЛ", f"s{sid}_2": "Город"}, auth=AUTH)
    saved = db.schedule_days(month)[sid]
    d1, d2 = today.replace(day=1), today.replace(day=2)
    assert saved[1] == (None if crews.default_store(db, cfg, sid, d1) == "МСЛ" else "МСЛ")  # магазин смены не храним
    assert saved[2] == (None if crews.default_store(db, cfg, sid, d2) == "Город" else "Город")
    sched = web.get(f"/schedule/{month}", auth=AUTH).text
    assert "Смена Эли · сильный" in sched and "Город</b>: продавцов" in sched


def test_owner_warned_about_staffing(env):  # noqa: F811
    cfg, db = env
    cfg.max.owners = [1]
    setup_crews(db, cfg, ["Продавец04"], ["Продавец05"])
    db.set_schedule("2026-10", db.seller_id("Продавец04", create=False), {20, 21})
    daily_run(db, cfg, Sender(db, cfg, None, dry_run=True), today=date(2026, 10, 20), force=True)
    text = db.one("SELECT text FROM message_log WHERE kind='staffing'")["text"]
    assert text.startswith("График работы на ближайшие дни:") and "вт 20.10, МСЛ: 0 из 2" in text

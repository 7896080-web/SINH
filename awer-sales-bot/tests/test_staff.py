"""Страница «Продавцы»: добавление, карточка подключения с QR, увольнение, возврат."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from awer_sales_bot import bot
from awer_sales_bot.jobs import Sender, daily_run
from awer_sales_bot.planning import service
from awer_sales_bot.web import create_app
from test_flow import env  # noqa: F401
from test_motivation import FakeMax, import_sales, oct_day

AUTH = ("admin", "pw")


def test_add_card_link_fire_restore(env, tmp_path, monkeypatch):  # noqa: F811
    cfg, db = env
    cfg.max.bot_link = "https://max.ru/id9102000000_bot"
    cfg.max.owners = [1]
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    web = TestClient(create_app(cfg, db))

    r = web.post("/sellers/add", data={"name": " Новикова  Мария ", "first_name": "Мария", "alias": "Новикова М."},
                 auth=AUTH, follow_redirects=False)
    sid = db.seller_id("Новикова Мария", create=False)
    assert r.status_code == 303 and r.headers["location"] == f"/sellers/{sid}/card"
    assert db.seller_id("Новикова М.", create=False) == sid  # другое написание в 1С
    s = db.seller(sid)
    assert s["hired_at"] and s["invite_code"] and s["active"] == 1
    card = web.get(f"/sellers/{sid}/card", auth=AUTH).text
    assert "<svg" in card and f"start={s['invite_code']}" in card and "Готово, Мария!" in card

    again = web.post("/sellers/add", data={"name": "Новикова Мария"}, auth=AUTH)
    assert "Не добавлен: «Новикова Мария» уже есть в списке" in again.text
    page = web.get("/sellers", auth=AUTH).text
    assert "Ждут подключения к боту (" in page and "Новикова Мария" in page

    client = FakeMax()
    bot.handle(db, cfg, client, {"update_type": "bot_started", "user": {"user_id": 555}, "payload": s["invite_code"]})
    assert client.sent[0][1].startswith("Готово, Мария!")
    assert "подключён к боту" in web.get(f"/sellers/{sid}/card", auth=AUTH).text

    service.freeze(db, cfg, "2026-10")
    import_sales(tmp_path / "s.csv", [(oct_day(5), "Новикова М.", 20_000, 2)], db, cfg)
    r = web.post("/sellers", data={"action": f"fire:{sid}"}, auth=AUTH)
    assert "уволен(а), бот отключён" in r.text and "Уволены (1)" in r.text
    s = db.seller(sid)
    assert s["active"] == 0 and s["max_user_id"] is None and s["fired_at"] == date.today().isoformat()

    client.sent.clear()
    bot.handle(db, cfg, client, {"update_type": "message_created",
                                 "message": {"sender": {"user_id": 555}, "body": {"text": "/plan"}}})
    assert client.sent[0][1] == "Сначала отправьте код от администратора."  # уволенному — ничего
    res = {x["name"]: x for x in service.close_month(db, cfg, "2026-10")}
    assert res["Новикова Мария"]["sales"] == 20_000  # продажи остались в итогах и ведомости

    r = web.post("/sellers", data={"action": f"restore:{sid}"}, auth=AUTH, follow_redirects=False)
    assert r.headers["location"] == f"/sellers/{sid}/card"
    s = db.seller(sid)
    assert s["active"] == 1 and s["fired_at"] is None and s["invite_code"]


def test_owner_warned_about_sales_of_fired(env, tmp_path):  # noqa: F811
    cfg, db = env
    cfg.max.owners = [1]
    sid = db.seller_id("Продавец04", create=False)
    db.fire(sid, date(2026, 10, 10))
    import_sales(tmp_path / "s.csv", [(oct_day(10), "Продавец04", 5_000, 1),
                                                 (oct_day(11), "Продавец04", 7_000, 1)], db, cfg)
    sender = Sender(db, cfg, None, dry_run=True)
    daily_run(db, cfg, sender, today=oct_day(11), force=True)  # продажи в день увольнения — его последняя смена
    assert not db.one("SELECT 1 FROM message_log WHERE kind='fired_sales'")
    daily_run(db, cfg, sender, today=oct_day(12), force=True)
    text = db.one("SELECT text FROM message_log WHERE kind='fired_sales'")["text"]
    assert text.startswith("В выгрузке 1С за 11.10 продажи уволенных: Продавец04 — 7 000 ₽.")


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    from awer_sales_bot.db import DB
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("CREATE TABLE sellers (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, role TEXT NOT NULL DEFAULT "
                "'seller', active INTEGER NOT NULL DEFAULT 1, first_name TEXT, max_user_id INTEGER UNIQUE, "
                "invite_code TEXT UNIQUE)")
    old.execute("INSERT INTO sellers(name) VALUES('Старый Продавец')")
    old.commit()
    old.close()
    db = DB(tmp_path / "old.db")
    db.fire(1)
    assert db.seller(1)["fired_at"] and db.seller(1)["active"] == 0


def test_id_command(env):  # noqa: F811
    cfg, db = env
    client = FakeMax()
    bot.handle(db, cfg, client, {"update_type": "message_created",
                                 "message": {"sender": {"user_id": 4242}, "body": {"text": "/id"}}})
    assert client.sent[0][1] == "Ваш user_id в MAX: 4242"


def test_test_card_gets_messages_but_not_payroll(env, tmp_path):  # noqa: F811
    """Тестовая карточка «Татьяна» (служебная запись 1С, просьба владельца 19.10.2026): план и утреннее сообщение
    есть, а в ведомости, средних по команде, плане новичка и «кого похвалить» её нет; импорт роль не сбрасывает."""
    from awer_sales_bot import metrics as M
    from awer_sales_bot.messages import daily
    from awer_sales_bot.planning import service
    cfg, db = env
    import_sales(tmp_path / "a.csv", [(oct_day(d), "Татьяна", 3_000, 1) for d in (1, 2)], db, cfg)
    sid = db.seller_id("Татьяна", create=False)
    assert db.seller(sid)["role"] == "system"
    nc_before = service.compute(db, cfg, "2026-11").store.newcomer_plan
    assert db.add_seller("Татьяна", "Татьяна", "test") == sid  # служебная → тестовая, без дубля
    assert db.seller(sid)["role"] == "test" and db.seller(sid)["active"] == 1
    import_sales(tmp_path / "b.csv", [(oct_day(3), "Татьяна", 3_000, 1)], db, cfg)
    assert db.seller(sid)["role"] == "test"  # выгрузка 1С не вернула её в служебные
    assert service.compute(db, cfg, "2026-11").store.newcomer_plan == pytest.approx(nc_before)
    service.freeze(db, cfg, "2026-10")
    assert service.frozen_plan(db, "2026-10", sid)  # план есть — сообщения как у продавца
    assert daily(db, cfg, sid, oct_day(3)).startswith("Доброе утро, Татьяна!")
    sellers_only = db.one("SELECT COALESCE(SUM(l.amount),0) s FROM sales_lines l JOIN sellers s ON s.id=l.seller_id "
                          "WHERE s.role='seller' AND l.day BETWEEN '2026-10-01' AND '2026-10-03'")["s"]
    assert M.team(db, oct_day(1), oct_day(3)).sales == pytest.approx(sellers_only)  # в средних по команде её нет
    rows = {r["seller_id"] for r in service.close_month(db, cfg, "2026-10")}
    assert sid not in rows  # в ведомость не идёт

"""Аудит В1, В2, В5, М1, М4: система не молчит — битый файл, пропущенный запуск, нет связи с MAX, ошибки MAX."""
import os
import threading
import time
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from awer_sales_bot import bot, health
from awer_sales_bot.config import MaxCfg
from awer_sales_bot.importers.onec_sales import import_inbox
from awer_sales_bot.jobs import Sender, daily_run, month_due
from awer_sales_bot.max_api import CA_FILE, MaxAuthError, MaxClient, MaxDenied, MaxError
from awer_sales_bot.planning import service
from awer_sales_bot.web import create_app
from test_flow import env  # noqa: F401
from test_motivation import FakeMax, write_sales


# ----------------------------------------------------------------------------- М1, М4: клиент MAX
def mock_client(handler) -> tuple[MaxClient, list]:
    calls = []

    def h(req: httpx.Request):
        calls.append(req)
        return handler(req, len(calls))

    c = MaxClient("tok", pause=0)
    c.http = httpx.Client(transport=httpx.MockTransport(h))
    return c, calls


def test_new_address_header_token_and_certificate():
    assert MaxCfg().base_url == "https://platform-api2.max.ru"
    assert MaxCfg(base_url="https://platform-api.max.ru").base_url == "https://platform-api2.max.ru"
    assert CA_FILE.is_file() and "BEGIN CERTIFICATE" in CA_FILE.read_text()
    c, calls = mock_client(lambda req, n: httpx.Response(200, json={"user_id": 1, "name": "План"}))
    assert c.me()["name"] == "План"
    req = calls[0]
    assert str(req.url) == "https://platform-api2.max.ru/me"
    assert req.headers["Authorization"] == "tok" and "access_token" not in str(req.url)


def test_errors_are_told_apart():
    c, _ = mock_client(lambda req, n: httpx.Response(401, json={"code": "verify.token", "message": "Invalid"}))
    with pytest.raises(MaxAuthError):
        c.me()
    c, _ = mock_client(lambda req, n: httpx.Response(403, json={"code": "chat.denied", "message": "denied"}))
    with pytest.raises(MaxDenied) as e:
        c.send("x", user_id=5)
    assert e.value.code == "chat.denied"
    # другой 403 (бот заблокирован, нет прав) — сбой, а не «продавец остановил бота» (аудит Р9)
    c, _ = mock_client(lambda req, n: httpx.Response(403, json={"code": "access.denied", "message": "blocked"}))
    with pytest.raises(MaxError) as e:
        c.send("x", user_id=5)
    assert not isinstance(e.value, MaxDenied)


def test_send_is_not_repeated_on_server_error_but_get_is():
    c, calls = mock_client(lambda req, n: httpx.Response(500, text="boom"))
    with pytest.raises(MaxError):
        c.send("Ваш план", user_id=5)
    assert len(calls) == 1  # повтор POST /messages = дубль сообщения продавцу

    c, calls = mock_client(lambda req, n: httpx.Response(502) if n == 1 else httpx.Response(200, json={"ok": 1}))
    assert c.me() == {"ok": 1} and len(calls) == 2


def test_send_is_repeated_when_connection_failed():
    def h(req, n):
        if n == 1:
            raise httpx.ConnectError("no route", request=req)
        return httpx.Response(200, json={"message": {}})
    c, calls = mock_client(h)
    c.send("Ваш план", user_id=5)
    assert len(calls) == 2

    def h2(req, n):
        raise httpx.ReadTimeout("slow", request=req)
    c, calls = mock_client(h2)
    with pytest.raises(MaxError):
        c.send("Ваш план", user_id=5)
    assert len(calls) == 1  # запрос ушёл, ответа нет — мог дойти, не повторяем


def test_poll_http_timeout_longer_than_poll():
    c, calls = mock_client(lambda req, n: httpx.Response(200, json={"updates": [], "marker": 1}))
    c.updates(timeout=60)
    assert calls[0].extensions["timeout"]["read"] >= 75
    assert "bot_stopped" in calls[0].url.params["types"]


# ----------------------------------------------------------------------------- В5: бот не молчит
def test_connect_waits_instead_of_dying(env):  # noqa: F811
    cfg, db = env
    stop = threading.Event()

    class Down:
        n = 0

        def me(self):
            self.n += 1
            if self.n == 2:
                stop.set()
                raise MaxAuthError("401", 401)
            raise httpx.ConnectError("no route")

    orig = threading.Event.wait
    threading.Event.wait = lambda self, t=None: orig(self, 0)
    try:
        assert bot.connect(db, Down(), "plan", stop) is None
    finally:
        threading.Event.wait = orig
    ok, text = health.bot_state(db, "plan")
    assert not ok and "не принял токен" in text


def test_failed_update_is_logged_and_marker_moves(env):  # noqa: F811
    cfg, db = env
    stop = threading.Event()

    class Max(FakeMax):
        def updates(self, marker, timeout=30):
            stop.set()
            return {"updates": [{"update_type": "message_created",
                                 "message": {"sender": {"user_id": 77}, "recipient": {"chat_type": "dialog"},
                                             "body": {"text": "/plan"}}}], "marker": 5}

    def broken(*a):
        raise RuntimeError("сбой")

    m = Max()
    bot.poll_forever(db, cfg, m, stop, handler=broken)
    assert db.get_kv("max_marker") == "5"
    r = db.one("SELECT * FROM message_log WHERE kind='incoming'")
    assert r["status"] == "error" and "/plan" in r["text"] and "сбой" in r["error"]
    assert m.sent and "Попробуйте ещё раз" in m.sent[0][1]
    assert health.bot_state(db, "plan")[0]


def test_groups_bots_and_stopped(env):  # noqa: F811
    cfg, db = env
    m = FakeMax()
    group = {"update_type": "message_created", "message": {"sender": {"user_id": 9}, "recipient": {"chat_type": "chat"},
                                                         "body": {"text": "/help"}}}
    from_bot = {"update_type": "message_created", "message": {"sender": {"user_id": 9, "is_bot": True},
                                                            "body": {"text": "/help"}}}
    bot.handle(db, cfg, m, group)
    bot.handle(db, cfg, m, from_bot)
    assert m.sent == []
    sid = db.add_seller("Новикова Мария", "Мария")
    with db.tx() as c:
        c.execute("UPDATE sellers SET max_user_id=501 WHERE id=?", (sid,))
    bot.handle(db, cfg, m, {"update_type": "bot_stopped", "user": {"user_id": 501}})
    assert db.one("SELECT status FROM message_log WHERE kind='bot_stopped' AND seller_id=?", sid)["status"] == "stopped"

    class Denied(FakeMax):
        def send(self, *a, **kw):
            raise MaxDenied("403 chat.denied", 403, "chat.denied")
    assert Sender(db, cfg, Denied()).to_seller("2026-10-17", "daily", sid, "план") == "stopped"


def test_health_line(env, monkeypatch):  # noqa: F811
    cfg, db = env
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    assert "нет токена" in health.line(db, cfg) and 'href="/bot"' in health.line(db, cfg)
    monkeypatch.setenv("MAX_BOT_TOKEN", "x")
    health.ok(db, "plan")
    now = datetime.now()
    assert health.bot_state(db, "plan", now)[0]
    assert not health.bot_state(db, "plan", now + timedelta(minutes=30))[0]  # давно не было связи
    health.error(db, "plan", "нет связи с MAX (ConnectError)")
    assert 'class="health bad"' in health.line(db, cfg)
    late = datetime.combine(date.today(), datetime.min.time()).replace(hour=15)
    assert not health.daily_state(db, cfg, late)[0]
    db.set_kv("daily_done", (date.today() - timedelta(days=1)).isoformat())
    assert health.daily_state(db, cfg, late)[0]
    assert "health" in TestClient(create_app(cfg, db)).get("/sellers", auth=("admin", "pw")).text


# ----------------------------------------------------------------------------- В1: битый файл
def test_bad_file_does_not_stop_import(env, monkeypatch):  # noqa: F811
    cfg, db = env
    old = time.time() - 600
    good = write_sales(cfg.onec.inbox / "sales_1017.csv", [(date(2026, 10, 17), "Продавец04", 5600, 1)])
    bad = cfg.onec.inbox / "owner_notes.csv"
    bad.write_text("что-то;не то\n1;2\n", encoding="utf-8")
    fresh = cfg.onec.inbox / "sales_1018.csv"
    fresh.write_text("пишется", encoding="utf-8")
    for f in (good, bad):
        os.utime(f, (old, old))
    reps = {r.file: r for r in import_inbox(db, cfg)}
    assert reps["sales_1017.csv"].lines == 1 and reps["owner_notes.csv"].error
    assert fresh.exists()  # моложе минуты — 1С может ещё дописывать, заберём в следующий раз
    assert not good.exists() and not bad.exists()
    assert len(list((cfg.onec.archive / "bad").glob("*owner_notes.csv"))) == 1

    cfg.max.owners = [900]
    bad.write_text("что-то;не то\n", encoding="utf-8")
    os.utime(bad, (old, old))
    daily_run(db, cfg, Sender(db, cfg, None, dry_run=True), today=date(2026, 10, 18), force=True)
    r = db.one("SELECT text FROM message_log WHERE kind LIKE 'bad_file:%'")
    assert "owner_notes.csv" in r["text"] and "archive/bad" in r["text"]


def test_upload_bad_file(env, monkeypatch):  # noqa: F811
    cfg, db = env
    monkeypatch.setenv("UPLOAD_TOKEN", "tok")
    web = TestClient(create_app(cfg, db))
    r = web.post("/api/upload", content="а;б\n1;2\n".encode(), headers={"X-Token": "tok", "X-Filename": "x.csv"})
    assert r.status_code == 422 and "не распознан" in r.text
    assert list((cfg.onec.archive / "bad").glob("*x.csv")) and not list(cfg.onec.inbox.glob("*"))


# ----------------------------------------------------------------------------- В2: пропущенный запуск
def test_missed_first_of_month_is_caught_up(env):  # noqa: F811
    cfg, db = env
    cfg.max.owners = [900]
    service.freeze(db, cfg, "2026-10")

    def october(days, name):  # выгрузка ОРП по обоим магазинам — как файлы обработки 1С
        for store in ("ГОРОД", "МСЛ"):
            f = write_sales(cfg.onec.inbox / f"{name}_{store}.csv",
                            [(date(2026, 10, d), "Продавец04", 5600, 1) for d in days])
            f.write_text(f.read_text(encoding="utf-8").replace("ГОРОД_склад", f"{store}_склад"), encoding="utf-8")
            os.utime(f, (time.time() - 600,) * 2)

    october(range(1, 31), "s")
    nov2 = date(2026, 11, 2)
    assert month_due(db, cfg, nov2)  # план ноября не зафиксирован, октябрь не закрыт — неважно, что сегодня 2-е
    daily_run(db, cfg, Sender(db, cfg, None, dry_run=True), today=nov2, force=True)
    # за 31.10 выгрузки нет — итоги и ставки по неполным данным не считаем (аудит С2, Д5), план ноября ждёт (Д4)
    assert not db.one("SELECT 1 FROM results WHERE month='2026-10'") and month_due(db, cfg, nov2)
    assert not db.one("SELECT 1 FROM plans WHERE month='2026-11'")
    assert "31.10" in db.one("SELECT text FROM message_log WHERE kind='month_close_wait'")["text"]
    assert "31.10" in db.one("SELECT text FROM message_log WHERE kind='plan_wait'")["text"]

    october([31], "s31")
    daily_run(db, cfg, Sender(db, cfg, None, dry_run=True), today=nov2, force=True)
    assert db.one("SELECT 1 FROM results WHERE month='2026-10'") and service.is_final(db, "2026-10")
    assert db.one("SELECT 1 FROM plans WHERE month='2026-11'") and not month_due(db, cfg, nov2)


def test_plan_frozen_on_third_and_trial_month_not_closed(env):  # noqa: F811
    """Выгрузки за последний день так и нет — план фиксируется 3-го. Пробный месяц (до first_month) не
    закрывается: итогов со ставкой за него нет (решение владельца 19.10.2026)."""
    cfg, db = env
    cfg.max.owners = [900]
    cfg.first_month = "2026-11"
    service.freeze(db, cfg, "2026-10")
    write = write_sales(cfg.onec.inbox / "s.csv", [(date(2026, 10, 30), "Продавец04", 5600, 1)])
    os.utime(write, (time.time() - 600,) * 2)
    daily_run(db, cfg, Sender(db, cfg, None, dry_run=True), today=date(2026, 11, 3), force=True)
    assert db.one("SELECT 1 FROM plans WHERE month='2026-11'")
    assert not db.one("SELECT 1 FROM results WHERE month='2026-10'")
    assert not db.one("SELECT 1 FROM message_log WHERE kind IN ('month_close_wait', 'month_result')")
    assert not month_due(db, cfg, date(2026, 11, 3))


def test_daily_done_and_no_duplicates_for_owner(env):  # noqa: F811
    cfg, db = env
    cfg.max.owners = [900]
    m = FakeMax()
    s = Sender(db, cfg, m)
    s.GAP = 0
    f = write_sales(cfg.onec.inbox / "s.csv", [(date(2026, 10, 17), "Продавец04", 5600, 1)])
    os.utime(f, (time.time() - 600,) * 2)
    for _ in range(2):  # догон после расписания — тот же день ещё раз
        daily_run(db, cfg, s, today=date(2026, 10, 18), force=True)
    assert db.get_kv("daily_done") == "2026-10-17"
    kinds = [r["kind"] for r in db.q("SELECT kind FROM message_log WHERE max_user_id=900 AND status='ok'")]
    assert kinds.count("owner_daily") == 1 and kinds.count("missing_day") == 1


def test_onec_error_file_reaches_owner(env):  # noqa: F811
    cfg, db = env
    cfg.max.owners = [900]
    err = cfg.onec.inbox / "error_20261018-020000_sales.txt"
    err.write_text("sales: {Запрос}: Поле не найдено \"Т.Продавец\"\n", encoding="utf-8-sig")
    part = cfg.onec.inbox / "sales_20261017_20261017.csv.part"  # 1С ещё пишет — не трогаем
    part.write_text("Дата;Склад", encoding="utf-8")
    old = time.time() - 600
    os.utime(err, (old, old))
    os.utime(part, (old, old))
    daily_run(db, cfg, Sender(db, cfg, None, dry_run=True), today=date(2026, 10, 18), force=True)
    r = db.one("SELECT text FROM message_log WHERE kind LIKE 'bad_file:error_%'")
    assert "Поле не найдено" in r["text"] and "1С не смогла выгрузить" in r["text"]
    assert part.exists() and not err.exists()


def test_empty_day_is_not_sent_to_sellers(env):  # noqa: F811
    """После 13:00 без единой выгрузки за день продавцам не уходит «продаж нет» — только владельцу (аудит С2)."""
    cfg, db = env
    cfg.max.owners = [900]
    m = FakeMax()
    s = Sender(db, cfg, m)
    s.GAP = 0
    sid = db.seller_id("Продавец04", create=False)
    with db.tx() as c:
        c.execute("UPDATE sellers SET max_user_id=501 WHERE id=?", (sid,))
    service.freeze(db, cfg, "2026-10")
    daily_run(db, cfg, s, today=date(2026, 10, 18), force=True)
    # утреннего «продаж нет» нет; «план на месяц» досылается подключённому — это не данные дня (аудит Р1)
    assert [t.split("\n")[0].split(" (")[0] for uid, t, _ in m.sent if uid == 501] == [
        "**Как читать сообщения бота**", "Продавец04, ваш план на октябрь"]
    assert not db.one("SELECT 1 FROM message_log WHERE kind IN ('daily', 'owner_daily')")  # Р6: и сводки нет
    assert db.get_kv("daily_done") is None  # догон повторит, когда выгрузка придёт
    assert "не отправлена" in db.one("SELECT text FROM message_log WHERE kind='missing_day'")["text"]


def test_closed_day_is_not_a_gap(env):  # noqa: F811
    """Магазины не работали (25.06.2026): день не пропуск данных — месяц не досчитывается, выгрузку не ждём,
    продавцам «продаж нет» не шлём; продажи из 1С за этот день главнее отметки."""
    cfg, db = env
    cfg.max.owners = [900]
    m = FakeMax()
    s = Sender(db, cfg, m)
    s.GAP = 0
    sid = db.seller_id("Продавец04", create=False)
    with db.tx() as c:
        c.execute("UPDATE sellers SET max_user_id=501 WHERE id=?", (sid,))
    service.freeze(db, cfg, "2026-10")
    f = write_sales(cfg.onec.inbox / "s.csv", [(date(2026, 10, 16), "Продавец04", 5600, 1)])
    os.utime(f, (time.time() - 600,) * 2)
    import_inbox(db, cfg)
    web = TestClient(create_app(cfg, db))
    closed = date(2026, 10, 17)
    assert db.data_gaps(date(2026, 10, 16)) == []  # 16.10 — первый день истории, пропусков до него нет
    assert db.data_gaps(closed) == [closed.isoformat()]
    assert f'name=add value="{closed}"' in web.get("/closed", auth=("admin", "pw")).text  # предлагает отметить

    assert db.covered_days("2026-10") == 1
    r = web.post("/closed", data={"day": "17.10.2026"}, auth=("admin", "pw"), follow_redirects=False)
    assert r.status_code == 303 and db.store_closed(closed) and db.covered_days("2026-10") == 2
    page = web.get("/closed", auth=("admin", "pw")).text
    assert f'name=remove value="{closed}"' in page and f'name=add value="{closed}"' not in page
    assert db.history(cfg.norm_shifts).coverage["2026-10"] == pytest.approx(2 / 31)
    assert not db.day_closed(closed, cfg.stores)  # выгрузку за этот день бот не ждёт
    daily_run(db, cfg, s, today=date(2026, 10, 18), force=True)
    # сводки за день нет ни продавцу, ни владельцу (урок недели — про сегодня, он уходит как обычно)
    assert not db.one("SELECT 1 FROM message_log WHERE kind IN ('daily','missing_day','owner_daily')")
    assert db.get_kv("daily_done") == closed.isoformat()  # догон не повторяет

    web.post("/closed", data={"remove": closed.isoformat()}, auth=("admin", "pw"))
    assert not db.store_closed(closed) and db.data_gaps(closed) == [closed.isoformat()]
    assert "Магазины были закрыты" in web.get("/closed", auth=("admin", "pw")).text
    db.set_store_closed(closed, cfg.stores)
    f = write_sales(cfg.onec.inbox / "s2.csv", [(closed, "Продавец04", 3000, 1)])
    os.utime(f, (time.time() - 600,) * 2)
    import_inbox(db, cfg)
    assert not db.store_closed(closed)  # пришли продажи — отметка снялась
    assert "не отмечено" in web.post("/closed", data={"day": "мусор"}, auth=("admin", "pw")).text.lower()


def test_foreign_warehouse_is_not_revenue(env):  # noqa: F811
    cfg, db = env
    cfg.max.owners = [900]
    f = write_sales(cfg.onec.inbox / "s.csv", [(date(2026, 10, 17), "Продавец04", 5600, 1),
                                               (date(2026, 10, 17), "Продавец04", 100000, 1)])
    lines = f.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace("ГОРОД_склад", "ЦС Склад")
    f.write_text("\n".join(lines), encoding="utf-8")
    os.utime(f, (time.time() - 600,) * 2)
    daily_run(db, cfg, Sender(db, cfg, None, dry_run=True), today=date(2026, 10, 18), force=True)
    assert db.one("SELECT SUM(amount) a FROM sales_lines")["a"] == 5600
    assert db.one("SELECT COUNT(*) n FROM sales_lines WHERE store NOT IN ('Город','МСЛ')")["n"] == 0
    text = db.one("SELECT text FROM message_log WHERE kind LIKE 'foreign:%'")["text"]
    assert "ЦС Склад — 100 000" in text and "store_map" in text


def test_command_menu():
    from awer_sales_bot.config import Settings
    cfg = Settings()
    assert [n for n, _ in bot.commands(cfg, "plan")] == ["plan", "baza", "help"]  # второго бота нет
    assert [n for n, _ in bot.commands(cfg, "training")] == ["baza", "help"]
    c, calls = mock_client(lambda req, n: httpx.Response(200, json={"commands": []}))
    c.set_commands(bot.commands(cfg, "plan"))
    assert calls[0].method == "PATCH" and calls[0].url.path == "/me/commands"
    assert b'"name":"plan"' in calls[0].content.replace(b" ", b"")


def test_wrong_codes_are_limited(env):  # noqa: F811
    cfg, db = env
    m = FakeMax()
    sid = db.add_seller("Новикова Мария", "Мария")
    code = db.new_invite(sid)
    for i in range(bot.CODE_TRIES):
        bot.handle(db, cfg, m, {"update_type": "bot_started", "user": {"user_id": 666}, "payload": f"BAD{i:03d}"})
    assert m.sent[-1][1].startswith("Код не найден")
    bot.handle(db, cfg, m, {"update_type": "bot_started", "user": {"user_id": 666}, "payload": code})
    assert m.sent[-1][1] == bot.BLOCKED and db.seller(sid)["max_user_id"] is None  # даже верный код — через час
    bot.handle(db, cfg, m, {"update_type": "bot_started", "user": {"user_id": 501}, "payload": code})
    assert db.seller(sid)["max_user_id"] == 501  # другой человек не заблокирован


def test_set_role_typo_does_not_create_seller(env, tmp_path):  # noqa: F811
    from awer_sales_bot import cli
    cfg, db = env
    conf = tmp_path / "c.yaml"
    conf.write_text(f"db_path: {cfg.db_path}\n", encoding="utf-8")
    before = len(db.sellers(active_only=False))
    with pytest.raises(SystemExit):
        cli.main(["-c", str(conf), "set-role", "Продавец 04", "admin"])
    assert len(db.sellers(active_only=False)) == before


def test_db_is_safe_across_threads(env):  # noqa: F811
    cfg, db = env
    errors = []

    def work(k):
        try:
            for i in range(50):
                with db.tx() as c:
                    c.execute("INSERT INTO kv(k,v) VALUES(?,?)", (f"t{k}-{i}", "x"))
                    db.q("SELECT COUNT(*) FROM kv")
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and db.one("SELECT COUNT(*) n FROM kv WHERE k LIKE 't%'")["n"] == 200



# ----------------------------------------------------------------------------- Р1, Р2, Р3, Д2, Д3
class FlakyMax(FakeMax):
    """MAX, который отвечает ошибкой нужному получателю, пока broken."""
    def __init__(self, broken: set[int]):
        super().__init__()
        self.broken = broken

    def send(self, text, user_id=None, buttons=None, **kw):
        if user_id in self.broken:
            raise MaxError("502 Bad Gateway")
        return super().send(text, user_id, buttons)


def _link(db, names_uids):
    with db.tx() as c:
        for name, uid in names_uids:
            c.execute("UPDATE sellers SET max_user_id=? WHERE id=?", (uid, db.seller_id(name, create=False)))


def test_month_messages_resent_and_daily_done_only_when_all_sent(env):  # noqa: F811
    cfg, db = env
    _link(db, [("Продавец04", 501), ("Продавец05", 502)])
    service.freeze(db, cfg, "2026-10")
    f = write_sales(cfg.onec.inbox / "s.csv", [(date(2026, 10, 17), n, 5600, 1) for n in ("Продавец04", "Продавец05")])
    os.utime(f, (time.time() - 600,) * 2)
    m = FlakyMax({502})
    s = Sender(db, cfg, m)
    s.GAP = 0
    daily_run(db, cfg, s, today=date(2026, 10, 18), force=True)
    assert db.get_kv("daily_done") is None and db.get_kv("daily_sent") == "2026-10-17|1|2"
    ok, text = health.daily_state(db, cfg, now=datetime(2026, 10, 18, 11, 0))
    assert not ok and "ушло 1 из 2" in text
    m.broken.clear()  # связь вернулась — догон дошлёт только тому, кому не ушло
    daily_run(db, cfg, s, today=date(2026, 10, 18), force=True)
    to502 = [t.split("\n")[0] for uid, t, _ in m.sent if uid == 502]
    assert to502[0] == "**Как читать сообщения бота**" and to502[1].startswith("Продавец05, ваш план на октябрь")
    assert to502[2].startswith("Доброе утро")
    first = [t for uid, t, _ in m.sent if uid == 501]
    assert len(first) == len(set(first))  # 501 ничего не получил второй раз
    assert db.get_kv("daily_done") == "2026-10-17"


def test_error_in_one_seller_does_not_stop_others(env, monkeypatch):  # noqa: F811
    from awer_sales_bot import messages
    cfg, db = env
    cfg.max.owners = [900]
    _link(db, [("Продавец04", 501), ("Продавец05", 502)])
    service.freeze(db, cfg, "2026-10")
    f = write_sales(cfg.onec.inbox / "s.csv", [(date(2026, 10, 17), n, 5600, 1) for n in ("Продавец04", "Продавец05")])
    os.utime(f, (time.time() - 600,) * 2)
    bad = db.seller_id("Продавец04", create=False)
    real = messages.daily

    def daily(db_, cfg_, sid, day):
        if sid == bad:
            raise ZeroDivisionError("битые данные")
        return real(db_, cfg_, sid, day)
    monkeypatch.setattr(messages, "daily", daily)
    m = FakeMax()
    s = Sender(db, cfg, m)
    s.GAP = 0
    daily_run(db, cfg, s, today=date(2026, 10, 18), force=True)
    assert any(uid == 502 and t.startswith("Доброе утро") for uid, t, _ in m.sent)
    assert "Продавец04 — утреннее сообщение" in db.one("SELECT text FROM message_log WHERE kind='send_fail'")["text"]
    assert db.get_kv("daily_done") == "2026-10-17"  # ошибка в данных — не повод повторять каждые 15 минут


def test_role_on_freeze_day_and_preliminary_results(env):  # noqa: F811
    """Повышение до старшего после фиксации не добавляет доплату за этот месяц (Д3); итоги по кнопке до конца
    месяца — предварительные, бот их потом пересчитает (Д2)."""
    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    sid = db.seller_id("Продавец04", create=False)
    with db.tx() as c:
        c.execute("UPDATE sellers SET role='admin' WHERE id=?", (sid,))
    rows = {r["seller_id"]: r for r in service.close_month(db, cfg, "2026-10")}
    assert rows[sid]["role"] == "seller" and rows[sid]["rate"] < cfg.params.base_rate + cfg.params.admin_bonus
    assert not service.is_final(db, "2026-10")
    assert month_due(db, cfg, date(2026, 11, 1))  # предварительные итоги не отключают закрытие месяца


def test_dry_run_does_not_touch_real_database(env, tmp_path, monkeypatch, capsys):  # noqa: F811
    from awer_sales_bot import cli
    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    f = write_sales(cfg.onec.inbox / "s.csv", [(date(2026, 10, 31), "Продавец04", 5600, 1)])
    os.utime(f, (time.time() - 600,) * 2)
    conf = tmp_path / "config.yaml"
    conf.write_text(f"db_path: {cfg.db_path.as_posix()}\nonec:\n  inbox: {cfg.onec.inbox.as_posix()}\n"
                    f"  archive: {cfg.onec.archive.as_posix()}\n", encoding="utf-8")
    cli.main(["-c", str(conf), "daily", "--dry-run", "--force", "--today", "2026-11-05"])
    assert "копии базы" in capsys.readouterr().out
    assert f.exists()  # файл 1С не забран
    assert not db.one("SELECT 1 FROM plans WHERE month='2026-11'") and not db.one("SELECT 1 FROM message_log")


def test_hello_is_not_a_bad_code_and_guide_goes_once(env, monkeypatch):  # noqa: F811
    """Аудит Р8: «привет» не съедает попытки кода; повтор ссылки с кодом после подключения — «вы уже подключены».
    Памятка — при подключении и больше не повторяется утром."""
    cfg, db = env
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    m = FakeMax()
    say = lambda text: bot.handle(db, cfg, m, {"update_type": "message_created",  # noqa: E731
                                                "message": {"sender": {"user_id": 601}, "body": {"text": text}}})
    for _ in range(6):
        say("привет")
    assert m.sent[-1][1].startswith("Здравствуйте! Отправьте код из 6 знаков")
    sid = db.seller_id("Продавец04", create=False)
    code = db.new_invite(sid)
    say(code)
    assert m.sent[-2][1].startswith("Готово") and m.sent[-1][1].startswith("**Как читать сообщения бота**")
    bot.handle(db, cfg, m, {"update_type": "bot_started", "user": {"user_id": 601}, "payload": code})
    assert m.sent[-1][1].startswith("Вы уже подключены")
    say("/help")
    assert m.sent[-1][1].startswith("**Как читать сообщения бота**")
    service.freeze(db, cfg, "2026-10")
    s = Sender(db, cfg, m)
    s.GAP = 0
    n = len(m.sent)
    daily_run(db, cfg, s, today=date(2026, 10, 18), force=True)
    assert not any(t.startswith("**Как читать") for _, t, _ in m.sent[n:])


def test_second_bot_instance_is_refused(env, tmp_path):  # noqa: F811
    """Аудит Р4: замок рядом с базой — второй процесс бота не запускается."""
    import subprocess
    import sys
    cfg, _ = env
    assert bot.single_instance(cfg)
    code = ("import sys; from pathlib import Path; from awer_sales_bot.config import Settings; "
            "from awer_sales_bot import bot; sys.exit(0 if bot.single_instance(Settings(db_path=Path(sys.argv[1]))) else 3)")
    r = subprocess.run([sys.executable, "-c", code, str(cfg.db_path)], capture_output=True)
    assert r.returncode == 3, r.stderr
    bot._LOCK.close()
    bot._LOCK = None

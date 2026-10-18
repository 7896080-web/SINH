"""Сквозной сценарий: история → выгрузка 1С за дни октября → план → сообщение → админка."""
import json
import random
from datetime import date, timedelta
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from awer_sales_bot.config import Settings
from awer_sales_bot.db import DB
from awer_sales_bot.importers.onec_sales import import_file
from awer_sales_bot.jobs import Sender, daily_run
from awer_sales_bot.messages import daily
from awer_sales_bot.planning import service
from awer_sales_bot.web import create_app

FIX = Path(__file__).parent / "fixtures" / "history_anon.json"
CATS = [("Джинсы", 5600), ("Рубашка Д/р", 3600), ("Ремень", 1900), ("Футболка-O", 2100), ("Куртка демисезон", 9500)]


def make_export(folder: Path, days: list[date], sellers: list[str]) -> tuple[Path, Path]:
    """Две выгрузки как из обработки 1С: строки ОРП и шапки ОРП."""
    rnd = random.Random(1)
    lines = ["Дата;Склад;Документ;Продавец;Номенклатура;Номенклатурная группа;Количество;Сумма;Скидка"]
    docs = ["Дата;Магазин;Касса;Документ;Чеков;Чеков возврата;Выручка"]
    for d in days:
        for store, sklad in (("ГОРОД", "ГОРОД_склад"), ("МСЛ", "МСЛ_склад")):
            rev, items, doc = 0.0, 0, f"КР{d:%m%d}{store[:1]}"
            for s in sellers:
                for _ in range(rnd.randint(2, 6)):
                    cat, price = rnd.choice(CATS)
                    disc = price * 0.1 if rnd.random() < 0.2 else 0
                    lines.append(f"{d:%d.%m.%Y};{sklad};{doc};{s};Арт {cat};{cat};1;{price - disc:.2f};{disc:.2f}")
                    rev += price - disc
                    items += 1
            lines.append(f"{d:%d.%m.%Y};{sklad};{doc};Wildberries;x;x;1;9999;0")  # исключается
            docs.append(f"{d:%d.%m.%Y};{store};P1 {store}_Касса_Основная;{doc};{max(1, items // 2)};0;{rev:.2f}")
    a, b = folder / "orp_lines_test.csv", folder / "orp_docs_test.csv"
    a.write_text("\n".join(lines), encoding="utf-8")
    b.write_text("\n".join(docs), encoding="utf-8")
    return a, b


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = Settings(db_path=tmp_path / "t.db")
    cfg.onec.inbox = tmp_path / "inbox"
    cfg.onec.archive = tmp_path / "archive"
    cfg.onec.inbox.mkdir()
    db = DB(cfg.db_path)
    d = json.loads(FIX.read_text(encoding="utf-8"))
    with db.tx() as c:
        for m, sellers in d["months"].items():
            for name, v in sellers.items():
                sid = db.seller_id(name)
                c.execute("INSERT INTO monthly_history(month,seller_id,sales,fte) VALUES(?,?,?,?)",
                          (m, sid, v["sales"], v["fte"]))
        for m, cov in d["coverage"].items():
            c.execute("INSERT INTO month_coverage VALUES(?,?)", (m, cov))
        for name, role in d["roles"].items():
            c.execute("UPDATE sellers SET role=? WHERE name=?", (role, name))
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    return cfg, db


def test_flow(env, tmp_path):
    cfg, db = env
    mp = service.freeze(db, cfg, "2026-10")
    plan04 = next(r for r in mp.rows if r.name == "Продавец04").plan_final
    assert plan04 == pytest.approx(561_764, abs=1)

    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(17)]
    lines_f, docs_f = make_export(cfg.onec.inbox, days, ["Продавец04", "Админ2"])
    rep = import_file(docs_f, db, cfg)
    assert rep.kind == "docs" and len(rep.days) == 34
    rep = import_file(lines_f, db, cfg)
    assert rep.kind == "lines" and rep.skipped == 34 and len(rep.days) == 34
    assert {d[1] for d in rep.days} == {"Город", "МСЛ"}
    assert db.day_closed(date(2026, 10, 17), cfg.stores) == []

    sid = db.seller_id("Продавец04", create=False)
    st = service.status(db, cfg, sid, date(2026, 10, 17))
    assert st.worked == 17 and st.planned_shifts == 15
    assert st.plan_month == pytest.approx(plan04, abs=1)
    text = daily(db, cfg, sid, date(2026, 10, 17))
    assert "Базовый план" in text and "средний чек" in text and "Вещей за смену" in text

    # ручные правки меняют план
    service.save_month_settings(db, "2026-11", trend_manual=1.0, step=0.05, step_bonus=3000)
    p = service.month_params(db, cfg, "2026-11")
    assert p.step == 0.05 and p.step_bonus == 3000 and p.admin_bonus == 13_000
    service.save_plan_override(db, "2026-10", sid, 500_000)
    assert next(r for r in service.compute(db, cfg, "2026-10").rows if r.seller_id == sid).plan_final == 500_000

    # рассылка (dry-run) — день закрыт, сообщения уходят в журнал
    out = daily_run(db, cfg, Sender(db, cfg, None, dry_run=True), today=date(2026, 10, 18))
    assert out == "sent"

    client = TestClient(create_app(cfg, db))
    r = client.get("/month/2026-10", auth=("admin", "pw"))
    assert r.status_code == 200 and "Коэффициент год к году" in r.text
    r = client.post("/month/2026-10/settings", data={"trend_manual": "0,9", "step": "10", "step_bonus": "5 000"},
                    auth=("admin", "pw"), follow_redirects=False)
    assert r.status_code == 303
    assert service.trend_manual(db, "2026-10") == 0.9
    assert client.get("/sellers", auth=("admin", "pw")).status_code == 200
    assert client.get("/month/2026-10", auth=("admin", "wrong")).status_code == 401


def test_upload_csv(env, monkeypatch):
    cfg, db = env
    monkeypatch.setenv("UPLOAD_TOKEN", "tok")
    csv_text = ("Дата;Склад;Документ;Продавец;Номенклатура;Номенклатурная группа;Количество;Сумма;Скидка\n"
                "17.10.2026;ГОРОД_склад;КР1;Продавец04;Джинсы X;Джинсы;1;5600;400\n"
                "17.10.2026;ГОРОД_склад;КР1;Продавец04;Ремень Y;Ремень;1;1900;0\n"
                "17.10.2026;МСЛ_склад;КР2;Админ2;Рубашка;Рубашка Д/р;-1;-3600;0\n")
    client = TestClient(create_app(cfg, db))
    assert client.post("/api/upload", content=csv_text.encode(), headers={"X-Token": "bad"}).status_code == 401
    r = client.post("/api/upload", content=csv_text.encode("utf-8"), headers={"X-Token": "tok", "X-Filename": "s.csv"})
    assert r.status_code == 200 and r.json()["lines"] == 3
    assert db.day_closed(date(2026, 10, 17), cfg.stores) == []

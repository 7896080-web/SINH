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


def make_export(folder: Path, days: list[date], sellers: list[str], receipts: bool = True) -> tuple[Path, Path]:
    """Выгрузки как из обработки 1С: строки продаж (из чеков или из ОРП) и шапки ОРП."""
    rnd = random.Random(1)
    lines = ["Дата;Время;Склад;Документ;Чек;Продавец;Номенклатура;Номенклатурная группа;Количество;Цена;Сумма;"
             "Скидка;Ручная скидка %;Дисконтная карта"]
    docs = ["Дата;Магазин;Касса;Документ;Чеков;Чеков возврата;Выручка"]
    n = 0
    for d in days:
        for store, sklad in (("ГОРОД", "ГОРОД_склад"), ("МСЛ", "МСЛ_склад")):
            rev, checks, doc = 0.0, 0, f"КР{d:%m%d}{store[:1]}"
            for s in sellers:
                for _ in range(rnd.randint(2, 4)):
                    n += 1
                    checks += 1
                    card = f"300{rnd.randint(100, 999)}" if rnd.random() < 0.5 else ""
                    hh = rnd.randint(10, 21)
                    for _ in range(rnd.randint(1, 3)):
                        cat, price = rnd.choice(CATS)
                        manual = 10 if rnd.random() < 0.15 else 0
                        summa = price * (1 - manual / 100)
                        rec = f"Ч{n}" if receipts else ""
                        tm = f"{hh}:15" if receipts else ""
                        lines.append(f"{d:%d.%m.%Y};{tm};{sklad};{doc};{rec};{s};Арт {cat};{cat};1;{price};{summa:.2f};"
                                     f";{manual};{card}")
                        rev += summa
            lines.append(f"{d:%d.%m.%Y};;{sklad};{doc};;;Арт x;Джинсы;1;5000;5000;;0;")  # без продавца
            lines.append(f"{d:%d.%m.%Y};;{sklad};{doc};;Wildberries;x;x;1;9999;9999;;0;")  # исключается
            docs.append(f"{d:%d.%m.%Y};{store};P1 {store}_Касса_Основная;{doc};{checks};0;{rev:.2f}")
    a, b = folder / "sales_test.csv", folder / "orp_docs_test.csv"
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
    assert plan04 == pytest.approx(547_706, abs=1)

    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(17)]
    lines_f, docs_f = make_export(cfg.onec.inbox, days, ["Продавец04", "Админ2"])
    rep = import_file(docs_f, db, cfg)
    assert rep.kind == "docs" and len(rep.days) == 34
    rep = import_file(lines_f, db, cfg)
    assert rep.kind == "lines" and rep.skipped == 34 and len(rep.days) == 34  # 34 строки Wildberries
    assert {d[1] for d in rep.days} == {"Город", "МСЛ"}
    assert db.day_closed(date(2026, 10, 17), cfg.stores) == []

    sid = db.seller_id("Продавец04", create=False)
    st = service.status(db, cfg, sid, date(2026, 10, 17))
    assert st.worked == 17 and st.planned_shifts == 18  # графика нет — норма 18 смен
    assert st.plan_month == pytest.approx(plan04, abs=1)
    text = daily(db, cfg, sid, date(2026, 10, 17))
    assert "Базовый план" in text and "Средний чек" in text and "Вещей в чеке" in text
    assert "(без продавца)" not in [r["name"] for r in db.sellers()]

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


def test_orp_mode_cards_stock_arrivals(env):
    """Без номеров чеков: покупки по картам, средний чек магазина; остатки, поступления, «битые» размеры."""
    from awer_sales_bot import metrics as M
    from awer_sales_bot.messages import owner_summary

    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(10)]
    lines_f, docs_f = make_export(cfg.onec.inbox, days, ["Продавец04", "Админ2"], receipts=False)
    import_file(docs_f, db, cfg)
    import_file(lines_f, db, cfg)
    sid = db.seller_id("Продавец04", create=False)
    m = M.seller(db, sid, days[0], days[-1])
    assert not m.has_receipts and m.card_purchases > 0 and m.card_avg > 0 and m.manual_discount > 0
    assert M.store(db, days[0], days[-1]).checks > 0
    assert M.unattributed(db, days[0], days[-1]) == 5000 * 20

    st = cfg.onec.inbox / "stock_20261010.csv"
    st.write_text("Дата;Склад;Номенклатура;Номенклатурная группа;Характеристика;Остаток\n"
                  "10.10.2026;ГОРОД_склад;Арт Джинсы;Джинсы;32;1\n"
                  "10.10.2026;ГОРОД_склад;Арт Ремень;Ремень;U;12\n"
                  "10.10.2026;ГОРОД_склад;Старая модель;Пиджак;50;3\n", encoding="utf-8")
    ar = cfg.onec.inbox / "arrivals_20261010.csv"
    ar.write_text("Дата;Склад;Документ;Номенклатура;Номенклатурная группа;Характеристика;Поступило;Бренд;Цвет\n"
                  "10.10.2026;ГОРОД_склад;Поступление 5;Куртка A;Куртка демисезон;50;2;МСЛ;Индиго\n"
                  "10.10.2026;ГОРОД_склад;Поступление 5;Куртка B;Куртка демисезон;52;3;Кензо;\n", encoding="utf-8")
    assert import_file(st, db, cfg).kind == "stock"
    assert import_file(ar, db, cfg).kind == "arrivals"
    assert M.arrivals_summary(db, days[-1]) == [("Куртка демисезон", 2, 5.0)]
    brands = {r["item"]: (r["brand"], r["color"]) for r in db.q("SELECT item, brand, color FROM arrivals")}
    assert brands == {"Куртка A": (None, "Индиго"), "Куртка B": ("Кензо", None)}  # «МСЛ» в 1С — не бренд
    assert M.broken_sizes(db, days[-1])[0][1] == "Арт Джинсы"
    assert M.stale_stock(db, days[-1]) == (0, 0.0)  # продаж в базе всего 10 дней — «90 дней без продаж» не знаем
    with db.tx() as c:  # история продаж длиннее 90 дней: «Старая модель» не продавалась, джинсы и ремень — да
        c.execute("INSERT INTO sales_lines(day,store,seller_id,item,category,qty,amount,source) "
                  "VALUES('2026-06-01','Город',?,'Арт Пиджак','Пиджак',1,9000,'old')", (sid,))
    assert M.stale_stock(db, days[-1]) == (1, 3.0)
    text = daily(db, cfg, sid, days[-1])
    assert "Покупка клиента с картой" in text and "Новые поступления" in text
    summ = owner_summary(db, cfg, days[-1])
    assert "без продавца" in summ and "Ручные скидки" in summ and "1–2 размера" in summ

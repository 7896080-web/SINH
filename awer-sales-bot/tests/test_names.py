"""Имена в 1С ↔ продавец ↔ MAX: другие написания и объединение одного человека, записанного по-разному."""
from datetime import date

from fastapi.testclient import TestClient

from awer_sales_bot import names
from awer_sales_bot.importers.onec_sales import import_file
from awer_sales_bot.web import create_app
from test_flow import env  # noqa: F401
from test_motivation import write_sales

AUTH = ("admin", "pw")


def test_similar_names():
    assert names.similar("Эльвина Абдулиннанова", "Абдулиннанова Эльвина")  # другой порядок слов
    assert names.similar("Абдулиннанова Эльвина Рустемовна", "Абдулиннанова Эльвина")  # с отчеством
    assert names.similar("Абдулинанова Эльвина", "Абдулиннанова Эльвина")  # опечатка
    assert not names.similar("Петрова Ольга", "Петрова Олеся")
    assert not names.similar("Иванова Анна", "Иванова Алла")
    assert not names.similar("Продавец01", "Продавец02")


def test_payroll_and_1c_spellings_merged(env, tmp_path):  # noqa: F811
    cfg, db = env
    old = db.seller_id("Эльвина Абдулиннанова")  # из ведомостей
    with db.tx() as c:
        c.execute("INSERT INTO monthly_history(month,seller_id,sales,fte) VALUES('2026-08',?,900000,1)", (old,))
        c.execute("INSERT INTO monthly_history(month,seller_id,sales,fte) VALUES('2026-09',?,800000,1)", (old,))
        c.execute("UPDATE sellers SET role='admin' WHERE id=?", (old,))
    import_file(write_sales(tmp_path / "s.csv", [(date(2026, 10, 17), "Абдулиннанова Эльвина", 50000, 5)]), db, cfg)
    new = db.seller_id("Абдулиннанова Эльвина", create=False)
    assert new != old
    with db.tx() as c:  # в 1С-записи уже подключён бот, есть месяц из ведомостей и план новичка
        c.execute("UPDATE sellers SET max_user_id=501, max_name='Эля' WHERE id=?", (new,))
        c.execute("INSERT INTO monthly_history(month,seller_id,sales,fte) VALUES('2026-09',?,700000,1)", (new,))
        c.execute("INSERT INTO plans(month,seller_id,plan_full,method,frozen_at) VALUES('2026-10',?,300000,"
                  "'newcomer','x')", (new,))
        c.execute("INSERT INTO plans(month,seller_id,plan_full,method,frozen_at) VALUES('2026-10',?,1100000,"
                  "'history','x')", (old,))

    web = TestClient(create_app(cfg, db))
    assert "похоже, есть повторы" in web.get("/sellers", auth=AUTH).text
    keep, drop = names.suspects(db)[0]
    assert keep["id"] == new and drop["id"] == old  # остаётся имя, как в 1С сейчас
    page = web.get("/sellers/names", auth=AUTH).text
    assert "Похоже, это один человек" in page and f"merge:{new}:{old}" in page

    r = web.post("/sellers/names", data={"action": f"merge:{new}:{old}"}, auth=AUTH)
    assert "Объединено" in r.text and "2026-09" in r.text
    s = db.seller(new)
    assert db.seller(old) is None and s["max_user_id"] == 501 and s["role"] == "admin"
    hist = {r["month"]: r["sales"] for r in db.q("SELECT month, sales FROM monthly_history WHERE seller_id=?", new)}
    assert hist == {"2026-08": 900000, "2026-09": 800000}  # месяц у обоих — большее, не сумма
    assert db.one("SELECT plan_full FROM plans WHERE month='2026-10' AND seller_id=?", new)["plan_full"] == 1100000
    assert db.seller_id("Эльвина Абдулиннанова", create=False) == new  # старое написание ведёт к нему
    assert names.suspects(db) == []

    # следующая выгрузка со старым написанием — не новый продавец, а тот же
    rep = import_file(write_sales(tmp_path / "s2.csv", [(date(2026, 10, 18), "Эльвина Абдулиннанова", 1000, 1)]),
                      db, cfg)
    assert not rep.unknown_sellers
    assert db.one("SELECT COUNT(DISTINCT seller_id) n FROM sales_lines WHERE day>='2026-10-17'")["n"] == 1


def test_aliases_and_not_duplicate(env):  # noqa: F811
    cfg, db = env
    web = TestClient(create_app(cfg, db))
    sid = db.add_seller("Новикова Мария", "Мария")
    other = db.add_seller("Новикова Мария Ивановна", "Мария")
    r = web.post("/sellers/names", data={"action": f"alias:{sid}", f"alias{sid}": "Новикова (Петрова) Мария"},
                 auth=AUTH)
    assert "теперь считается тем же продавцом" in r.text
    assert db.seller_id("Новикова (Петрова) Мария", create=False) == sid
    r = web.post("/sellers/names", data={"action": f"alias:{sid}", f"alias{sid}": "Новикова Мария Ивановна"},
                 auth=AUTH)
    assert "Не сделано" in r.text and "Объединить" in r.text  # занятое имя — это объединение, а не написание
    web.post("/sellers/names", data={"action": f"notdup:{sid}:{other}"}, auth=AUTH)
    assert names.suspects(db) == []
    web.post("/sellers/names", data={"action": "unalias:Новикова (Петрова) Мария"}, auth=AUTH)
    assert db.seller_id("Новикова (Петрова) Мария", create=False) is None

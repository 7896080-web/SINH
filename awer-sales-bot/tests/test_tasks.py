"""Задания: тесты, опросы, чек-листы — рассылка в тестовые часы, ответы кнопками и текстом, рейтинг."""
from datetime import date, datetime

from fastapi.testclient import TestClient

from awer_sales_bot import bot, tasks
from awer_sales_bot.jobs import Sender
from awer_sales_bot.web import create_app
from test_flow import env  # noqa: F401
from test_motivation import LESSONS_YAML, FakeMax

AUTH = ("admin", "pw")
TASKS_YAML = """
tests:
  - id: test-1
    title: Ткани
    questions:
      - {q: "Что такое эластан?", options: [Тянется, Греет], correct: 0, explain: Добавляет растяжимость}
      - {q: "Лён мнётся?", options: [Нет, Да], correct: 1, explain: Лён мнётся}
surveys:
  - id: opros-1
    title: Как прошёл день
    questions:
      - {q: "Что мешало?", options: [Размеры, Ничего], free_text: true}
checklists:
  - id: check-1
    title: Открытие
    items: [Свет, Примерочные]
"""
DAY = date(2026, 10, 20)


def make(env, tmp_path, monkeypatch):  # noqa: F811
    cfg, db = env
    (tmp_path / "lessons.yaml").write_text(LESSONS_YAML, encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text(TASKS_YAML, encoding="utf-8")
    cfg.training.lessons_path = tmp_path / "lessons.yaml"
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    s4, s5 = db.seller_id("Продавец04", create=False), db.seller_id("Продавец05", create=False)
    with db.tx() as c:
        c.execute("UPDATE sellers SET max_user_id=404 WHERE id=?", (s4,))
        c.execute("UPDATE sellers SET max_user_id=405 WHERE id=?", (s5,))
    db.set_schedule("2026-10", s4, {20, 21})
    db.set_schedule("2026-10", s5, {21, 22})  # 20-го не работает — задание не получит
    client = FakeMax()
    sender = Sender(db, cfg, client)
    sender.GAP = 0
    web = TestClient(create_app(cfg, db))
    return cfg, db, client, sender, web, s4


def press(db, cfg, client, payload, uid=404):
    client.sent.clear()
    bot.handle(db, cfg, client, {"update_type": "message_callback",
                                 "callback": {"callback_id": "c", "payload": payload, "user": {"user_id": uid}}})
    return client.answers[-1], [t for _, t, _ in client.sent]


def test_campaigns_hours_working_day_answers_and_rating(env, tmp_path, monkeypatch):  # noqa: F811
    cfg, db, client, sender, web, s4 = make(env, tmp_path, monkeypatch)
    for cid in ("test-1", "opros-1", "check-1"):
        r = web.post("/tasks", data={"content_id": cid, "audience": "all", "repeat": "daily", "start_day": "2026-10-20",
                                     "hour_from": "11:00", "hour_to": "13:00", "only_working": "on"}, auth=AUTH)
        assert "Рассылка создана" in r.text
    assert tasks.run_due(db, cfg, sender, datetime(2026, 10, 20, 10, 0)) == 0  # до тестовых часов
    assert tasks.run_due(db, cfg, sender, datetime(2026, 10, 20, 11, 30)) == 3  # 3 задания одному Продавец04
    assert tasks.run_due(db, cfg, sender, datetime(2026, 10, 20, 12, 30)) == 0  # повторно не шлём
    assert {u for u, _, _ in client.sent} == {404}
    texts = [t for _, t, _ in client.sent]
    assert texts[0].startswith("📝 Тест: **Ткани**") and texts[1].startswith("Вопрос 1 из 2")

    c1, c2, c3 = (r["id"] for r in db.q("SELECT id FROM campaigns ORDER BY id"))
    d = DAY.isoformat()
    (cb, note, _), msgs = press(db, cfg, client, f"t|{c1}|{d}|0|0")
    assert note == "Верно!" and msgs[0].startswith("✅ Верно!") and msgs[1].startswith("Вопрос 2 из 2")
    (cb, note, _), msgs = press(db, cfg, client, f"t|{c1}|{d}|1|0")
    assert msgs[0].startswith("Не совсем. Верный ответ: «Да».") and msgs[1] == "Тест пройден: 1 из 2. Спасибо!"
    assert press(db, cfg, client, f"t|{c1}|{d}|1|1")[0][1] == "Ответ уже принят"

    (cb, note, _), msgs = press(db, cfg, client, f"s|{c2}|{d}|0|-1")
    assert msgs == ["Напишите ответ одним сообщением."]
    client.sent.clear()
    bot.handle(db, cfg, client, {"update_type": "message_created",
                                 "message": {"sender": {"user_id": 404}, "body": {"text": "Не было 52 размера"}}})
    assert [t for _, t, _ in client.sent] == ["Записал, спасибо!", "Спасибо за ответы! Владелец их увидит."]

    (cb, note, replace), _ = press(db, cfg, client, f"c|{c3}|{d}|0|x")
    assert note == "Отмечено" and "1 из 2" in replace["text"]
    assert replace["attachments"][0]["payload"]["buttons"][0][0]["text"] == "✅ Свет"
    _, msgs = press(db, cfg, client, f"c|{c3}|{d}|done|x")
    assert msgs == ["Чек-лист «Открытие»: отмечено 1 из 2. Спасибо!"]
    assert press(db, cfg, client, f"t|{c1}|{d}|0|0", uid=405)[0][1] == "Кнопка устарела"  # чужое задание

    rows = {r["name"]: r for r in tasks.rating(db, "2026-10")}
    assert rows["Продавец04"]["test"] == 0.5 and rows["Продавец04"]["done"] == 3
    assert rows["Продавец04"]["score"] == (0.5 + 1.0) / 2
    page = web.get(f"/tasks/{c2}", auth=AUTH).text
    assert "Не было 52 размера" in page
    assert "Продавец04" in web.get("/rating?month=2026-10", auth=AUTH).text
    assert "Тесты, опросы и чек-листы приходят в бот «Обучение»" in web.get("/tasks", auth=AUTH).text


def test_store_audience_and_once(env, tmp_path, monkeypatch):  # noqa: F811
    from awer_sales_bot import crews
    cfg, db, client, sender, web, s4 = make(env, tmp_path, monkeypatch)
    crews.ensure_crews(db, cfg)
    c1, _ = crews.crews(db)
    with db.tx() as c:
        c.execute("UPDATE crews SET store='МСЛ', anchor='2026-10-14' WHERE id=?", (c1["id"],))
        c.execute("UPDATE sellers SET crew_id=? WHERE id=?", (c1["id"], s4))
    web.post("/tasks", data={"content_id": "check-1", "audience": "store:Город", "repeat": "once",
                             "start_day": "2026-10-20"}, auth=AUTH)
    assert tasks.run_due(db, cfg, sender, datetime(2026, 10, 20, 11, 30)) == 0  # он сегодня в МСЛ
    web.post("/tasks", data={"content_id": "check-1", "audience": "store:МСЛ", "repeat": "once",
                             "start_day": "2026-10-20"}, auth=AUTH)
    assert tasks.run_due(db, cfg, sender, datetime(2026, 10, 20, 11, 30)) == 1
    # 21.10 — среда: смена переехала в Город, «один раз» в МСЛ больше не шлём, а рассылку для Города он получает
    assert tasks.run_due(db, cfg, sender, datetime(2026, 10, 21, 11, 30)) == 1
    got = {(r["campaign_id"], r["day"]) for r in db.q("SELECT campaign_id, day FROM deliveries")}
    first, second = (r["id"] for r in db.q("SELECT id FROM campaigns ORDER BY id"))
    assert got == {(second, "2026-10-20"), (first, "2026-10-21")}

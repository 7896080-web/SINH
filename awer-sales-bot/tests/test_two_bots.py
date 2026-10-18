"""Два бота: «План» (план, показатели, сводка) и «Обучение» (уроки, квизы, программа новичка)."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from awer_sales_bot import bot, goals
from awer_sales_bot.importers.onec_sales import import_file
from awer_sales_bot.jobs import Sender, training_run
from awer_sales_bot.web import create_app
from test_flow import env  # noqa: F401
from test_motivation import LESSONS_YAML, FakeMax, write_sales

AUTH = ("admin", "pw")


def test_one_qr_connects_both_bots_and_messages_are_split(env, tmp_path, monkeypatch):  # noqa: F811
    cfg, db = env
    monkeypatch.setenv("MAX_TRAINING_BOT_TOKEN", "t-token")
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    cfg.max_training.bot_link = "https://max.ru/id9102000000_2_bot"
    (tmp_path / "lessons.yaml").write_text(LESSONS_YAML, encoding="utf-8")
    cfg.training.lessons_path = tmp_path / "lessons.yaml"
    plan, train = FakeMax(), FakeMax()

    sid = db.add_seller("Новикова Мария", "Мария")
    bot.handle(db, cfg, plan, {"update_type": "bot_started", "user": {"user_id": 501}, "payload": db.new_invite(sid)})
    assert plan.sent[0][1].startswith("Готово, Мария!")
    button = plan.sent[1][2][0][0]
    assert button["type"] == "link" and button["text"] == "Подключить обучение"
    code = button["url"].split("start=")[1]
    assert not any("Программа новичка" in t for _, t, _ in plan.sent)  # уроки — не в «План»

    web = TestClient(create_app(cfg, db))
    assert "Осталось подключить бот «Обучение»" in web.get(f"/sellers/{sid}/card", auth=AUTH).text

    bot.handle_training(db, cfg, train, {"update_type": "bot_started", "user": {"user_id": 9501}, "payload": code})
    assert db.seller(sid)["training_user_id"] == 9501
    assert train.sent[0][1].startswith("Готово, Мария! Здесь уроки, тесты и база знаний")
    assert train.sent[1][1].startswith("📚 **Программа новичка, рабочий день 1 из 2")
    assert "обучение ✅" in web.get("/sellers", auth=AUTH).text

    # уроки — в «Обучение», план — в «План»
    sid4 = db.seller_id("Продавец04", create=False)
    with db.tx() as c:
        c.execute("UPDATE sellers SET max_user_id=404, training_user_id=9404 WHERE id=?", (sid4,))
    rows = []
    for i in range(10):
        d = date(2026, 9, 22) + timedelta(days=i * 2)
        rows += [(d, "Продавец04", 4_000, 1), (d, "Продавец05", 12_000, 3)]
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)
    sender = Sender(db, cfg, plan, training_client=train)
    sender.GAP = 0
    plan.sent.clear()
    train.sent.clear()
    goals.ensure_week(db, cfg, date(2026, 10, 19))
    training_run(db, cfg, sender, date(2026, 10, 19))
    assert [t.splitlines()[0] for u, t, _ in train.sent if u == 9404][:2] == [
        "📚 **Урок недели: Образ 1**", "❓ Вопрос 1 из 1 к уроку «Образ 1»"]
    assert not [t for u, t, _ in plan.sent if u == 404]
    sender.to_seller("2026-10-18", "daily", sid4, "план на день")
    assert plan.sent[-1][:2] == (404, "план на день")

    # кнопка квиза приходит во второй бот
    bot.handle_training(db, cfg, train, {"update_type": "message_callback", "callback": {
        "callback_id": "c1", "payload": "quiz|obraz-1|0|0", "user": {"user_id": 9404}}})
    assert train.answers[-1][1] == "Верно!"

    # без подключения к «Обучению» урок не уходит в «План», а пишется «не привязан»
    sid5 = db.seller_id("Продавец05", create=False)
    with db.tx() as c:
        c.execute("UPDATE sellers SET max_user_id=405 WHERE id=?", (sid5,))
    assert sender.to_seller("2026-10-26", "lesson", sid5, "урок") == "no_user"

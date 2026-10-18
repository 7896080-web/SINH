"""База знаний в боте «Обучение» и карточки новинок с брендом и цветом из 1С."""
from datetime import date

from awer_sales_bot import bot, kb
from awer_sales_bot.config import Settings
from awer_sales_bot.importers.onec_sales import import_file
from awer_sales_bot.jobs import Sender, arrivals_run
from test_flow import env  # noqa: F401
from test_motivation import FakeMax

ARR = ("Дата;Склад;Документ;Номенклатура;Номенклатурная группа;Характеристика;Поступило;Бренд;Цвет\n"
       "19.10.2026;ГОРОД_склад;П1;Пуховик Норд;Пуховик;50;2;Nordway;чёрный\n"
       "19.10.2026;ГОРОД_склад;П1;Пуховик Норд;Пуховик;52;3;Nordway;чёрный\n"
       "19.10.2026;ГОРОД_склад;П1;Джемпер кашемир;Джемпер;M;4;;серый\n")


def real(env):  # noqa: F811
    cfg, db = env
    cfg.training.lessons_path = Settings().training.lessons_path  # content/ из репозитория
    return cfg, db


def test_search_and_sections(env):  # noqa: F811
    cfg, db = real(env)
    text, rows = kb.home(db, cfg)
    assert text.startswith("📖 **База знаний.**") and rows[0][0]["payload"] == "k|sec|tkani"
    assert kb.search(db, cfg, "кашемир")[0]["title"] == "Кашемир"
    assert any("Пух" in x["title"] or "Пуховик" in x["title"] for x in kb.search(db, cfg, "пуховика"))
    assert kb.answer_search(db, cfg, "трактор")[0].startswith("По запросу «трактор» ничего не нашёл.")


def test_training_bot_kb_and_views(env, monkeypatch):  # noqa: F811
    cfg, db = real(env)
    monkeypatch.setenv("MAX_TRAINING_BOT_TOKEN", "t")
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    sid = db.seller_id("Продавец04", create=False)
    with db.tx() as c:
        c.execute("UPDATE sellers SET training_user_id=9404 WHERE id=?", (sid,))
    train = FakeMax()
    bot.handle_training(db, cfg, train, {"update_type": "message_created",
                                         "message": {"sender": {"user_id": 9404}, "body": {"text": "мембрана"}}})
    text, buttons = train.sent[0][1], train.sent[0][2]
    assert text.startswith("Нашёл по запросу «мембрана»") and buttons[0][0]["payload"].startswith("k|art|")
    bot.handle_training(db, cfg, train, {"update_type": "message_callback", "callback": {
        "callback_id": "c", "payload": buttons[0][0]["payload"], "user": {"user_id": 9404}}})
    replaced = train.answers[-1][2]
    assert replaced["text"].startswith("📖 **Мембраны")
    assert db.one("SELECT COUNT(*) n FROM kb_views WHERE seller_id=?", sid)["n"] == 1
    train.sent.clear()
    bot.handle_training(db, cfg, train, {"update_type": "message_created",
                                         "message": {"sender": {"user_id": 9404}, "body": {"text": "/baza"}}})
    assert train.sent[0][1].startswith("📖 **База знаний.**")


def test_arrivals_card_with_brand_color(env, tmp_path):  # noqa: F811
    cfg, db = real(env)
    (tmp_path / "arrivals_1.csv").write_text(ARR, encoding="utf-8")
    import_file(tmp_path / "arrivals_1.csv", db, cfg)
    text, buttons = kb.arrivals_card(db, cfg, date(2026, 10, 19))
    assert "• Пуховик Норд — Nordway, чёрный (размеры: 50, 52)" in text or \
        "• Пуховик Норд — Nordway, чёрный (размеры: 52, 50)" in text
    assert "• Джемпер кашемир — серый (размеры: M)" in text
    assert buttons and all(b[0]["payload"].startswith("k|art|") for b in buttons)

    s4, s5 = db.seller_id("Продавец04", create=False), db.seller_id("Продавец05", create=False)
    with db.tx() as c:
        c.execute("UPDATE sellers SET max_user_id=404 WHERE id=?", (s4,))
        c.execute("UPDATE sellers SET max_user_id=405 WHERE id=?", (s5,))
    db.set_schedule("2026-10", s5, {21})  # 20-го у Продавец05 не рабочий день
    client = FakeMax()
    sender = Sender(db, cfg, client)
    sender.GAP = 0
    arrivals_run(db, cfg, sender, date(2026, 10, 20), date(2026, 10, 19))
    arrivals_run(db, cfg, sender, date(2026, 10, 20), date(2026, 10, 19))  # повтор — без дубля
    assert [u for u, t, _ in client.sent if t.startswith("🆕 **Новинки 19.10**")] == [404]


def test_single_bot_baza_without_free_text_search(env):  # noqa: F811
    cfg, db = real(env)
    sid = db.seller_id("Продавец04", create=False)
    with db.tx() as c:
        c.execute("UPDATE sellers SET max_user_id=404 WHERE id=?", (sid,))
    plan = FakeMax()
    bot.handle(db, cfg, plan, {"update_type": "message_created",
                               "message": {"sender": {"user_id": 404}, "body": {"text": "/baza"}}})
    assert plan.sent[0][1].startswith("📖 **База знаний.**")
    bot.handle(db, cfg, plan, {"update_type": "message_created",
                               "message": {"sender": {"user_id": 404}, "body": {"text": "кашемир"}}})
    assert plan.sent[1][1].startswith("Я присылаю каждое утро")  # в «Плане» поиска по любому слову нет

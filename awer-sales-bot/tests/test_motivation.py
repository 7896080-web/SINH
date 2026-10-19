"""Мотивация и обучение: рекорды, цель недели, «кого похвалить», цель магазина, уроки, новинки, листок старшего,
программа новичка, квизы."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from awer_sales_bot import achievements
from awer_sales_bot.importers.onec_sales import import_file
from awer_sales_bot.messages import daily
from awer_sales_bot.planning import service
from test_flow import env  # noqa: F401 — фикстура: история из обезличенных ведомостей

HEAD = "Дата;Время;Склад;Документ;Чек;Продавец;Номенклатура;Номенклатурная группа;Количество;Цена;Сумма;Скидка"


def write_sales(path: Path, rows: list[tuple]) -> Path:
    """rows: (день, продавец, сумма, вещей[, категория, чек, скидка]) — одна строка на вещь не нужна, хватит итога.
    Как обработка 1С: строки ОРП (деньги) в path; если у строк есть чек — ещё receipts_<имя> рядом (средний чек)."""
    orp, rec = [HEAD], [HEAD]
    for i, r in enumerate(rows):
        d, seller, amount, qty = r[:4]
        cat = r[4] if len(r) > 4 else "Джинсы"
        chk = r[5] if len(r) > 5 else ""
        disc = r[6] if len(r) > 6 else 0
        price = (amount + disc) / qty if qty else amount
        tail = f"{seller};Арт{i};{cat};{qty};{price:.2f};{amount:.2f};{disc}"
        orp.append(f"{d:%d.%m.%Y};;ГОРОД_склад;Д{d:%m%d};;{tail}")
        if chk:
            rec.append(f"{d:%d.%m.%Y};12:00;ГОРОД_склад;{chk};{chk};{tail}")
    path.write_text("\n".join(orp), encoding="utf-8")
    if len(rec) > 1:
        path.with_name("receipts_" + path.name).write_text("\n".join(rec), encoding="utf-8")
    return path


def import_sales(path: Path, rows: list[tuple], db, cfg):
    """write_sales + загрузка строк ОРП и чеков; отчёт — по строкам ОРП."""
    rep = import_file(write_sales(path, rows), db, cfg)
    rec = path.with_name("receipts_" + path.name)
    if rec.exists():
        import_file(rec, db, cfg)
    return rep


def oct_day(n: int) -> date:
    return date(2026, 10, 1) + timedelta(days=n - 1)


def test_records_and_streak(env, tmp_path):  # noqa: F811
    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    sid = db.seller_id("Продавец04", create=False)
    rows = [(oct_day(i), "Продавец04", 30_000, 3) for i in range(1, 12)]  # 11 рабочих дней по 30 000
    rows += [(oct_day(12), "Продавец04", 40_000, 4), (oct_day(13), "Продавец04", 45_000, 5),
             (oct_day(14), "Продавец04", 50_000, 6)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    plan = service.frozen_plan(db, "2026-10", sid)  # 547 706 / 18 → дневная норма ≈ 30 428
    got = {a.kind: a for a in achievements.for_day(db, cfg, sid, oct_day(14), plan)}
    assert "Личный рекорд: 50 000 ₽" in got["record_sales"].text and "45 000 ₽" in got["record_sales"].text
    assert got["record_items"].notable
    assert got["streak"].text.startswith("Серия: 3 рабочих дня подряд") and got["streak"].notable
    assert achievements.for_day(db, cfg, sid, oct_day(15), plan) == []  # не работал — нечего хвалить
    text = daily(db, cfg, sid, oct_day(14))
    assert "🏅 Личный рекорд: 50 000 ₽" in text


def test_best_shift_of_month_before_enough_history(env, tmp_path):  # noqa: F811
    cfg, db = env
    sid = db.seller_id("Продавец04", create=False)
    rows = [(oct_day(1), "Продавец04", 20_000, 2), (oct_day(2), "Продавец04", 25_000, 2),
            (oct_day(3), "Продавец04", 30_000, 3)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    kinds = [a.kind for a in achievements.for_day(db, cfg, sid, oct_day(3), None)]
    assert kinds == ["best_month"]  # «за всё время» — только после 10 рабочих дней в истории


def test_weekly_goal_set_progress_and_close(env, tmp_path):  # noqa: F811
    from awer_sales_bot import goals
    cfg, db = env
    sid = db.seller_id("Продавец04", create=False)
    rows = []
    for i in range(10):  # 4 недели до понедельника 19.10: у Продавец04 1 вещь за смену, у Продавец05 — 3
        d = date(2026, 9, 22) + timedelta(days=i * 2)
        rows += [(d, "Продавец04", 4_000, 1), (d, "Продавец05", 12_000, 3)]
    rows += [(date(2026, 10, 19), "Продавец04", 8_000, 2), (date(2026, 10, 20), "Продавец04", 8_000, 2),
             (date(2026, 10, 19), "Продавец05", 12_000, 3)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)

    goals.ensure_week(db, cfg, date(2026, 10, 19))
    goals.ensure_week(db, cfg, date(2026, 10, 19))  # повторный запуск ничего не меняет
    g = goals.get(db, sid, date(2026, 10, 19))
    assert g.metric == "items_per_shift" and g.baseline == 1 and g.team == 2 and g.target == pytest.approx(1.5)
    assert db.one("SELECT COUNT(*) n FROM weekly_goals")["n"] == 2
    assert goals.lines(db, sid, date(2026, 10, 18)) == [
        "🎯 **Цель новой недели:** вещей за рабочий день 1,5 (сейчас 1,0, команда 2,0)"]
    assert goals.lines(db, sid, date(2026, 10, 20)) == [
        "🎯 Цель недели: вещей за рабочий день 1,5 — с понедельника у вас 2,0"]

    closed = goals.ensure_week(db, cfg, date(2026, 10, 26))
    assert {(c.seller_id, c.done) for c in closed} >= {(sid, 1)}
    assert goals.lines(db, sid, date(2026, 10, 25))[0] == \
        "Итог недели: цель «вещей за рабочий день 1,5» выполнена ✅, у вас 2,0"
    assert "🎯" in daily(db, cfg, sid, date(2026, 10, 20))


def test_owner_praise(env, tmp_path):  # noqa: F811
    from awer_sales_bot import goals
    from awer_sales_bot.messages import owner_summary
    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    rows = [(oct_day(i), "Продавец04", 30_000, 3) for i in range(1, 12)] + [(oct_day(12), "Продавец04", 60_000, 4)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    text = owner_summary(db, cfg, oct_day(12))
    assert "👏 **Кого похвалить:**" in text and "Продавец04 — Личный рекорд: 60 000 ₽" in text
    assert "Кого похвалить" not in owner_summary(db, cfg, oct_day(11))  # обычный рабочий день — без похвалы

    sid = db.seller_id("Продавец04", create=False)
    with db.tx() as c:  # цель недели 05.10–11.10 выполнена → в сводке за воскресенье 11.10
        c.execute("INSERT INTO weekly_goals(week,seller_id,metric,target,result,done,closed_at) "
                  "VALUES('2026-10-05',?,'items_per_shift',2.5,3,1,'x')", (sid,))
    assert any("цель недели «вещей за рабочий день 2,5» выполнена" in w
               for _, _, why in achievements.praise(db, cfg, oct_day(11)) for w in why)
    assert goals.monday(oct_day(11)).isoformat() == "2026-10-05"


def test_store_plan_only_for_owner_and_example_config_keeps_no_seller(env, tmp_path):  # noqa: F811
    from awer_sales_bot import config
    from awer_sales_bot.messages import owner_summary, store_goal
    cfg, db = env
    assert store_goal(db, cfg, oct_day(10)) == []  # план не зафиксирован — молчим
    mp = service.freeze(db, cfg, "2026-10")
    rows = [(oct_day(1), "Продавец04", 1_000_000, 50), (oct_day(2), "", 121_090, 5)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    line = store_goal(db, cfg, oct_day(10))[0]
    assert line.startswith("Магазины вместе: 1 121 090 ₽ из 5 605 452 ₽ — 20% плана, прошло 10 из 31 дня")
    assert mp.store.plan == pytest.approx(5_605_452, abs=1)
    sid = db.seller_id("Продавец04", create=False)
    seller_text = daily(db, cfg, sid, oct_day(2))
    assert "Магазины вместе" not in seller_text  # продавцу — только личное
    assert "**Ваш план на октябрь**" in seller_text and "**Ваши показатели с начала месяца**" in seller_text
    assert "Магазины вместе: 1 121 090 ₽" in owner_summary(db, cfg, oct_day(10))

    ex = config.load(Path(__file__).parents[1] / "config.example.yaml")
    assert "" not in ex.onec.exclude_sellers and ex.motivation.records and ex.training.onboarding_shifts == 10


LESSONS_YAML = """
lessons:
  - {id: obraz-1, topic: items, title: Образ 1, text: Текст образа 1, try_today: Предложите ремень,
     quiz: [{q: "Что предложить к брюкам?", options: [Ремень, Ничего, Скидку], correct: 0,
            explain: Ремень завершает образ}]}
  - {id: obraz-2, topic: items, title: Образ 2, text: Текст образа 2, try_today: Вторая вещь, quiz: []}
  - {id: kontakt-1, topic: general, title: Контакт, text: Текст контакта, try_today: Поздоровайтесь, quiz: []}
onboarding:
  - {shift: 2, title: Примерочная, text: Текст смены 2, try_today: Проводите до примерочной}
  - {shift: 1, title: Знакомство, text: Текст смены 1, try_today: Обойдите зал}
"""


class FakeMax:
    def __init__(self):
        self.sent, self.answers = [], []

    def send(self, text, user_id=None, buttons=None, **kw):
        self.sent.append((user_id, text, buttons))
        return {}

    def answer(self, callback_id, notification=None, message=None):
        self.answers.append((callback_id, notification, message))
        return {}


def linked_env(env, tmp_path):  # noqa: F811
    from awer_sales_bot.jobs import Sender
    cfg, db = env
    (tmp_path / "lessons.yaml").write_text(LESSONS_YAML, encoding="utf-8")
    cfg.training.lessons_path = tmp_path / "lessons.yaml"
    with db.tx() as c:
        for i, s in enumerate(db.sellers()):
            c.execute("UPDATE sellers SET max_user_id=? WHERE id=?", (1000 + i, s["id"]))
    client = FakeMax()
    sender = Sender(db, cfg, client)
    sender.GAP = 0
    return cfg, db, client, sender


def test_weekly_lesson_by_goal_topic(env, tmp_path):  # noqa: F811
    from awer_sales_bot import goals
    from awer_sales_bot.jobs import training_run
    cfg, db, client, sender = linked_env(env, tmp_path)
    sid = db.seller_id("Продавец04", create=False)
    rows = []
    for i in range(10):
        d = date(2026, 9, 22) + timedelta(days=i * 2)
        rows += [(d, "Продавец04", 4_000, 1), (d, "Продавец05", 12_000, 3)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    uid = db.seller(sid)["max_user_id"]

    titles = []
    for monday in (date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 2)):
        goals.ensure_week(db, cfg, monday)
        training_run(db, cfg, sender, monday)
        training_run(db, cfg, sender, monday + timedelta(days=1))  # повтор в ту же неделю — без второго урока
        mine = [t for u, t, _ in client.sent if u == uid and t.startswith("📚")]
        assert len(mine) == 1
        titles.append(mine[0].splitlines()[0])
        client.sent.clear()
    assert titles == ["📚 **Урок недели: Образ 1**", "📚 **Урок недели: Образ 2**", "📚 **Урок недели: Контакт**"]
    assert {"obraz-1", "obraz-2", "kontakt-1"} <= {r["lesson_id"] for r in db.q("SELECT lesson_id FROM lessons_sent")}


def test_arrivals_pairing(env, tmp_path):  # noqa: F811
    from awer_sales_bot.messages import pairing
    cfg, db = env
    sid = db.seller_id("Продавец04", create=False)
    rows = []
    for i in range(12):  # 12 чеков: 9 «джинсы + ремень», 3 — только джинсы
        d = oct_day(1 + i % 5)
        rows.append((d, "Продавец04", 5_000, 1, "Джинсы", f"Ч{i}"))
        if i < 9:
            rows.append((d, "Продавец04", 1_500, 1, "Ремень", f"Ч{i}"))
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    (tmp_path / "arrivals_1.csv").write_text(
        "Дата;Склад;Документ;Номенклатура;Номенклатурная группа;Характеристика;Поступило\n"
        "10.10.2026;ГОРОД_склад;П1;Джинсы слим;Джинсы;32;6", encoding="utf-8")
    import_file(tmp_path / "arrivals_1.csv", db, cfg)
    assert pairing(db, cfg, oct_day(10), ["Джинсы"]) == []  # джинсы идут с ремнём в комплекте — не советуем
    cfg.recommendations.included = {}
    assert pairing(db, cfg, oct_day(10), ["Джинсы"]) == ["С чем предлагать: к «Джинсы» чаще берут «Ремень» (75%)"]
    assert "С чем предлагать: к «Джинсы»" in daily(db, cfg, sid, oct_day(10))


def test_coach_sheet_to_admins_and_owner_once(env, tmp_path):  # noqa: F811
    from awer_sales_bot import coach, goals
    from awer_sales_bot.jobs import training_run
    cfg, db, client, sender = linked_env(env, tmp_path)
    cfg.max.owners = [1]
    rows = []
    for i in range(10):
        d = date(2026, 9, 22) + timedelta(days=i * 2)
        rows += [(d, "Продавец04", 4_000, 1), (d, "Продавец05", 12_000, 3)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    monday = date(2026, 10, 19)
    goals.ensure_week(db, cfg, monday)
    training_run(db, cfg, sender, monday)
    client.sent.clear()
    coach.run(db, cfg, sender, monday)
    coach.run(db, cfg, sender, monday + timedelta(days=2))  # в ту же неделю — без повтора

    admin_uid = db.seller(db.seller_id("Админ1", create=False))["max_user_id"]
    seller_uids = {db.seller(db.seller_id(n, create=False))["max_user_id"] for n in ("Продавец04", "Продавец05")}
    got = [(u, t) for u, t, _ in client.sent]
    assert sorted(u for u, _ in got) == sorted([1, admin_uid, db.seller(db.seller_id("Админ2", create=False))["max_user_id"]])
    assert not seller_uids & {u for u, _ in got}  # рядовым продавцам листок не уходит
    text = got[0][1]
    assert "**Продавец05** — похвалить: вещей за рабочий день 3,0 (команда 2,0)" in text
    assert "**Продавец04** — обсудить: вещей за рабочий день 1,0 → цель 1,5; урок недели «Образ 1»" in text


def test_newcomer_mid_month_gets_plan_and_payroll_row(env, tmp_path):  # noqa: F811
    from awer_sales_bot.jobs import Sender, daily_run
    cfg, db = env
    cfg.max.owners = [1]
    mp = service.freeze(db, cfg, "2026-10")
    before = {r["seller_id"]: r["plan_full"] for r in db.q("SELECT * FROM plans WHERE month='2026-10'")}
    rows = [(oct_day(10 + i), "Новенькая Анна", 40_000, 4) for i in range(8)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    for n in ("Новенькая Анна", "Ушедший Пётр"):  # второй: продажи есть, а плана не будет (не активен)
        db.seller_id(n)
    with db.tx() as c:
        c.execute("UPDATE sellers SET active=0 WHERE name='Ушедший Пётр'")
    import_sales(tmp_path / "s2.csv", [(oct_day(25), "Ушедший Пётр", 10_000, 1)], db, cfg)

    daily_run(db, cfg, Sender(db, cfg, None, dry_run=True), today=oct_day(18), force=True)
    nid = db.seller_id("Новенькая Анна", create=False)
    row = db.one("SELECT * FROM plans WHERE month='2026-10' AND seller_id=?", nid)
    assert row["method"] == "newcomer" and row["plan_full"] == pytest.approx(mp.store.newcomer_plan)
    after = {r["seller_id"]: r["plan_full"] for r in db.q("SELECT * FROM plans WHERE month='2026-10'")}
    assert {k: v for k, v in after.items() if k != nid} == before  # остальные планы не тронуты
    note = db.one("SELECT text FROM message_log WHERE kind=?", f"new_plan_{nid}")["text"]
    assert note.startswith("Новый продавец Новенькая Анна: план 449 146 ₽ на полный месяц (новичок)")
    assert service.ensure_plans(db, cfg, "2026-10") == []  # второй раз — ничего

    res = {r["name"]: r for r in service.close_month(db, cfg, "2026-10")}
    assert res["Новенькая Анна"]["plan"] == pytest.approx(mp.store.newcomer_plan)
    assert res["Ушедший Пётр"]["plan"] is None and res["Ушедший Пётр"]["rate"] == 25_000  # в ведомости, база


def test_onboarding_lessons_one_per_day(env, tmp_path, monkeypatch):  # noqa: F811
    from awer_sales_bot import bot
    from awer_sales_bot.jobs import training_run
    cfg, db, client, sender = linked_env(env, tmp_path)
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    nid = db.seller_id("Новенькая Анна")
    code = db.new_invite(nid)
    bot.handle(db, cfg, client, {"update_type": "bot_started", "user": {"user_id": 777}, "payload": code})
    assert [t.splitlines()[0] for _, t, _ in client.sent][1] == "📚 **Программа новичка (1 из 2): Знакомство**"

    import_sales(tmp_path / "s.csv", [(oct_day(10), "Новенькая Анна", 30_000, 3)], db, cfg)
    client.sent.clear()
    training_run(db, cfg, sender, oct_day(11))
    training_run(db, cfg, sender, oct_day(11))  # повтор в тот же день — без второго урока
    training_run(db, cfg, sender, oct_day(12))  # смена 2 уже отправлена — больше уроков программы нет
    mine = [t.splitlines()[0] for u, t, _ in client.sent if u == 777]
    assert mine == ["📚 **Программа новичка (2 из 2): Примерочная**"]


def test_real_lessons_file_is_valid_and_returns_topic(env, tmp_path):  # noqa: F811
    from awer_sales_bot import lessons
    from awer_sales_bot.config import Settings
    from awer_sales_bot.jobs import training_run
    data = lessons.load(Settings())  # content/lessons.yaml из репозитория
    assert len(data["lessons"]) >= 14 and [x["shift"] for x in data["onboarding"]] == list(range(1, 11))
    ids = [x["id"] for x in data["lessons"]]
    assert len(ids) == len(set(ids))
    for x in data["lessons"]:
        assert x["topic"] in {"items", "avg_price", "discount", "returns", "general"}
        assert len(x["title"]) <= 60 and len(x["text"]) <= 900 and len(x["try_today"]) <= 200
        assert "*" not in x["text"] + x["title"] + x["try_today"] and "_" not in x["text"]
        for q in x["quiz"]:
            assert len(q["options"]) == 3 and 0 <= q["correct"] < 3 and all(len(o) <= 60 for o in q["options"])

    cfg, db, client, sender = linked_env(env, tmp_path)
    cfg.training.lessons_path = Settings().training.lessons_path
    rows = [(oct_day(i), "Продавец04", 10_000, 2) for i in range(1, 9)] + [(oct_day(9), "Продавец04", -3_000, -1)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    uid = db.seller(db.seller_id("Продавец04", create=False))["max_user_id"]
    training_run(db, cfg, sender, date(2026, 10, 12))  # возвраты 3 000 / 80 000 = 3,75 % — урок не про возвраты
    first = [t for u, t, _ in client.sent if u == uid and t.startswith("📚")][-1]
    assert "Примерка, после которой" not in first
    import_sales(tmp_path / "s.csv", rows + [(oct_day(10), "Продавец04", -8_000, -2)], db, cfg)
    training_run(db, cfg, sender, date(2026, 10, 19))  # 11 000 / 80 000 = 13,75 % > 8 %
    assert [t for u, t, _ in client.sent if u == uid and t.startswith("📚")][-1].startswith(
        "📚 **Урок недели: Примерка, после которой")


def test_quiz_after_lesson_and_answers(env, tmp_path, monkeypatch):  # noqa: F811
    from awer_sales_bot import bot, goals
    from awer_sales_bot.jobs import training_run
    cfg, db, client, sender = linked_env(env, tmp_path)
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    rows = []
    for i in range(10):
        d = date(2026, 9, 22) + timedelta(days=i * 2)
        rows += [(d, "Продавец04", 4_000, 1), (d, "Продавец05", 12_000, 3)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    sid = db.seller_id("Продавец04", create=False)
    uid = db.seller(sid)["max_user_id"]
    goals.ensure_week(db, cfg, date(2026, 10, 19))
    training_run(db, cfg, sender, date(2026, 10, 19))
    q = [(t, b) for u, t, b in client.sent if u == uid][-1]
    assert q[0].startswith("❓ Вопрос 1 из 1 к уроку «Образ 1»") and q[1][1][0]["payload"] == "quiz|obraz-1|0|1"

    def press(user, payload):
        client.sent.clear()
        bot.handle(db, cfg, client, {"update_type": "message_callback",
                                     "callback": {"callback_id": "cb1", "payload": payload, "user": {"user_id": user}}})
        return client.answers[-1][1], [t for _, t, _ in client.sent]

    note, msgs = press(uid, "quiz|obraz-1|0|1")  # неверно: правильный — «Ремень» (0)
    assert note == "Ответ принят" and msgs[0].startswith("Не совсем. Верный ответ: «Ремень».")
    assert msgs[1] == "Квиз пройден: 0 из 1. Спасибо! Следующий урок — через неделю."
    assert press(uid, "quiz|obraz-1|0|0")[0] == "Ответ уже принят"  # переответить нельзя
    other = db.seller(db.seller_id("Админ1", create=False))["max_user_id"]  # урок ему не отправлялся
    assert press(other, "quiz|obraz-1|0|0") == ("Кнопка устарела", [])
    assert press(uid, "quiz|obraz-1|abc|0")[0] == "Кнопка устарела"
    assert db.one("SELECT correct FROM quiz_answers WHERE seller_id=?", sid)["correct"] == 0


def test_owner_praise_button(env, tmp_path, monkeypatch):  # noqa: F811
    from awer_sales_bot import bot
    from awer_sales_bot.jobs import daily_run
    cfg, db, client, sender = linked_env(env, tmp_path)
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    cfg.max.owners = [1]
    service.freeze(db, cfg, "2026-10")
    rows = [(oct_day(i), "Продавец04", 30_000, 3) for i in range(1, 12)] + [(oct_day(12), "Продавец04", 60_000, 4)]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    daily_run(db, cfg, sender, today=oct_day(13), force=True)
    owner_msgs = [(t, b) for u, t, b in client.sent if u == 1 and t.startswith("**Сводка")]
    button = owner_msgs[0][1][0][0]
    assert button["text"] == "👏 Похвалить: Продавец04" and button["payload"].startswith("praise|")

    sid = db.seller_id("Продавец04", create=False)
    seller_uid = db.seller(sid)["max_user_id"]
    for who, expect in ((seller_uid, "Кнопка устарела"), (1, "Отправлено 👏"), (1, "Уже отправлено")):
        client.sent.clear()
        bot.handle(db, cfg, client, {"update_type": "message_callback", "callback": {
            "callback_id": "c", "payload": button["payload"], "user": {"user_id": who}}})
        assert client.answers[-1][1] == expect
    assert db.one("SELECT text FROM message_log WHERE kind='praise'")["text"].startswith(
        "👏 Владелец отметил ваш результат за 12.10: Личный рекорд: 60 000 ₽")


def test_max_step_tip_does_not_promise_next_step(env, tmp_path):  # noqa: F811
    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    sid = db.seller_id("Продавец04", create=False)
    # 10 рабочих дней × 90 000 = 900 000 ₽ = 164 % плана месяца 547 706 ₽ — выше 160 %, это максимальная ступень
    import_sales(tmp_path / "s.csv", [(oct_day(i), "Продавец04", 90_000, 8) for i in range(1, 11)], db, cfg)
    text = daily(db, cfg, sid, oct_day(10))
    assert "Максимальная ступень достигнута" in text and "следующая ступень уже близко" not in text


def test_tips_name_only_categories_in_stock(env, tmp_path):  # noqa: F811
    from awer_sales_bot import recommendations as R
    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    sid = db.seller_id("Продавец04", create=False)
    rows = []
    for i in range(12):  # команда (Продавец05) продаёт джинсы с ремнём и галстуком, Продавец04 — одну вещь
        d = oct_day(1 + i % 6)
        rows += [(d, "Продавец05", 5_000, 1, "Джинсы", f"Б{i}"), (d, "Продавец05", 1_500, 1, "Ремень", f"Б{i}"),
                 (d, "Продавец05", 900, 1, "Галстук", f"Б{i}"), (d, "Продавец04", 5_000, 1, "Джинсы", f"А{i}")]
    import_sales(tmp_path / "s.csv", rows, db, cfg)
    st = service.status(db, cfg, sid, oct_day(6))

    tips = " ".join(t for _, t in R.rules(db, cfg, sid, st, oct_day(6)))  # остатков из 1С ещё нет
    assert "Предлагайте второй товар" in tips and "«Галстук»" not in tips and "«Ремень»" not in tips
    assert "к основной вещи предлагайте вторую" in tips

    (tmp_path / "stock_1.csv").write_text(  # на остатке ГОРОДа: джинсы и ремни, галстуков нет
        "Дата;Склад;Номенклатура;Номенклатурная группа;Характеристика;Остаток\n"
        "06.10.2026;ГОРОД_склад;Джинсы слим;Джинсы;32;5\n06.10.2026;ГОРОД_склад;Ремень кожа;Ремень;U;7\n", encoding="utf-8")
    import_file(tmp_path / "stock_1.csv", db, cfg)
    tips = " ".join(t for _, t in R.rules(db, cfg, sid, st, oct_day(6)))
    assert "Галстук" not in tips and "«Джинсы» + «Ремень»" not in tips  # ремень идёт с джинсами в комплекте
    cfg.recommendations.included = {}
    tips = " ".join(t for _, t in R.rules(db, cfg, sid, st, oct_day(6)))
    assert "«Ремень»" in tips and "Галстук" not in tips
    assert "«Джинсы» + «Ремень»" in tips

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
    """rows: (день, продавец, сумма, вещей[, категория, чек, скидка]) — одна строка на вещь не нужна, хватит итога."""
    out = [HEAD]
    for i, r in enumerate(rows):
        d, seller, amount, qty = r[:4]
        cat = r[4] if len(r) > 4 else "Джинсы"
        rec = r[5] if len(r) > 5 else ""
        disc = r[6] if len(r) > 6 else 0
        price = (amount + disc) / qty if qty else amount
        out.append(f"{d:%d.%m.%Y};{'12:00' if rec else ''};ГОРОД_склад;Д{d:%m%d};{rec};{seller};Арт{i};{cat};{qty};"
                   f"{price:.2f};{amount:.2f};{disc}")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def oct_day(n: int) -> date:
    return date(2026, 10, 1) + timedelta(days=n - 1)


def test_records_and_streak(env, tmp_path):  # noqa: F811
    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    sid = db.seller_id("Продавец04", create=False)
    rows = [(oct_day(i), "Продавец04", 30_000, 3) for i in range(1, 12)]  # 11 смен по 30 000
    rows += [(oct_day(12), "Продавец04", 40_000, 4), (oct_day(13), "Продавец04", 45_000, 5),
             (oct_day(14), "Продавец04", 50_000, 6)]
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)
    plan = service.frozen_plan(db, "2026-10", sid)  # 561 764 → норма смены ≈ 37 451
    got = {a.kind: a for a in achievements.for_day(db, cfg, sid, oct_day(14), plan)}
    assert "Личный рекорд: 50 000 ₽" in got["record_sales"].text and "45 000 ₽" in got["record_sales"].text
    assert got["record_items"].notable
    assert got["streak"].text.startswith("Серия: 3 смены подряд") and got["streak"].notable
    assert achievements.for_day(db, cfg, sid, oct_day(15), plan) == []  # не работал — нечего хвалить
    text = daily(db, cfg, sid, oct_day(14))
    assert "🏅 Личный рекорд: 50 000 ₽" in text


def test_best_shift_of_month_before_enough_history(env, tmp_path):  # noqa: F811
    cfg, db = env
    sid = db.seller_id("Продавец04", create=False)
    rows = [(oct_day(1), "Продавец04", 20_000, 2), (oct_day(2), "Продавец04", 25_000, 2),
            (oct_day(3), "Продавец04", 30_000, 3)]
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)
    kinds = [a.kind for a in achievements.for_day(db, cfg, sid, oct_day(3), None)]
    assert kinds == ["best_month"]  # «за всё время» — только после 10 смен в истории


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
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)

    goals.ensure_week(db, cfg, date(2026, 10, 19))
    goals.ensure_week(db, cfg, date(2026, 10, 19))  # повторный запуск ничего не меняет
    g = goals.get(db, sid, date(2026, 10, 19))
    assert g.metric == "items_per_shift" and g.baseline == 1 and g.team == 2 and g.target == pytest.approx(1.5)
    assert db.one("SELECT COUNT(*) n FROM weekly_goals")["n"] == 2
    assert goals.lines(db, sid, date(2026, 10, 18)) == [
        "🎯 **Цель новой недели:** вещей за смену 1,5 (сейчас 1,0, команда 2,0)"]
    assert goals.lines(db, sid, date(2026, 10, 20)) == [
        "🎯 Цель недели: вещей за смену 1,5 — с понедельника у вас 2,0"]

    closed = goals.ensure_week(db, cfg, date(2026, 10, 26))
    assert {(c.seller_id, c.done) for c in closed} >= {(sid, 1)}
    assert goals.lines(db, sid, date(2026, 10, 25))[0] == \
        "Итог недели: цель «вещей за смену 1,5» выполнена ✅, у вас 2,0"
    assert "🎯" in daily(db, cfg, sid, date(2026, 10, 20))


def test_owner_praise(env, tmp_path):  # noqa: F811
    from awer_sales_bot import goals
    from awer_sales_bot.messages import owner_summary
    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    rows = [(oct_day(i), "Продавец04", 30_000, 3) for i in range(1, 12)] + [(oct_day(12), "Продавец04", 60_000, 4)]
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)
    text = owner_summary(db, cfg, oct_day(12))
    assert "👏 **Кого похвалить:**" in text and "Продавец04 — Личный рекорд: 60 000 ₽" in text
    assert "Кого похвалить" not in owner_summary(db, cfg, oct_day(11))  # обычная смена — без похвалы

    sid = db.seller_id("Продавец04", create=False)
    with db.tx() as c:  # цель недели 05.10–11.10 выполнена → в сводке за воскресенье 11.10
        c.execute("INSERT INTO weekly_goals(week,seller_id,metric,target,result,done,closed_at) "
                  "VALUES('2026-10-05',?,'items_per_shift',2.5,3,1,'x')", (sid,))
    assert any("цель недели «вещей за смену 2,5» выполнена" in w
               for _, _, why in achievements.praise(db, cfg, oct_day(11)) for w in why)
    assert goals.monday(oct_day(11)).isoformat() == "2026-10-05"


def test_store_goal_and_example_config_keeps_no_seller(env, tmp_path):  # noqa: F811
    from awer_sales_bot import config
    from awer_sales_bot.messages import store_goal
    cfg, db = env
    assert store_goal(db, cfg, oct_day(10)) == []  # план не зафиксирован — молчим
    mp = service.freeze(db, cfg, "2026-10")
    rows = [(oct_day(1), "Продавец04", 1_000_000, 50), (oct_day(2), "", 121_090, 5)]
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)
    line = store_goal(db, cfg, oct_day(10))[0]
    assert line.startswith("Магазины вместе: 1 121 090 ₽ из 5 605 452 ₽ — 20% плана, прошло 10 из 31 дня")
    assert mp.store.plan == pytest.approx(5_605_452, abs=1)
    cfg.motivation.store_goal_reward = "общий ужин команды"
    assert store_goal(db, cfg, oct_day(10))[1] == "Если магазины выполнят план: общий ужин команды"

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
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)
    uid = db.seller(sid)["max_user_id"]

    titles = []
    for monday in (date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 2)):
        goals.ensure_week(db, cfg, monday)
        training_run(db, cfg, sender, monday)
        training_run(db, cfg, sender, monday + timedelta(days=1))  # повтор в ту же неделю — без второго урока
        mine = [t for u, t, _ in client.sent if u == uid]
        titles.append(mine[-1].splitlines()[0])
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
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)
    (tmp_path / "arrivals_1.csv").write_text(
        "Дата;Склад;Документ;Номенклатура;Номенклатурная группа;Характеристика;Поступило\n"
        "10.10.2026;ГОРОД_склад;П1;Джинсы слим;Джинсы;32;6", encoding="utf-8")
    import_file(tmp_path / "arrivals_1.csv", db, cfg)
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
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)
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
    assert "**Продавец05** — похвалить: вещей за смену 3,0 (команда 2,0)" in text
    assert "**Продавец04** — обсудить: вещей за смену 1,0 → цель 1,5; урок недели «Образ 1»" in text

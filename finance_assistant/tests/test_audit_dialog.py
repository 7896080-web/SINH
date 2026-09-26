"""Регрессии по аудиту диалога: старые кнопки, удалённые карты, файлы."""
import os
from datetime import date

from conftest import CHAT, answer, payment, png
from finance.flow import _parse_date
from test_sverka import PDF, op, statement


def buttons(reply):
    return [d for row in reply.buttons for _, d in row]


def test_old_date_button_does_not_touch_next_draft(env):
    db, rec, flow = env
    rec.payments += [payment(date="", merchant="A"), payment(date="2026-09-20", merchant="B",
                                                             category_confident=False)]
    [qa] = flow.on_files(CHAT, [png()], "")
    old_yesterday = next(b for b in buttons(qa) if b.endswith(":date:1"))
    flow.on_files(CHAT, [png()], "")          # B встаёт в очередь
    flow.on_text(CHAT, "05.09")               # A сохранена, B спрашивает статью
    [r] = flow.on_button(CHAT, old_yesterday)
    assert "неактуален" in r.text
    assert db.get_state(CHAT)["drafts"][0]["date"] == "2026-09-20"


def test_old_cancel_button_does_not_drop_next_draft(env):
    db, rec, flow = env
    rec.payments += [payment(currency="USD", merchant="A"),
                     payment(merchant="B", category_confident=False)]
    [qa] = flow.on_files(CHAT, [png()], "")
    old_cancel = next(b for b in buttons(qa) if b.endswith(":skip"))
    flow.on_files(CHAT, [png()], "")
    flow.on_text(CHAT, "700")
    flow.on_button(CHAT, old_cancel)
    assert len(db.get_state(CHAT)["drafts"]) == 1   # B на месте


def test_old_category_button_does_not_create_wrong_rule(env):
    db, rec, flow = env
    rec.payments += [payment(merchant="Яндекс Директ", category_confident=False),
                     payment(merchant="Пятёрочка", looks_personal=True, amount="900")]
    [qa] = flow.on_files(CHAT, [png()], "")
    old_cat = next(b for b in buttons(qa) if b.endswith(f":cat:{db.category_id('Реклама и продвижение')}"))
    flow.on_command(CHAT, "cancel")
    flow.on_files(CHAT, [png()], "")                 # Пятёрочка: «Бизнес / Личное?»
    assert "неактуален" in flow.on_button(CHAT, old_cat)[0].text
    answer(flow, "d:purpose:personal")
    assert db.rules() == [] and db.expenses("2026-09")[0].purpose == "personal"


def test_button_of_other_question_same_draft_rejected(env):
    db, rec, flow = env
    rec.payments.append(payment(card_last4="", bank="", category_confident=False))
    flow.on_files(CHAT, [png()], "")                 # вопрос «С какой карты?»
    did = db.get_state(CHAT)["drafts"][0]["id"]
    assert "неактуален" in flow.on_button(CHAT, f"d:{did}:tr:0")[0].text   # не KeyError
    assert "неактуален" in flow.on_button(CHAT, "d:card:1")[0].text        # старый формат


def test_card_deleted_while_question_open(env):
    db, rec, flow = env
    oz = db.add_card("Озон", "4444", "Озон")
    rec.payments.append(payment(card_last4="4444", bank="Озон", category_confident=False))
    flow.on_files(CHAT, [png()], "")
    assert "сейчас нужна" in flow.on_command(CHAT, "delcard", str(oz.id))[0].text
    # Даже если карта исчезла (например, удалена в другом чате), вопрос не зависает.
    db.conn.execute("PRAGMA foreign_keys = OFF")
    db.conn.execute("DELETE FROM cards WHERE id = ?", (oz.id,))
    db.conn.commit()
    db.conn.execute("PRAGMA foreign_keys = ON")
    replies = answer(flow, f"d:cat:{db.category_id('Прочее')}")
    assert "С какой карты" in replies[-1].text
    answer(flow, f"d:card:{db.cards()[0].id}")
    assert len(db.expenses("2026-09")) == 1


def test_statement_card_gone_resets_mode(env):
    db, rec, flow = env
    oz = db.add_card("Озон", "4444", "Озон")
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{oz.id}")
    assert "сейчас нужна" in flow.on_command(CHAT, "delcard", str(oz.id))[0].text
    db.conn.execute("DELETE FROM cards WHERE id = ?", (oz.id,))
    db.conn.commit()
    assert "сверка сброшена" in flow.on_files(CHAT, [PDF], "")[0].text
    assert "statement" not in db.get_state(CHAT)


def test_old_reset_button_does_not_wipe_other_card(env):
    db, rec, flow = env
    a, b = db.cards()[0].id, db.cards()[1].id
    for cid in (a, b):
        flow.on_command(CHAT, "sverka", "2026-09")
        flow.on_button(CHAT, f"s:c:{cid}")
        rec.statements.append(statement([op("2026-09-02", "100")]))
        flow.on_files(CHAT, [(b"%PDF " + bytes([cid]), "application/pdf")], "")
        flow.on_command(CHAT, "done")
    flow.on_command(CHAT, "sverka", "2026-09")
    [ra] = flow.on_button(CHAT, f"s:c:{a}")
    old_reset = buttons(ra)[0]
    flow.on_command(CHAT, "done")
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{b}")
    assert "неактуален" in flow.on_button(CHAT, old_reset)[0].text
    assert db.statement(b, "2026-09") is not None


def test_old_accept_button_does_not_record_other_list(env):
    db, rec, flow = env

    def sverka(cid, desc, cat, blob):
        flow.on_command(CHAT, "sverka", "2026-09")
        flow.on_button(CHAT, f"s:c:{cid}")
        rec.statements.append(statement([{**op("2026-09-02", "100", desc=desc),
                                          "business_category": cat}]))
        flow.on_files(CHAT, [(blob, "application/pdf")], "")
        return flow.on_command(CHAT, "done")
    replies = sverka(db.cards()[0].id, "ads A", "Реклама и продвижение", b"%PDF a")
    old_acc = next(b for r in replies for b in buttons(r) if b.startswith("s:acc:"))
    sverka(db.cards()[1].id, "Ресторан B", "Прочее", b"%PDF b")
    assert "неактуален" in flow.on_button(CHAT, old_acc)[0].text
    assert db.expenses("2026-09") == []


def test_file_for_wrong_month_can_be_reused(env):
    db, rec, flow = env
    card_id = db.cards()[0].id
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{card_id}")
    rec.statements += [statement([op("2026-08-10", "100")]), statement([op("2026-08-10", "100")])]
    [r] = flow.on_files(CHAT, [PDF], "")                  # не тот месяц: ничего не взято
    assert "Принято операций: 0" in r.text
    flow.on_command(CHAT, "done")
    flow.on_command(CHAT, "sverka", "2026-08")
    flow.on_button(CHAT, f"s:c:{card_id}")
    [r] = flow.on_files(CHAT, [PDF], "")
    assert "Принято операций: 1" in r.text


def test_rule_not_learned_from_statement_record(env):
    db, rec, flow = env
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[0].id}")
    rec.statements.append(statement([op("2026-09-02", "500", desc="Оплата у ИП Сидоров")]))
    flow.on_files(CHAT, [PDF], "")
    flow.on_command(CHAT, "done")
    line = db.statement(db.cards()[0].id, "2026-09")[1][0]
    flow.on_command(CHAT, "biz", str(line.id))
    answer(flow, f"d:cat:{db.category_id('Прочее')}")
    e = db.expenses("2026-09")[0]
    flow.on_button(CHAT, f"e:cat:{e.id}:{db.category_id('Аренда')}")
    assert db.rules() == []


def test_receipts_not_kept_for_skipped_or_non_payments(env):
    db, rec, flow = env
    rec.payments += [{**payment(), "is_payment": False}, payment(direction="in"),
                     payment(currency="USD"), payment()]
    flow.on_files(CHAT, [png()], "")
    flow.on_files(CHAT, [png()], "")
    flow.on_files(CHAT, [png()], "")
    answer(flow, "d:skip")
    flow.on_files(CHAT, [png()], "")
    files = [f for _, _, fs in os.walk(flow.receipts_dir) for f in fs]
    assert len(files) == 1 and os.path.exists(db.expenses("2026-09")[0].receipt_path)
    flow.on_button(CHAT, f"e:del:{db.expenses('2026-09')[0].id}")
    assert [f for _, _, fs in os.walk(flow.receipts_dir) for f in fs] == []


def test_leap_day_without_year():
    assert _parse_date("29.02", date(2028, 3, 5)) == "2028-02-29"
    assert _parse_date("30.12", date(2026, 9, 26)) == "2025-12-30"
    assert _parse_date("31.02", date(2026, 9, 26)) is None


def test_forgotten_done_closes_statement_after_two_hours(env):
    db, rec, flow = env
    now = [1_000_000.0]
    flow.clock = lambda: now[0]
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{db.cards()[0].id}")
    now[0] += 3600                                   # через час — ещё сверка
    rec.statements.append(statement([op("2026-09-02", "100")]))
    assert "Принято операций: 1" in flow.on_files(CHAT, [PDF], "")[0].text
    now[0] += 3 * 3600                               # забыли /done
    rec.payments.append(payment())
    closed, saved = flow.on_files(CHAT, [png()], "")
    assert "закрыл сам" in closed.text and saved.text.startswith("✅")
    assert "statement" not in db.get_state(CHAT)

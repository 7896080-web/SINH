import os

from conftest import CHAT, PNG, payment
from finance.storage import BUSINESS, PERSONAL, REIMBURSEMENT


def buttons(reply):
    return [data for row in reply.buttons for _, data in row]


def test_clear_screenshot_saved_without_questions(env):
    db, rec, flow = env
    rec.payments.append(payment())
    [reply] = flow.on_files(CHAT, [PNG], "")
    assert reply.text.startswith("✅ Записано")
    [e] = db.expenses("2026-09")
    assert (e.amount, e.card, e.purpose, e.category) == (150000, "Сбер", BUSINESS, "Логистика и доставка")
    assert os.path.exists(e.receipt_path)
    assert "e:del:%d" % e.id in buttons(reply)
    assert db.get_state(CHAT) == {}


def test_caption_and_cards_passed_to_model(env):
    db, rec, flow = env
    rec.payments.append(payment())
    flow.on_files(CHAT, [PNG], "реклама вк")
    _, files, text, kw = rec.calls[0]
    assert text == "реклама вк" and files == [PNG]
    assert len(kw["cards"]) == 3 and "Прочее" in kw["categories"]


def test_unknown_card_and_unsure_category_are_asked(env):
    db, rec, flow = env
    rec.payments.append(payment(card_last4="", bank="", category_confident=False))
    [q1] = flow.on_files(CHAT, [PNG], "")
    assert "С какой карты" in q1.text
    card_id = db.cards()[1].id
    [q2] = flow.on_button(CHAT, f"d:card:{card_id}")
    assert "статья" in q2.text.lower()
    assert any(label.startswith("✓ Логистика") for row in q2.buttons for label, _ in row)
    cat = db.category_id("Реклама и продвижение")
    [saved] = flow.on_button(CHAT, f"d:cat:{cat}")
    assert "Тинькофф" in saved.text
    assert db.expenses("2026-09")[0].category == "Реклама и продвижение"


def test_card_guessed_by_bank_name(env):
    db, rec, flow = env
    rec.payments.append(payment(card_last4="", bank="Т-банк"))
    flow.on_files(CHAT, [PNG], "")
    assert db.expenses("2026-09")[0].card == "Тинькофф"


def test_looks_personal_asks_purpose(env):
    db, rec, flow = env
    rec.payments.append(payment(looks_personal=True, category="", category_confident=False))
    [q] = flow.on_files(CHAT, [PNG], "")
    assert "d:purpose:personal" in buttons(q)
    [saved] = flow.on_button(CHAT, "d:purpose:personal")
    e = db.expenses("2026-09")[0]
    assert e.purpose == PERSONAL and e.category is None
    assert "Личное" in saved.text


def test_incoming_is_reimbursement_or_skipped(env):
    db, rec, flow = env
    rec.payments += [payment(direction="in"), payment(direction="in", amount="99")]
    [q] = flow.on_files(CHAT, [PNG], "")
    assert "поступление" in q.text
    flow.on_button(CHAT, "d:kind:reimb")
    flow.on_files(CHAT, [PNG], "")
    flow.on_button(CHAT, "d:skip")
    [e] = db.expenses("2026-09")
    assert e.kind == REIMBURSEMENT and e.category is None


def test_foreign_currency_asks_rubles(env):
    db, rec, flow = env
    rec.payments.append(payment(currency="USD", amount="20"))
    [q] = flow.on_files(CHAT, [PNG], "")
    assert "USD" in q.text
    [again] = flow.on_text(CHAT, "двадцать")
    assert "Не понял" in again.text
    flow.on_text(CHAT, "1 845,30")
    assert db.expenses("2026-09")[0].amount == 184530


def test_missing_date_accepts_text(env):
    db, rec, flow = env
    rec.payments.append(payment(date=""))
    [q] = flow.on_files(CHAT, [PNG], "")
    assert "d:date:1" in buttons(q)
    flow.on_text(CHAT, "05.09")
    assert db.expenses("2026-09")[0].op_date == "2026-09-05"


def test_duplicate_detected(env):
    db, rec, flow = env
    rec.payments += [payment(), payment(), payment()]
    flow.on_files(CHAT, [PNG], "")
    [q] = flow.on_files(CHAT, [PNG], "")
    assert "уже записана" in q.text
    flow.on_button(CHAT, "d:skip")
    flow.on_files(CHAT, [PNG], "")
    flow.on_button(CHAT, "d:dup:ok")
    assert len(db.expenses("2026-09")) == 2


def test_screenshots_queue_while_question_open(env):
    db, rec, flow = env
    rec.payments += [payment(card_last4="", bank=""), payment(amount="700", merchant="Ozon")]
    flow.on_files(CHAT, [PNG], "")
    [queued] = flow.on_files(CHAT, [PNG], "")
    assert "в очереди: 1" in queued.text
    replies = flow.on_button(CHAT, f"d:card:{db.cards()[0].id}")
    assert [r.text.startswith("✅") for r in replies] == [True, True]
    assert sorted(e.amount for e in db.expenses("2026-09")) == [70000, 150000]


def test_text_expense_and_non_payment(env):
    db, rec, flow = env
    [hint] = flow.on_text(CHAT, "привет")
    assert "скриншот" in hint.text and rec.calls == []
    rec.payments += [payment(), {**payment(), "is_payment": False}]
    flow.on_text(CHAT, "1500 сдэк вчера со сбера")
    assert rec.calls[0][1] == [] and len(db.expenses("2026-09")) == 1
    [r] = flow.on_files(CHAT, [PNG], "")
    assert "Не вижу" in r.text


def test_edit_saved_expense(env):
    db, rec, flow = env
    rec.payments.append(payment())
    flow.on_files(CHAT, [PNG], "")
    eid = db.expenses("2026-09")[0].id
    [r] = flow.on_button(CHAT, f"e:purpose:{eid}:personal")
    assert db.expense(eid).purpose == PERSONAL and "Это бизнес" in str(r.buttons)
    [pick] = flow.on_button(CHAT, f"e:card:{eid}")
    flow.on_button(CHAT, buttons(pick)[2])
    assert db.expense(eid).card == "Альфа"
    cat = db.category_id("Прочее")
    flow.on_button(CHAT, f"e:cat:{eid}:{cat}")
    assert db.expense(eid).category == "Прочее" and db.expense(eid).purpose == BUSINESS
    flow.on_button(CHAT, f"e:del:{eid}")
    assert db.expense(eid) is None
    [gone] = flow.on_button(CHAT, f"e:del:{eid}")
    assert "уже нет" in gone.text


def test_no_cards_yet(tmp_path):
    from conftest import FakeRecognizer
    from finance.flow import Flow
    from finance.storage import Storage
    db = Storage(str(tmp_path / "x.db"))
    rec = FakeRecognizer()
    flow = Flow(db, rec, str(tmp_path / "r"))
    rec.payments.append(payment())
    [r] = flow.on_files(CHAT, [PNG], "")
    assert "/addcard" in r.text
    flow.on_command(CHAT, "addcard", "Сбер 1111 Сбер")
    [saved] = flow.on_button(CHAT, "d:retry")
    assert saved.text.startswith("✅")


def test_pdf_outside_sverka_is_not_a_payment(env):
    db, rec, flow = env
    [r] = flow.on_files(CHAT, [(b"%PDF", "application/pdf")], "")
    assert "/sverka" in r.text and rec.calls == []


def test_cancel_clears_queue(env):
    db, rec, flow = env
    rec.payments.append(payment(card_last4="", bank=""))
    flow.on_files(CHAT, [PNG], "")
    flow.on_command(CHAT, "cancel")
    assert db.get_state(CHAT) == {}
    [r] = flow.on_button(CHAT, "d:card:1")
    assert "неактуален" in r.text

"""Расчётный счёт ИП с бизнес-картой (образцы ПСБ). Номера счетов вымышленные."""
import pytest

from conftest import CHAT, PNG, TODAY, FakeRecognizer, payment
from finance.flow import Flow
from finance.reconcile import summarize
from finance.storage import Storage
from test_sverka import PDF, statement


@pytest.fixture
def biz(tmp_path):
    db = Storage(str(tmp_path / "f.db"))
    rec = FakeRecognizer()
    flow = Flow(db, rec, str(tmp_path / "r"), today=lambda: TODAY)
    flow.on_command(CHAT, "addcard", "ВТБ 5501 ВТБ")
    [added] = flow.on_command(CHAT, "addcard", "ПСБ ПСБ бизнес")
    assert "бизнес-счёт" in added.text
    yield db, rec, flow
    db.close()


def card(db, name):
    return next(c for c in db.cards() if c.name == name)


def kontur(**over):
    # «Платёжное поручение №… · Получатель АО "ПФ "СКБ КОНТУР" · 2 950,00 ₽ ·
    #  Оплата по счету … через СБП для ЮЛ · Плательщик ИП … 40802…0000»
    fields = dict(amount="2950", date="2026-07-30", card_last4="40802810000000000000",
                  bank="ПСБ", merchant="АО «ПФ «СКБ Контур»",
                  description="оплата по счёту (сервис Контур)",
                  category="Связь, сервисы, подписки", category_confident=True,
                  from_business_account=True, to_owner=False)
    return payment(**{**fields, **over})


def test_addcard_business_and_cards_list(biz):
    db, rec, flow = biz
    assert card(db, "ПСБ").is_business and not card(db, "ВТБ").is_business
    [r] = flow.on_command(CHAT, "cards")
    assert "🏦 ПСБ (ПСБ) — бизнес-счёт" in r.text and "💳 ВТБ" in r.text


def test_payment_order_goes_to_business_account_and_not_owed(biz):
    db, rec, flow = biz
    rec.payments.append(kontur())
    [saved] = flow.on_files(CHAT, [PNG], "")
    assert saved.text.startswith("✅ Записано")
    assert "к возмещению не добавляется" in saved.text
    assert card(db, "ПСБ").numbers == ["0000"]  # номер счёта запомнен
    assert db.owed_until("2026-07") == 0


def test_b2c_transfer_to_other_person_is_business_expense(biz):
    db, rec, flow = biz
    # «Перевод через СБП (B2C) по номеру телефона · Получатель ПЕТРОВ П. П. · 20 000,00 ₽»
    rec.payments.append(kontur(amount="20000", date="2026-08-07", merchant="Петров П. П.",
                               description="перевод по номеру телефона",
                               category="Подрядчики и зарплата", category_confident=False))
    [q] = flow.on_files(CHAT, [PNG], "")
    assert "статья" in q.text.lower()
    flow.on_button(CHAT, f"d:cat:{db.category_id('Подрядчики и зарплата')}")
    [e] = db.expenses("2026-08")
    assert e.card == "ПСБ" and e.category == "Подрядчики и зарплата" and e.owed_effect == 0


def test_transfer_to_owner_from_business_is_reimbursement(biz):
    db, rec, flow = biz
    rec.payments.append(payment(card_last4="5501", bank="ВТБ", amount="10000",
                                date="2026-09-03", category="Реклама и продвижение"))
    flow.on_files(CHAT, [PNG], "")
    rec.payments.append(kontur(amount="3000", date="2026-09-10", merchant="Иванов И. И.",
                               to_owner=True, category="", category_confident=False))
    [saved] = flow.on_files(CHAT, [PNG], "")
    assert "Перевод вам с бизнес-счёта" in saved.text
    rec.payments.append(kontur(amount="500", date="2026-09-11", looks_personal=True))
    flow.on_files(CHAT, [PNG], "")
    [q_reply] = flow.on_button(CHAT, "d:purpose:personal")
    assert "Личное из денег бизнеса" in q_reply.text
    rec.payments.append(kontur(amount="2950", date="2026-09-12"))
    flow.on_files(CHAT, [PNG], "")

    s = summarize(db, "2026-09")
    assert (s.business, s.business_account_spent, s.reimbursed, s.owed) == (
        1000000, 295000, 350000, 650000)
    [itog] = flow.on_command(CHAT, "itog", "2026-09")
    assert "🏦 ПСБ" in itog.text and "Расходы с бизнес-счёта (записанные): 2 950,00 ₽" in itog.text
    assert "Бизнес должен вам за месяц: 6 500,00 ₽" in itog.text
    assert db.owed_until("2026-09") == 650000


def test_income_to_business_account_not_recorded(biz):
    db, rec, flow = biz
    # «ЮЖНЫЙ Ф-Л ПАО "Банк ПСБ" +236 ₽ · Начисление кэшбэка по бизнес-карте»
    rec.payments.append(kontur(direction="in", amount="236", merchant="Банк ПСБ",
                               description="кэшбэк по бизнес-карте"))
    [r] = flow.on_files(CHAT, [PNG], "")
    assert "поступление на бизнес-счёт" in r.text and db.expenses("2026-07") == []
    assert db.get_state(CHAT) == {}


def test_transfer_between_own_personal_accounts_not_recorded(biz):
    db, rec, flow = biz
    rec.payments.append(payment(card_last4="5501", to_owner=True))
    [r] = flow.on_files(CHAT, [PNG], "")
    assert "между вашими счетами" in r.text and db.expenses("2026-09") == []


def test_business_account_sverka(biz):
    db, rec, flow = biz
    rec.payments.append(kontur(date="2026-09-05"))
    flow.on_files(CHAT, [PNG], "")
    psb = card(db, "ПСБ")
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{psb.id}")

    def line(d, amount, direction, desc, own=False, cat=""):
        return {"date": d, "time": "", "amount": amount, "direction": direction,
                "description": desc, "own_transfer": own, "business_category": cat}
    # Экран «Выписка по счёту · Расчётный счёт · На начало 917,55 · Пришло +236 · Ушло …»
    rec.statements.append(statement([
        line("2026-09-21", "236", "in", "Банк ПСБ, кэшбэк по бизнес-карте"),
        line("2026-09-05", "2950", "out", "СКБ Контур", cat="Связь, сервисы, подписки"),
        line("2026-09-07", "20000", "out", "Петров П. П., перевод СБП B2C"),
        line("2026-09-15", "5000", "out", "Иванов И. И., перевод себе", own=True),
    ], total_in="236", total_out="27950", last4=""))
    flow.on_files(CHAT, [PNG], "")
    replies = flow.on_command(CHAT, "done")
    text = "\n".join(r.text for r in replies)
    assert "🏦 ПСБ ·0000 (бизнес-счёт) — сентябрь 2026" in text
    assert "переведено вам: 5 000,00 ₽" in text and "расходы бизнеса: 22 950,00 ₽" in text
    assert "на личное" not in text
    [owner] = [r for r in replies if r.text.startswith("💸")]
    [spend] = [r for r in replies if r.text.startswith("📋")]
    assert "Петров" in spend.text and "Контур" not in spend.text  # Контур уже записан
    assert "→ Прочее" in spend.text

    [back] = flow.on_button(CHAT, "s:reimb")
    assert back.text.startswith("✅ Учтено переводов вам: 1 на 5 000,00 ₽")
    [rec_spend] = flow.on_button(CHAT, "s:acc")
    assert "Прочее: 20 000,00 ₽" in rec_spend.text

    s = summarize(db, "2026-09")
    cs = s.business_accounts[0]
    assert (cs.business, cs.reimbursed, cs.missing, cs.unmatched_out, cs.unmatched_own_out) == (
        2295000, 500000, [], [], [])
    assert s.total("total_out") is None  # бизнес-счёт не входит в итоги по личным картам
    assert db.owed_until("2026-09") == -500000  # бизнес перевёл вам больше, чем вы потратили за него

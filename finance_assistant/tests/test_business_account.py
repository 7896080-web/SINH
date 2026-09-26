"""Расчётный счёт ИП с бизнес-картой (образцы ПСБ). Номера счетов вымышленные."""
import pytest

from conftest import CHAT, png, TODAY, FakeRecognizer, payment
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
                  from_business_account=True, own_transfer=False)
    return payment(**{**fields, **over})


def test_addcard_business_and_cards_list(biz):
    db, rec, flow = biz
    assert card(db, "ПСБ").is_business and not card(db, "ВТБ").is_business
    [r] = flow.on_command(CHAT, "cards")
    assert "🏦 ПСБ (ПСБ) — бизнес-счёт" in r.text and "💳 ВТБ" in r.text


def test_payment_order_goes_to_business_account(biz):
    db, rec, flow = biz
    rec.payments.append(kontur())
    [saved] = flow.on_files(CHAT, [png()], "")
    assert saved.text.startswith("✅ Записано") and "ПСБ" in saved.text
    assert "Бизнес · Связь, сервисы, подписки" in saved.text
    assert card(db, "ПСБ").numbers == ["0000"]  # номер счёта запомнен


def test_b2c_transfer_to_other_person_is_business_expense(biz):
    db, rec, flow = biz
    # «Перевод через СБП (B2C) по номеру телефона · Получатель — другой человек · 20 000,00 ₽»
    rec.payments.append(kontur(amount="20000", date="2026-08-07", merchant="Петров П. П.",
                               description="перевод по номеру телефона",
                               category="Подрядчики и зарплата", category_confident=False))
    [q] = flow.on_files(CHAT, [png()], "")
    assert "статья" in q.text.lower()
    flow.on_button(CHAT, f"d:cat:{db.category_id('Подрядчики и зарплата')}")
    [e] = db.expenses("2026-08")
    assert e.card == "ПСБ" and e.category == "Подрядчики и зарплата"


def test_income_to_business_account_not_recorded(biz):
    db, rec, flow = biz
    # «ЮЖНЫЙ Ф-Л ПАО "Банк ПСБ" +236 ₽ · Начисление кэшбэка по бизнес-карте»
    rec.payments.append(kontur(direction="in", amount="236", merchant="Банк ПСБ"))
    [r] = flow.on_files(CHAT, [png()], "")
    assert "поступление — не записываю" in r.text
    assert db.expenses("2026-07") == [] and db.get_state(CHAT) == {}


def test_transfer_from_business_account_to_owner_card(biz):
    db, rec, flow = biz
    rec.payments.append(kontur(amount="3000", merchant="Иванов И. И.", own_transfer=True,
                               counterparty_last4="5501", counterparty_bank="ВТБ"))
    [r] = flow.on_files(CHAT, [png()], "")
    assert r.text.startswith("🔁 Перевод между своими счетами П1")
    assert "ПСБ → ВТБ" in r.text
    assert db.expenses("2026-07") == []


def test_month_totals_business_by_category_and_personal(biz):
    db, rec, flow = biz
    rec.payments.append(payment(card_last4="5501", bank="ВТБ", amount="10000",
                                date="2026-09-03", category="Реклама и продвижение"))
    flow.on_files(CHAT, [png()], "")
    rec.payments.append(kontur(amount="500", date="2026-09-11", looks_personal=True))
    flow.on_files(CHAT, [png()], "")
    [saved] = flow.on_button(CHAT, "d:purpose:personal")
    assert "Личное (с бизнес-счёта)" in saved.text
    rec.payments.append(kontur(amount="2950", date="2026-09-12"))
    flow.on_files(CHAT, [png()], "")

    s = summarize(db, "2026-09")
    assert (s.business, s.personal) == (1295000, 50000)
    assert s.by_category == {"Реклама и продвижение": 1000000, "Связь, сервисы, подписки": 295000}
    [itog] = flow.on_command(CHAT, "itog", "2026-09")
    assert "💼 Ушло на бизнес: 12 950,00 ₽" in itog.text
    assert "• Реклама и продвижение: 10 000,00 ₽" in itog.text
    assert "🏠 Личные расходы: 500,00 ₽" in itog.text
    assert "без выписки, личное не посчитано: ВТБ ·5501" in itog.text
    assert "должен" not in itog.text


def test_business_account_sverka(biz):
    db, rec, flow = biz
    rec.payments.append(kontur(date="2026-09-05"))
    flow.on_files(CHAT, [png()], "")
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
    flow.on_files(CHAT, [png()], "")
    replies = flow.on_command(CHAT, "done")
    text = "\n".join(r.text for r in replies)
    assert "🏦 ПСБ ·0000 (бизнес-счёт) — сентябрь 2026" in text
    assert "переведено вам: 5 000,00 ₽" in text
    # Всё, что ушло, кроме перевода себе, — бизнес, даже без статьи.
    assert "на бизнес: 22 950,00 ₽" in text and "• Без статьи: 20 000,00 ₽" in text
    assert "возмещ" not in text.lower()
    [spend] = [r for r in replies if r.text.startswith("📋")]
    assert "Петров" in spend.text and "Контур" not in spend.text  # Контур уже записан

    [done] = flow.on_button(CHAT, "s:acc")
    assert "Прочее: 20 000,00 ₽" in done.text
    s = summarize(db, "2026-09")
    cs = s.business_accounts[0]
    assert (cs.business, cs.personal, cs.missing, cs.unmatched_out) == (2295000, 0, [], [])
    assert s.by_category == {"Прочее": 2000000, "Связь, сервисы, подписки": 295000}

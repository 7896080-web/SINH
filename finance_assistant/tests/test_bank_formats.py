"""Сценарии по реальным образцам банка «Россия» (ABR Direct) и ВТБ.

Ответы модели здесь заданы вручную так, как их должен вернуть Claude по этим
скриншотам (по инструкциям из recognize.py). Номера карт/счетов вымышленные.
"""
from conftest import CHAT, png, FakeRecognizer, payment, answer
from finance.flow import Flow, _bank_key
from finance.reconcile import summarize
from finance.storage import Storage
from test_sverka import PDF, statement

import pytest


@pytest.fixture
def banks(tmp_path):
    db = Storage(str(tmp_path / "f.db"))
    rec = FakeRecognizer()
    from conftest import TODAY
    flow = Flow(db, rec, str(tmp_path / "r"), today=lambda: TODAY)
    flow.on_command(CHAT, "addcard", "ВТБ 5501 ВТБ")          # «Карта для жизни •5501»
    flow.on_command(CHAT, "addcard", "Россия Банк Россия")    # номер карты не знаем
    flow.on_command(CHAT, "addcard", "Сбер 3003 Сбер")
    yield db, rec, flow
    db.close()


def vtb_receipt(**over):
    # «Vata Production −14 000 ₽ · Развлечения · Счет списания: Мастер-счет •7702 ·
    #  17.09.2026 в 12:06»
    fields = dict(amount="14000", date="2026-09-17", card_last4="7702", bank="ВТБ",
                  merchant="Vata Production", description="услуги продакшн-студии",
                  category="Подрядчики и зарплата", category_confident=False,
                  looks_personal=False)
    return payment(**{**fields, **over})


def test_vtb_account_number_learned_after_one_question(banks):
    db, rec, flow = banks
    rec.payments.append(vtb_receipt())
    [q] = flow.on_files(CHAT, [png()], "")
    assert "С какой карты" in q.text  # •7702 незнаком, хотя банк ВТБ понятен
    vtb = next(c for c in db.cards() if c.name == "ВТБ")
    replies = answer(flow, f"d:card:{vtb.id}")
    assert replies[0].text.startswith("Запомнил: …7702")
    assert "статья" in replies[1].text.lower()
    answer(flow, f"d:cat:{db.category_id('Подрядчики и зарплата')}")
    assert db.card(vtb.id).numbers == ["5501", "7702"]

    # «Яндекс 360 −1 417 ₽ · Карта для жизни •5501» и снова Мастер-счет — без вопроса о карте
    rec.payments += [payment(amount="1417", date="2026-09-15", card_last4="5501", bank="ВТБ",
                             merchant="Яндекс 360", category="Связь, сервисы, подписки"),
                     vtb_receipt(amount="500", category_confident=True)]
    [a] = flow.on_files(CHAT, [png()], "")
    [b] = flow.on_files(CHAT, [png()], "")
    assert a.text.startswith("✅") and b.text.startswith("✅")
    assert {e.card for e in db.expenses("2026-09")} == {"ВТБ"}


def test_rossiya_card_found_by_bank_then_account_remembered(banks):
    db, rec, flow = banks
    # «ABR Direct · Оплата по QR-коду через СБП · Сервисы Яндекса · −122 000,00 ₽ ·
    #  Счет списания 40817…9012 · 24.09.2026, 17:58»
    rec.payments.append(payment(amount="122000", date="2026-09-24",
                                card_last4="40817810000000009012", bank="банк «Россия»",
                                merchant="Сервисы Яндекса (АО Яндекс Банк)",
                                description="оплата по QR-коду", category="Реклама и продвижение",
                                category_confident=False))
    [q] = flow.on_files(CHAT, [png()], "")
    # Номер незнаком, но у «России» номеров нет вовсе — всё равно спрашиваем
    # (по банку угадываем только когда номера не видно); спрашиваем карту.
    assert "С какой карты" in q.text
    rossiya = next(c for c in db.cards() if c.name == "Россия")
    replies = answer(flow, f"d:card:{rossiya.id}")
    assert "…9012" in replies[0].text
    assert "✓ Реклама и продвижение" in str(replies[1].buttons)


def test_card_by_bank_when_no_number_visible(banks):
    db, rec, flow = banks
    rec.payments.append(payment(card_last4="", bank="АБ РОССИЯ"))
    flow.on_files(CHAT, [png()], "")
    assert db.expenses("2026-09")[0].card == "Россия"


def test_bank_aliases():
    assert _bank_key("Т-Банк") == _bank_key("Тинькофф") == "тбанк"
    assert _bank_key("Банк «Россия»") == _bank_key("ABR Direct") == "россия"
    assert _bank_key("ВТБ Онлайн") == "втб"


def test_rossiya_statement_with_commission_and_times(banks):
    db, rec, flow = banks
    rossiya = next(c for c in db.cards() if c.name == "Россия")
    db.add_expense(op_date="2026-09-24", amount=12200000, card_id=rossiya.id,
                   category_id=db.category_id("Реклама и продвижение"), merchant="Сервисы Яндекса")
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{rossiya.id}")

    def line(d, t, amount, desc):
        return {"date": d, "time": t, "amount": amount, "direction": "out",
                "description": desc, "own_transfer": False, "business_category": ""}
    qr, phone = "Оплата по QR-коду через СБП", "Перевод по номеру телефона через СБП"
    # «Список операций за период с 19.09.2026 по 26.09.2026 · По всем продуктам»
    rec.statements.append(statement([
        line("2026-09-24", "18:03", "4277.00", qr),
        line("2026-09-24", "17:58", "122000.00", qr),
        line("2026-09-22", "09:38", "14000.00", phone),
        line("2026-09-21", "22:50", "22000.00", phone),
        line("2026-09-21", "22:50", "110.00", "Комиссия: " + phone),
        line("2026-09-21", "12:20", "14196.00", qr),
    ], last4=""))
    [got] = flow.on_files(CHAT, [PDF], "")
    assert "Принято операций: 6" in got.text
    card_reply, unmatched, _ = flow.on_command(CHAT, "done")
    assert "Ушло:   176 583,00 ₽" in card_reply.text
    assert "на бизнес: 122 000,00 ₽" in card_reply.text
    assert "на личное: 54 583,00 ₽" in card_reply.text
    assert "24.09 18:03  4 277,00 ₽  Оплата по QR-коду" in unmatched.text
    assert "17:58" not in unmatched.text  # сопоставлено со скриншотом


def test_vtb_history_screen_gives_month_totals(banks):
    db, rec, flow = banks
    vtb = next(c for c in db.cards() if c.name == "ВТБ")
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{vtb.id}")
    # Экран «История»: «Расходы в сентябре 615 736,05 ₽ · Поступления в сентябре 439 000 ₽»,
    # видны две операции за 25 сентября.
    rec.statements.append(statement([
        {"date": "2026-09-25", "time": "", "amount": "75", "direction": "out",
         "description": "Торговые автоматы", "own_transfer": False, "business_category": ""},
        {"date": "2026-09-25", "time": "", "amount": "199", "direction": "out",
         "description": "Яндекс Музыка", "own_transfer": False, "business_category": ""},
    ], total_in="439000", total_out="615736.05", last4=""))
    [got] = flow.on_files(CHAT, [png()], "")
    assert "Итоги из документа: пришло 439 000,00 ₽, ушло 615 736,05 ₽" in got.text
    cs = next(c for c in summarize(db, "2026-09").cards if c.card.id == vtb.id)
    assert (cs.total_in, cs.total_out) == (43900000, 61573605)


def test_history_screen_outside_sverka_points_to_it(banks):
    db, rec, flow = banks
    rec.payments.append({**payment(), "is_payment": False})
    [r] = flow.on_files(CHAT, [png()], "")
    assert "/sverka" in r.text and "истории" in r.text

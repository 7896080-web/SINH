"""Перемещения денег между своими счетами: не расход, исключаются из «пришло»/«ушло»."""
import pytest

from conftest import CHAT, PNG, TODAY, FakeRecognizer, payment
from finance.flow import Flow
from finance.reconcile import summarize
from finance.storage import Storage
from test_sverka import PDF, op, statement


@pytest.fixture
def own(tmp_path):
    db = Storage(str(tmp_path / "f.db"))
    rec = FakeRecognizer()
    flow = Flow(db, rec, str(tmp_path / "r"), today=lambda: TODAY)
    flow.on_command(CHAT, "addcard", "Россия 1111 Банк Россия")
    flow.on_command(CHAT, "addcard", "ВТБ 2222 ВТБ")
    yield db, rec, flow
    db.close()


def cid(db, name):
    return next(c.id for c in db.cards() if c.name == name)


def move(**over):
    fields = dict(amount="100000", date="2026-09-10", card_last4="1111", bank="Россия",
                  merchant="Перевод себе", description="перевод между своими счетами",
                  category="", category_confident=False, own_transfer=True,
                  counterparty_last4="2222", counterparty_bank="ВТБ")
    return payment(**{**fields, **over})


def test_outgoing_transfer_recorded_with_both_sides(own):
    db, rec, flow = own
    rec.payments.append(move())
    [r] = flow.on_files(CHAT, [PNG], "")
    assert r.text.startswith("🔁 Перевод между своими счетами П1")
    assert "100 000,00 ₽ · 10.09 · Россия → ВТБ" in r.text
    assert "t:del:1" in str(r.buttons)
    [t] = db.transfers("2026-09")
    assert (t.from_card_id, t.to_card_id) == (cid(db, "Россия"), cid(db, "ВТБ"))
    assert db.expenses("2026-09") == []


def test_same_transfer_from_other_side_not_doubled(own):
    db, rec, flow = own
    rec.payments.append(move(counterparty_last4="", counterparty_bank=""))
    [q] = flow.on_files(CHAT, [PNG], "")
    assert "Куда переведены деньги?" in q.text and "d:tr:0" in str(q.buttons)
    [saved] = flow.on_button(CHAT, "d:tr:0")
    assert "Россия → другой ваш счёт" in saved.text
    # Скриншот зачисления на ВТБ того же перевода, на день позже.
    rec.payments.append(move(direction="in", date="2026-09-11", card_last4="2222", bank="ВТБ",
                             counterparty_last4="1111", counterparty_bank="Россия"))
    [r] = flow.on_files(CHAT, [PNG], "")
    assert "уже записан (П1: Россия → ВТБ)" in r.text
    assert len(db.transfers("2026-09")) == 1


def test_incoming_transfer_asks_where_from(own):
    db, rec, flow = own
    rec.payments.append(move(direction="in", card_last4="2222", bank="ВТБ",
                             counterparty_last4="", counterparty_bank=""))
    [q] = flow.on_files(CHAT, [PNG], "")
    assert "Откуда пришли деньги?" in q.text
    [saved] = flow.on_button(CHAT, f"d:tr:{cid(db, 'Россия')}")
    assert "Россия → ВТБ" in saved.text


def test_edit_and_delete_transfer(own):
    db, rec, flow = own
    rec.payments.append(move())
    flow.on_files(CHAT, [PNG], "")
    [ask] = flow.on_button(CHAT, "t:to:1")
    assert "Куда перевод П1?" in ask.text
    [same] = flow.on_button(CHAT, f"t:to:1:{cid(db, 'Россия')}")
    assert "одна и та же карта" in same.text
    [r] = flow.on_button(CHAT, "t:to:1:0")
    assert "Россия → другой ваш счёт" in r.text
    [r] = flow.on_button(CHAT, "t:from:1:0")
    assert "Хотя бы одна сторона" in r.text
    assert "П1" in flow.on_command(CHAT, "fix", "п1")[0].text
    [lst] = flow.on_command(CHAT, "list")
    assert "Переводы между своими счетами:" in lst.text and "П1 10.09" in lst.text
    assert "удалить нельзя" in flow.on_command(CHAT, "delcard", str(cid(db, "Россия")))[0].text
    flow.on_button(CHAT, "t:del:1")
    assert db.transfers("2026-09") == []
    assert "уже нет" in flow.on_button(CHAT, "t:del:1")[0].text


def test_transfers_excluded_from_vtb_totals(own):
    """ВТБ: есть только итоги месяца с экрана «История» — переводы вычитаем сами."""
    db, rec, flow = own
    rec.payments += [move(),                                         # Россия → ВТБ 100 000
                     move(amount="50000", date="2026-09-20", card_last4="2222", bank="ВТБ",
                          counterparty_last4="1111", counterparty_bank="Россия")]  # ВТБ → Россия
    flow.on_files(CHAT, [PNG], "")
    flow.on_files(CHAT, [PNG], "")
    rec.payments.append(payment(amount="15417", date="2026-09-17", card_last4="2222", bank="ВТБ",
                                category="Подрядчики и зарплата"))
    flow.on_files(CHAT, [PNG], "")

    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{cid(db, 'ВТБ')}")
    rec.statements.append(statement([], total_in="439000", total_out="615736.05", last4=""))
    flow.on_files(CHAT, [PNG], "")
    card_reply, *_ = flow.on_command(CHAT, "done")
    assert "Пришло: 439 000,00 ₽" in card_reply.text
    assert "− переводы со своих счетов: 100 000,00 ₽" in card_reply.text
    assert "= приход без переводов: 339 000,00 ₽" in card_reply.text
    assert "− переводы на свои счета: 50 000,00 ₽" in card_reply.text
    # 615 736,05 − 50 000 своих − 15 417 бизнес
    assert "на личное: 550 319,05 ₽" in card_reply.text

    [itog] = flow.on_command(CHAT, "itog", "2026-09")
    assert "без переводов между своими: пришло 339 000,00 ₽ · ушло 565 736,05 ₽" in itog.text
    assert "📥 Пришло без переводов между своими счетами: 339 000,00 ₽" in itog.text


def test_statement_line_not_subtracted_twice(own):
    """Модель пометила строку как перевод, и он же записан вами — вычитаем один раз."""
    db, rec, flow = own
    rec.payments.append(move())
    flow.on_files(CHAT, [PNG], "")
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{cid(db, 'Россия')}")
    rec.statements.append(statement([
        op("2026-09-10", "100000", desc="Перевод на карту ВТБ *2222", own=True),
        op("2026-09-11", "30000", desc="Перевод себе по номеру телефона"),  # не распознан как свой
        op("2026-09-12", "4000", desc="Магазин"),
    ], last4=""))
    flow.on_files(CHAT, [PDF], "")
    flow.on_command(CHAT, "done")
    # Второй перевод модель не распознала — записываем его скриншотом.
    rec.payments.append(move(amount="30000", date="2026-09-11", counterparty_last4="",
                             counterparty_bank=""))
    flow.on_files(CHAT, [PNG], "")
    flow.on_button(CHAT, "d:tr:0")
    cs = next(c for c in summarize(db, "2026-09").cards if c.card.name == "Россия")
    assert (cs.total_out, cs.own_out, cs.personal) == (13400000, 13000000, 400000)
    assert [ln.description for ln in cs.unmatched_out] == ["Магазин"]
    assert cs.missing_transfers == []


def test_transfer_missing_in_statement_warned(own):
    db, rec, flow = own
    rec.payments.append(move(date="2026-09-25"))
    flow.on_files(CHAT, [PNG], "")
    flow.on_command(CHAT, "sverka", "2026-09")
    flow.on_button(CHAT, f"s:c:{cid(db, 'Россия')}")
    rec.statements.append(statement([op("2026-09-02", "500", desc="Кафе")], last4=""))
    flow.on_files(CHAT, [PDF], "")
    card_reply, *_ = flow.on_command(CHAT, "done")
    assert "Перевод записан, но в выписке не найден" in card_reply.text
    assert "П1 25.09  100 000,00 ₽  Россия → ВТБ" in card_reply.text

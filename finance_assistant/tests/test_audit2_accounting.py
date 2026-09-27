"""Регрессии второго аудита: точность учёта (суммы посчитаны вручную)."""
import io

import openpyxl
import pytest

from conftest import CHAT, payment, png
from finance.money import parse_amount
from finance.reconcile import summarize
from finance.report import month_xlsx
from finance.storage import BUSINESS, BUSINESS_ACCOUNT


def stmt(db, card, month, lines, tin=None, tout=None):
    sid = db.statement_id(card, month)
    db.add_statement_lines(sid, [dict(op_date=d, amount=a, direction=dr, description=ds,
                                      own_transfer=o) for d, a, dr, ds, o in lines])
    db.set_statement_totals(sid, tin, tout)


def card_summary(db, month, card_id):
    return next(c for c in summarize(db, month).cards if c.card.id == card_id)


def test_history_screen_with_totals_and_some_lines(env):
    """Экран «История» ВТБ: итог месяца + только видимые строки. Бизнес-расход,
    которого нет среди видимых строк, всё равно вычитается из личного."""
    db, _, _ = env
    vtb = db.add_card("ВТБ", "4444", "ВТБ").id
    db.add_expense(op_date="2026-09-03", amount=1_541_700, card_id=vtb, purpose=BUSINESS,
                   category_id=db.category_id("Реклама и продвижение"), merchant="Яндекс Директ")
    stmt(db, vtb, "2026-09", [("2026-09-25", 50_000, "out", "Пятёрочка", False),
                              ("2026-09-24", 120_000, "out", "Кафе", False)],
         tin=43_900_000, tout=61_573_605)
    cs = card_summary(db, "2026-09", vtb)
    assert cs.business == 1_541_700
    assert cs.personal == 61_573_605 - 1_541_700
    assert cs.missing == []


def test_complete_lines_still_flag_wrong_card(env):
    """Если строки полные (их сумма = итогу), не найденный расход — по-прежнему
    «не найдено в выписке» и из личного не вычитается."""
    db, _, _ = env
    sber = db.cards()[0].id
    db.add_expense(op_date="2026-09-03", amount=500_000, card_id=sber, purpose=BUSINESS,
                   category_id=db.category_id("Прочее"), merchant="X")
    stmt(db, sber, "2026-09", [("2026-09-10", 300_000, "out", "Магазин", False)], tout=300_000)
    cs = card_summary(db, "2026-09", sber)
    assert [e.amount for e in cs.missing] == [500_000] and cs.personal == 300_000


def test_transfer_sides_three_days_apart_are_one_transfer(env):
    db, rec, flow = env
    sber, tb = db.cards()[0].id, db.cards()[1].id
    common = dict(own_transfer=True, merchant="Перевод", category="", amount="100000.00")
    rec.payments.append(payment(direction="out", date="2026-09-25", card_last4="1111", bank="Сбер",
                                counterparty_last4="2222", counterparty_bank="Т-Банк", **common))
    flow.on_files(CHAT, [png()], "")
    rec.payments.append(payment(direction="in", date="2026-09-28", card_last4="2222",
                                bank="Т-Банк", counterparty_last4="1111",
                                counterparty_bank="Сбер", **common))
    [r] = flow.on_files(CHAT, [png()], "")
    assert "вторая сторона перевода П1" in r.text
    assert len(db.transfers("2026-09")) == 1
    stmt(db, sber, "2026-09", [("2026-09-25", 10_000_000, "out", "Перевод на Т-Банк", True),
                               ("2026-09-10", 3_000_000, "out", "Магазин", False)])
    stmt(db, tb, "2026-09", [("2026-09-28", 10_000_000, "in", "Перевод из Сбера", True)])
    s = summarize(db, "2026-09")
    assert card_summary(db, "2026-09", sber).personal == 3_000_000
    assert card_summary(db, "2026-09", tb).net_in == 0 and s.net_in == 0


def test_missing_transfer_and_flagged_line_not_subtracted_twice(env):
    """Перевод записан 20.09, а в выписке он 25.09 (разрыв больше 3 дней) и
    помечен как «свой перевод»: вычитается один раз."""
    db, _, _ = env
    sber = db.cards()[0].id
    db.add_transfer(op_date="2026-09-20", amount=10_000_000, from_card_id=sber, to_card_id=None,
                    description="", receipt_path="", seen_side="out")
    stmt(db, sber, "2026-09", [("2026-09-25", 10_000_000, "out", "Перевод себе", True),
                               ("2026-09-10", 3_000_000, "out", "Магазин", False)])
    cs = card_summary(db, "2026-09", sber)
    assert cs.own_out == 10_000_000 and cs.net_out == 3_000_000 and cs.personal == 3_000_000


def test_totals_only_month_keeps_its_expense(env):
    db, _, _ = env
    vtb = db.add_card("ВТБ", "4444", "ВТБ").id
    db.add_expense(op_date="2026-09-29", amount=500_000, card_id=vtb, purpose=BUSINESS,
                   category_id=db.category_id("Реклама и продвижение"), merchant="VK")
    stmt(db, vtb, "2026-09", [], tin=0, tout=4_000_000)            # только итоги
    stmt(db, vtb, "2026-10", [("2026-10-02", 500_000, "out", "Магнит", False)])
    sep, octo = card_summary(db, "2026-09", vtb), card_summary(db, "2026-10", vtb)
    assert (sep.business, sep.personal) == (500_000, 3_500_000)
    assert (octo.business, octo.personal) == (0, 500_000)


def test_statement_totals_ignored_when_document_spans_two_months(env):
    db, rec, flow = env
    sber = db.cards()[0].id
    flow.on_button(CHAT, "s:m:2026-09")
    flow.on_button(CHAT, f"s:c:{sber}")
    rec.statements.append({"is_statement": True, "card_last4": "1111",
                           "total_in": "100000", "total_out": "80000", "operations": [
                               {"date": "2026-08-20", "time": "", "amount": "60000", "direction": "out",
                                "description": "Август", "own_transfer": False},
                               {"date": "2026-08-25", "time": "", "amount": "100000", "direction": "in",
                                "description": "Зарплата", "own_transfer": False},
                               {"date": "2026-09-05", "time": "", "amount": "20000", "direction": "out",
                                "description": "Сентябрь", "own_transfer": False}]})
    [r] = flow.on_files(CHAT, [png()], "")
    assert "Итоги документа не взял" in r.text
    cs = card_summary(db, "2026-09", sber)
    assert (cs.total_in, cs.total_out) == (0, 2_000_000)


@pytest.mark.parametrize("choice,business,personal", [("own", 3_000_000, 0),
                                                      ("pers", 3_000_000, 5_000_000),
                                                      ("keepbiz", 8_000_000, 0)])
def test_notbiz_on_business_account_line(env, choice, business, personal):
    db, rec, flow = env
    psb = db.add_card("ПСБ", "4987", "ПСБ", kind=BUSINESS_ACCOUNT).id
    stmt(db, psb, "2026-09", [("2026-09-05", 3_000_000, "out", "СДЭК", False),
                              ("2026-09-10", 5_000_000, "out", "Перевод владельцу", False)])
    line = [ln.id for ln in db.card_lines(psb) if ln.amount == 5_000_000][0]
    replies = flow.on_command(CHAT, "notbiz", str(line))
    assert f"s:own:{line}" in str(replies[-1].buttons)       # спросил, что это
    flow.on_button(CHAT, f"s:{choice}:{line}")
    cs = card_summary(db, "2026-09", psb)
    assert (cs.business, cs.personal) == (business, personal)


@pytest.mark.parametrize("text,kopecks", [("12.500", 1_250_000), ("1,234", 123_400),
                                          ("100.000", 10_000_000), ("0.500", 50),
                                          ("1234.5", 123_450), ("1 234,50", 123_450)])
def test_thousand_separators(text, kopecks):
    assert parse_amount(text) == kopecks


@pytest.mark.parametrize("text", ["1234.505", "12.5000"])
def test_three_decimals_rejected(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_excel_uses_bank_date(env):
    """Оплата 30.09, банк провёл 01.10 — в Excel за октябрь дата 01.10."""
    db, _, _ = env
    sber = db.cards()[0].id
    db.add_expense(op_date="2026-09-30", amount=100_000, card_id=sber, purpose=BUSINESS,
                   category_id=db.category_id("Прочее"), merchant="X")
    stmt(db, sber, "2026-10", [("2026-10-01", 100_000, "out", "X", False)])
    wb = openpyxl.load_workbook(io.BytesIO(month_xlsx(summarize(db, "2026-10"))))
    rows = list(wb["Бизнес-расходы"].iter_rows(min_row=2, values_only=True))
    assert rows[0][1] == "2026-10-01"

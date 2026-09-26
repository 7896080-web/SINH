"""Регрессии по аудиту расчётов: двойной вычет, граница месяца, /svod = /itog."""
import os
import tempfile
from datetime import date

import pytest

from finance.pivot import Period, business_items
from finance.reconcile import summarize
from finance.storage import BUSINESS, PERSONAL, Storage


@pytest.fixture
def db():
    d = tempfile.mkdtemp()
    s = Storage(os.path.join(d, "f.db"))
    s.add_card("Сбер", "1111", "Сбер")
    s.add_card("ПСБ", "4987", "ПСБ", kind="business")
    yield s
    s.close()


SBER, PSB = 1, 2


def exp(db, d, amt, card, purpose=BUSINESS, c="Прочее"):
    return db.add_expense(op_date=d, amount=amt, card_id=card, purpose=purpose,
                          category_id=db.category_id(c) if purpose == BUSINESS else None)


def stmt(db, card, month, lines, tout=None):
    sid = db.statement_id(card, month)
    db.add_statement_lines(sid, [dict(op_date=d, amount=a, direction=dr, description=ds,
                                      own_transfer=own) for d, a, dr, ds, own in lines])
    db.set_statement_totals(sid, None, tout)


def card(db, month, card_id):
    return next(c for c in summarize(db, month).cards if c.card.id == card_id)


def svod_total(db, start, end):
    return sum(i.amount for i in business_items(
        db, Period(date.fromisoformat(start), date.fromisoformat(end), "x")))


def test_expense_does_not_take_transfer_line(db):
    exp(db, "2026-09-10", 5000, SBER)
    db.add_transfer(op_date="2026-09-10", amount=5000, from_card_id=SBER, to_card_id=None)
    stmt(db, SBER, "2026-09", [("2026-09-10", 5000, "out", "Перевод себе", True),
                               ("2026-09-10", 5000, "out", "СДЭК", False),
                               ("2026-09-15", 20000, "out", "Магазин", False)])
    cs = card(db, "2026-09", SBER)
    assert (cs.own_out, cs.business, cs.personal) == (5000, 5000, 20000)


def test_flagged_line_taken_by_expense_not_subtracted_again(db):
    exp(db, "2026-09-10", 5000, SBER)
    stmt(db, SBER, "2026-09", [("2026-09-10", 5000, "out", "СБП Петров", True),
                               ("2026-09-15", 20000, "out", "Магазин", False)])
    cs = card(db, "2026-09", SBER)
    assert (cs.own_out, cs.personal) == (0, 20000)


def test_transfer_across_month_boundary_subtracted_once(db):
    db.add_transfer(op_date="2026-09-30", amount=10000, from_card_id=SBER, to_card_id=None)
    stmt(db, SBER, "2026-09", [("2026-09-15", 4000, "out", "Магазин", False)])
    stmt(db, SBER, "2026-10", [("2026-10-01", 10000, "out", "Перевод себе", True),
                               ("2026-10-05", 2000, "out", "Кафе", False)])
    sep, oct_ = card(db, "2026-09", SBER), card(db, "2026-10", SBER)
    assert (sep.own_out, sep.personal) == (0, 4000)
    assert (oct_.own_out, oct_.personal) == (10000, 2000)


def test_expense_across_month_boundary(db):
    exp(db, "2026-09-30", 3000, PSB, c="Реклама и продвижение")
    exp(db, "2026-09-10", 7000, PSB)
    stmt(db, PSB, "2026-09", [("2026-09-10", 7000, "out", "Контур", False)])
    stmt(db, PSB, "2026-10", [("2026-10-01", 3000, "out", "Директ", False)])
    itog = summarize(db, "2026-09").business + summarize(db, "2026-10").business
    assert itog == 10000 == svod_total(db, "2026-09-01", "2026-10-31")
    assert summarize(db, "2026-10").by_category == {"Реклама и продвижение": 3000}
    # Личная карта: оплата 30.09 с проводкой 01.10 не делает сентябрь отрицательным.
    exp(db, "2026-09-30", 900, SBER)
    stmt(db, SBER, "2026-09", [("2026-09-10", 1000, "out", "Магазин", False)])
    stmt(db, SBER, "2026-10", [("2026-10-01", 900, "out", "СДЭК", False)])
    assert card(db, "2026-09", SBER).personal == 1000
    assert (card(db, "2026-10", SBER).personal, card(db, "2026-10", SBER).unmatched_out) == (0, [])


def test_matching_not_greedy(db):
    exp(db, "2026-09-10", 1000, PSB)
    exp(db, "2026-09-14", 1000, PSB)
    stmt(db, PSB, "2026-09", [("2026-09-07", 1000, "out", "a", False),
                              ("2026-09-11", 1000, "out", "b", False)])
    cs = card(db, "2026-09", PSB)
    assert (cs.missing, cs.unmatched_out) == ([], [])
    assert svod_total(db, "2026-09-01", "2026-09-30") == summarize(db, "2026-09").business == 2000


@pytest.mark.parametrize("purpose", [BUSINESS, PERSONAL])
def test_svod_equals_itog_on_business_account_with_unmatched_record(db, purpose):
    exp(db, "2026-09-01", 3000, PSB, purpose=purpose, c="Реклама и продвижение")
    stmt(db, PSB, "2026-09", [("2026-09-06", 3000, "out", "Директ", False),
                              ("2026-09-10", 7000, "out", "Контур", False)])
    itog = summarize(db, "2026-09").business
    assert svod_total(db, "2026-09-01", "2026-09-30") == itog


def test_unmatched_transfer_not_in_svod(db):
    db.add_transfer(op_date="2026-09-10", amount=50000, from_card_id=PSB, to_card_id=SBER)
    stmt(db, PSB, "2026-09", [("2026-09-14", 50000, "out", "Иванову", False),
                              ("2026-09-10", 7000, "out", "Контур", False)])
    assert summarize(db, "2026-09").business == 7000 == svod_total(db, "2026-09-01", "2026-09-30")


def test_personal_never_negative(db):
    exp(db, "2026-09-10", 8000, SBER)          # на самом деле оплачено другой картой
    stmt(db, SBER, "2026-09", [("2026-09-15", 2000, "out", "Магазин", False)])
    cs = card(db, "2026-09", SBER)
    assert cs.personal == 2000 and [e.amount for e in cs.missing] == [8000]
    # Только итоги (нет строк) — не проверить, но и в минус не уходим.
    exp(db, "2026-10-10", 8000, SBER)
    stmt(db, SBER, "2026-10", [], tout=2000)
    cs = card(db, "2026-10", SBER)
    assert (cs.personal, cs.overbooked) == (0, 6000)


def test_other_side_never_makes_same_card_transfer(db):
    db.add_transfer(op_date="2026-09-10", amount=10000, from_card_id=SBER, to_card_id=None,
                    seen_side="out")
    # Через день 10 000 пришли на тот же Сбер с неизвестного своего счёта — другой перевод.
    assert db.find_same_transfer(op_date="2026-09-11", amount=10000,
                                 from_card_id=None, to_card_id=SBER) is None

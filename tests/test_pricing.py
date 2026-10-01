"""Репрайсер: расчёт цены, ограничители и предложения (app/pricing.py).

Главное, что здесь закреплено, — ограничители: цена ниже пола минимальной
наценки не может быть подтверждена никак (ни расчётом, ни ручной ценой), а
большой шаг — только с отдельным подтверждением. И расчёт сам ничего не
отправляет: он только создаёт предложения.
"""
from decimal import Decimal

from app.models import (Barcode, Platform, PriceChange, PriceChangeStatus, PriceRule, Product,
                        ProductPrice, SyncSetting)
from app.pricing import (BLOCK_FLOOR, BLOCK_MAX_CHANGE, approve, decide_price, recalculate_account,
                         round_price)
from tests.factories import make_account


def _rule(**kw) -> PriceRule:
    base = dict(markup_percent=100, fixed_add=0, round_step=1, round_minus=0,
                min_margin_percent=30, max_change_percent=20)
    base.update(kw)
    return PriceRule(**base)


def _product(db, uid="u1", cost="500.00", account=None, enabled=True):
    p = Product(uid_1c=uid, article=f"A-{uid}", name="Джинсы", size="46",
                cost_price=Decimal(cost) if cost is not None else None)
    db.add(p)
    db.add(Barcode(barcode=f"bc-{uid}", uid_1c=uid))
    if account is not None:
        db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=enabled))
    db.commit()
    return p


# ------------------------------------------------------------- округление

def test_round_up_to_step_with_nice_ending():
    assert round_price(Decimal("1201"), 100, 1) == 1299
    assert round_price(Decimal("1300"), 100, 1) == 1399   # 1299 < 1300 — следующий шаг
    assert round_price(Decimal("1299.5"), 100, 1) == 1399
    assert round_price(Decimal("1299"), 100, 1) == 1299


def test_round_never_goes_below_raw():
    assert round_price(Decimal("1000.01"), 1, 0) == 1001
    assert round_price(Decimal("1000"), 10, 0) == 1000
    assert round_price(Decimal("1001"), 10, 0) == 1010


def test_minus_not_less_than_step_is_ignored():
    assert round_price(Decimal("1201"), 10, 10) == 1210


# ------------------------------------------------------------- решение

def test_rule_price_markup_and_fixed_add():
    d = decide_price(Decimal("500"), _rule(markup_percent=150, fixed_add=99), None, None)
    assert d.new_price == 1349 and d.block_reason is None and d.source == "rule"


def test_no_cost_no_price():
    d = decide_price(None, _rule(), None, None)
    assert d.new_price is None and "себестоимост" in d.note


def test_unconfigured_rule_gives_no_price():
    d = decide_price(Decimal("500"), _rule(markup_percent=0), None, None)
    assert d.new_price is None and "не настроено" in d.note
    # ручная цена при этом работает
    assert decide_price(Decimal("500"), _rule(markup_percent=0, min_margin_percent=0), 700, None).new_price == 700


def test_floor_blocks_rule_price():
    # наценка 20% при полу 30% — расчёт сам ниже пола
    d = decide_price(Decimal("1000"), _rule(markup_percent=20, min_margin_percent=30), None, None)
    assert d.new_price == 1200 and d.block_reason == BLOCK_FLOOR


def test_floor_blocks_manual_price_too():
    d = decide_price(Decimal("1000"), _rule(), manual_price=1100, last_sent_price=None)
    assert d.source == "manual" and d.block_reason == BLOCK_FLOOR


def test_manual_price_without_cost_is_allowed_with_warning():
    d = decide_price(None, _rule(), manual_price=900, last_sent_price=None)
    assert d.new_price == 900 and d.block_reason is None and "пол не проверен" in d.note


def test_large_change_is_blocked():
    d = decide_price(Decimal("500"), _rule(), None, last_sent_price=700)    # 1000 vs 700 = +42.9%
    assert d.new_price == 1000 and d.block_reason == BLOCK_MAX_CHANGE


def test_change_within_limit_passes():
    d = decide_price(Decimal("500"), _rule(), None, last_sent_price=900)    # +11%
    assert d.block_reason is None


def test_first_price_has_no_change_limit():
    d = decide_price(Decimal("500"), _rule(max_change_percent=1), None, last_sent_price=None)
    assert d.block_reason is None


# ------------------------------------------------------------- предложения

def test_recalculate_only_enabled_products_and_sends_nothing(db):
    account = make_account(db)
    _product(db, "u1", "500", account)
    _product(db, "u2", "500", account, enabled=False)
    _product(db, "u3", None, account)
    db.add(_rule(account_id=account.id))
    db.commit()

    stats = recalculate_account(db, account)

    assert stats == {"proposed": 1, "blocked": 0, "unchanged": 0, "no_cost": 1}
    changes = db.query(PriceChange).all()
    assert [(c.uid_1c, c.new_price, c.status) for c in changes] == [("u1", 1000, PriceChangeStatus.proposed)]
    assert db.query(ProductPrice).count() == 0          # ничего не «отправлено»


def test_recalculate_supersedes_open_but_keeps_approved(db):
    account = make_account(db)
    _product(db, "u1", "500", account)
    db.add(_rule(account_id=account.id))
    db.commit()
    recalculate_account(db, account)
    first = db.query(PriceChange).one()
    first.status = PriceChangeStatus.approved
    db.commit()

    recalculate_account(db, account)
    recalculate_account(db, account)

    statuses = sorted(c.status.value for c in db.query(PriceChange).all())
    assert statuses == ["approved", "proposed", "rejected"]


def test_unchanged_price_gets_no_proposal(db):
    account = make_account(db)
    _product(db, "u1", "500", account)
    db.add(_rule(account_id=account.id))
    db.add(ProductPrice(uid_1c="u1", account_id=account.id, last_sent_price=1000))
    db.commit()

    assert recalculate_account(db, account)["unchanged"] == 1
    assert db.query(PriceChange).count() == 0


def test_rules_are_per_account(db):
    wb = make_account(db, Platform.wb, "WB")
    ozon = make_account(db, Platform.ozon, "Ozon")
    _product(db, "u1", "500", wb)
    db.add(SyncSetting(uid_1c="u1", account_id=ozon.id, enabled=True))
    db.add(_rule(account_id=wb.id, markup_percent=100))
    db.add(_rule(account_id=ozon.id, markup_percent=140))
    db.commit()

    recalculate_account(db, wb)
    recalculate_account(db, ozon)

    by_account = {c.account_id: c.new_price for c in db.query(PriceChange).all()}
    assert by_account == {wb.id: 1000, ozon.id: 1200}


# ------------------------------------------------------------- подтверждение

def test_floor_can_never_be_approved():
    c = PriceChange(new_price=1, status=PriceChangeStatus.blocked, block_reason=BLOCK_FLOOR)
    assert approve(c, "admin", confirm_large=True) is not None
    assert c.status == PriceChangeStatus.blocked


def test_large_change_needs_explicit_confirmation():
    c = PriceChange(new_price=1, status=PriceChangeStatus.blocked, block_reason=BLOCK_MAX_CHANGE)
    assert approve(c, "admin") is not None
    assert c.status == PriceChangeStatus.blocked
    assert approve(c, "admin", confirm_large=True) is None
    assert c.status == PriceChangeStatus.approved and c.decided_by == "admin"


def test_already_decided_is_not_reapproved():
    c = PriceChange(new_price=1, status=PriceChangeStatus.rejected)
    assert approve(c, "admin") == "уже решено"

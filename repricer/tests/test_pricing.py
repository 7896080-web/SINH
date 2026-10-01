"""Расчёт цены: экономика заказчика и ограничители.

себестоимость ₽ = себестоимость $ × курс; к получению = цена × (1 − комиссия);
наценка = к получению − себестоимость ₽.
"""
from decimal import Decimal

from priceapp.models import PlatformRule, PriceChange, ProductPrice
from priceapp.pricing import (BLOCK_FLOOR, BLOCK_MAX_CHANGE, approve, decide, markup, payout,
                              recalculate_account, round_price)
from tests import factories as f

RATE = Decimal("81.5")


def _rule(**kw):
    base = dict(markup_coef=2, round_step=10, round_minus=1, min_markup_coef=Decimal("1.3"),
                max_change_percent=20)
    base.update(kw)
    return PlatformRule(**base)


def test_customer_example_sweatshirt_16_24_usd():
    """Свитшот 39681 GRI MELANG: «Цена СС» 16,24 $, WB комиссия 25%."""
    d = decide(Decimal("16.24"), RATE, 25, _rule(), None, None)
    assert d.cost_rub == Decimal("1323.56")              # 16,24 × 81,5
    assert d.new_price == 3539                            # 1323,56 × 2 / 0,75 = 3529,49 → вверх до 10, −1
    assert payout(3539, 25) == Decimal("2654.25")         # 3539 × 0,75
    assert d.markup_rub == Decimal("1330.69")             # 2654,25 − 1323,56
    assert d.markup_coef == Decimal("2.01")               # 2654,25 / 1323,56
    assert d.block_reason is None


def test_markup_definition_is_payout_minus_cost_and_ratio():
    rub, coef = markup(1000, Decimal("600"), 20)
    assert rub == Decimal("200.00") and coef == Decimal("1.33")


def test_coefficients_mean_customer_percentages():
    """100% — 2, 150% — 2,5, 200% — 3 (без комиссии и округления)."""
    for coef, pct in ((2, 100), (Decimal("2.5"), 150), (3, 200)):
        d = decide(Decimal("10"), Decimal("1"), 0, _rule(markup_coef=coef, round_step=1, round_minus=0,
                                                       min_markup_coef=1), None, None)
        assert d.new_price == 10 * coef and d.markup_rub == Decimal(10 * pct / 100).quantize(Decimal("0.01"))
        assert d.markup_coef == Decimal(str(coef)).quantize(Decimal("0.01"))


def test_rounding_up_never_eats_markup():
    assert round_price(Decimal("1201"), 100, 1) == 1299
    assert round_price(Decimal("1300"), 100, 1) == 1399
    assert round_price(Decimal("1000.01"), 1, 0) == 1001


def test_floor_blocks_manual_price():
    d = decide(Decimal("16.24"), RATE, 25, _rule(), 1500, None)
    assert d.source == "manual" and d.block_reason == BLOCK_FLOOR
    assert d.markup_rub < 0 and "2295" in d.note          # 1323,56 × 1,3 / 0,75 = 2294,17


def test_no_commission_no_price():
    d = decide(Decimal("10"), RATE, None, _rule(), None, None)
    assert d.new_price is None and "комиссия" in d.note


def test_no_rate_no_price():
    d = decide(Decimal("10"), None, 25, _rule(), None, None)
    assert d.new_price is None and "курса" in d.note


def test_no_cost_no_price_but_manual_allowed_with_warning():
    assert decide(None, RATE, 25, _rule(), None, None).new_price is None
    d = decide(None, RATE, 25, _rule(), 990, None)
    assert d.new_price == 990 and d.block_reason is None and "пол не проверен" in d.note


def test_unconfigured_rule():
    d = decide(Decimal("10"), RATE, 25, _rule(markup_coef=1), None, None)
    assert d.new_price is None and "не настроено" in d.note


def test_large_change_blocked_first_price_not():
    assert decide(Decimal("16.24"), RATE, 25, _rule(), None, 2000).block_reason == BLOCK_MAX_CHANGE
    assert decide(Decimal("16.24"), RATE, 25, _rule(max_change_percent=1), None, None).block_reason is None


def test_commission_changes_price():
    wb = decide(Decimal("16.24"), RATE, 25, _rule(), None, None).new_price
    oz = decide(Decimal("16.24"), RATE, 30, _rule(), None, None).new_price
    assert oz > wb


def test_recalculate_creates_proposals_with_economics(db):
    acc = f.account(db, commission=25)
    f.rule(db, acc)
    f.manual_rate(db, "81.5")
    f.sku(db, "u1", "39681", "L", barcodes=["b1"], cost_usd="16.24")
    f.sku(db, "u2", "39682", "M", barcodes=["b2"])              # без себестоимости
    f.item(db, acc, "b1", "39681-L")
    f.item(db, acc, "b2", "39682-M")
    f.item(db, acc, "b9", "чужой")                              # нет в 1С

    st = recalculate_account(db, acc)

    assert (st.proposed, st.skipped) == (1, 1)
    ch = db.query(PriceChange).one()
    assert (ch.item_id, ch.new_price, ch.barcode) == ("u1", 3539, "b1")
    assert ch.cost_usd == Decimal("16.24") and ch.usd_rub == Decimal("81.5")
    assert ch.cost_rub == Decimal("1323.56") and ch.markup_rub == Decimal("1330.69")
    assert db.query(ProductPrice).count() == 0                 # ничего не «отправлено»


def test_recalculate_supersedes_open_keeps_approved(db):
    acc = f.account(db)
    f.rule(db, acc)
    f.manual_rate(db)
    f.sku(db, "u1", "A", barcodes=["b1"], cost_usd="10")
    f.item(db, acc, "b1", "A")
    recalculate_account(db, acc)
    db.query(PriceChange).one().status = "approved"
    db.commit()
    recalculate_account(db, acc)
    recalculate_account(db, acc)
    assert sorted(c.status for c in db.query(PriceChange)) == ["approved", "proposed", "rejected"]


def test_floor_never_approved_large_change_only_with_flag():
    c = PriceChange(new_price=1, status="blocked", block_reason=BLOCK_FLOOR)
    assert approve(c, "op", confirm_large=True) is not None and c.status == "blocked"
    c = PriceChange(new_price=1, status="blocked", block_reason=BLOCK_MAX_CHANGE)
    assert approve(c, "op") is not None
    assert approve(c, "op", confirm_large=True) is None and c.status == "approved"

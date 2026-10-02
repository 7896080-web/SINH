"""Расчёт цены: экономика заказчика и ограничители.

базовая ₽ = себестоимость $ × курс (руками не правится); цена = базовая ×
коэффициент; к получению = цена × (1 − комиссия); маржинальность = к получению /
себестоимость ₽.
"""
from decimal import Decimal

from priceapp.models import PlatformRule, PriceChange, ProductPrice
from priceapp.pricing import (BLOCK_FLOOR, BLOCK_MAX_CHANGE, approve, base_price, coef_for, decide, load_inputs,
                              markup, payout, recalculate_account, round_price)
from tests import factories as f

RATE = Decimal("81.5")


def _rule(commission=25, **kw):
    base = dict(round_step=10, round_minus=1, min_markup_coef=Decimal("1.3"), max_change_percent=20,
                commission_percent=commission)
    base.update(kw)
    return PlatformRule(**base)


def test_customer_example_sweatshirt_16_24_usd():
    """Свитшот 39681 GRI MELANG: «Цена СС» 16,24 $, WB комиссия 25%, коэффициент 2,5."""
    assert base_price(Decimal("16.24"), RATE) == Decimal("1323.56")      # 16,24 × 81,5
    d = decide(Decimal("16.24"), RATE, _rule(), Decimal("2.5"), "article", None, None)
    assert d.cost_rub == Decimal("1323.56")
    assert d.new_price == 3309                            # 1323,56 × 2,5 = 3308,9 → вверх до 10, −1
    assert payout(3309, 25) == Decimal("2481.75")         # 3309 × 0,75
    assert d.markup_rub == Decimal("1158.19")             # 2481,75 − 1323,56
    assert d.markup_coef == Decimal("1.88")               # маржинальность 2481,75 / 1323,56
    assert d.coef == Decimal("2.5") and d.source == "article" and d.block_reason is None


def test_markup_definition_is_payout_minus_cost_and_ratio():
    rub, coef = markup(1000, Decimal("600"), 20)
    assert rub == Decimal("200.00") and coef == Decimal("1.33")


def test_price_is_base_times_coef_without_commission_in_it():
    """Без комиссии и округления цена = базовая × коэффициент, маржинальность — тот же коэффициент."""
    for coef in (2, Decimal("2.5"), 3):
        d = decide(Decimal("10"), Decimal("1"), _rule(0, round_step=1, round_minus=0, min_markup_coef=1),
                   coef, "article", None, None)
        assert d.new_price == 10 * coef and d.markup_coef == Decimal(str(coef)).quantize(Decimal("0.01"))


def test_rounding_up_never_eats_markup():
    assert round_price(Decimal("1201"), 100, 1) == 1299
    assert round_price(Decimal("1300"), 100, 1) == 1399
    assert round_price(Decimal("1000.01"), 1, 0) == 1001


def test_floor_blocks_low_coef_and_manual_price():
    d = decide(Decimal("16.24"), RATE, _rule(), Decimal("1.5"), "article", None, None)
    assert d.block_reason == BLOCK_FLOOR and "2295" in d.note       # 1323,56 × 1,3 / 0,75 = 2294,17
    d = decide(Decimal("16.24"), RATE, _rule(), Decimal("2.5"), "article", 1500, None)
    assert d.source == "manual" and d.block_reason == BLOCK_FLOOR and d.markup_rub < 0


def test_no_commission_no_price():
    d = decide(Decimal("10"), RATE, _rule(None), 2, "article", None, None)
    assert d.new_price is None and "комиссия" in d.note


def test_no_rate_no_price():
    d = decide(Decimal("10"), None, _rule(), 2, "article", None, None)
    assert d.new_price is None and "курса" in d.note


def test_no_cost_no_price_but_manual_allowed_with_warning():
    assert decide(None, RATE, _rule(), 2, "article", None, None).new_price is None
    d = decide(None, RATE, _rule(), 2, "article", 990, None)
    assert d.new_price == 990 and d.block_reason is None and "пол не проверен" in d.note


def test_no_coef_no_price():
    d = decide(Decimal("10"), RATE, _rule(), None, "rule", None, None)
    assert d.new_price is None and "коэффициент не задан" in d.note


def test_large_change_blocked_first_price_not():
    assert decide(Decimal("16.24"), RATE, _rule(), Decimal("2.5"), "article", None, 2000).block_reason == \
        BLOCK_MAX_CHANGE
    assert decide(Decimal("16.24"), RATE, _rule(max_change_percent=1), Decimal("2.5"), "article",
                  None, None).block_reason is None


def test_commission_changes_margin_not_price():
    wb = decide(Decimal("16.24"), RATE, _rule(25), Decimal("2.5"), "article", None, None)
    oz = decide(Decimal("16.24"), RATE, _rule(30), Decimal("2.5"), "article", None, None)
    assert oz.new_price == wb.new_price and oz.markup_coef < wb.markup_coef


def test_coef_cabinet_beats_platform_beats_default(db):
    a1 = f.account(db, "wb", "ИП Яворская")
    a2 = f.account(db, "wb", "ИП Ребрик")
    f.rule(db, a1, base_coef="2.2")
    f.sku(db, "u1", "39681", "L", barcodes=["b1"])
    f.sku(db, "u2", "39681", "M", barcodes=["b2"])
    f.sku(db, "u3", "4033", "XL", barcodes=["b3"])
    f.coef(db, "39681", "wb", "2.5")
    f.coef(db, "39681", "wb", "2.7", account_id=a2.id)
    inp = load_inputs(db, "wb")
    assert coef_for(inp, "u1", a1.id) == (Decimal("2.5"), "article")
    assert coef_for(inp, "u2", a2.id) == (Decimal("2.7"), "cabinet")     # все размеры артикула
    assert coef_for(inp, "u3", a2.id) == (Decimal("2.2"), "rule")
    assert coef_for(inp, "u2", None) == (Decimal("2.5"), "article")       # площадка целиком


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
    assert (ch.item_id, ch.new_price, ch.barcode) == ("u1", 3309, "b1")
    assert ch.cost_usd == Decimal("16.24") and ch.usd_rub == Decimal("81.5")
    assert ch.cost_rub == Decimal("1323.56") and ch.markup_rub == Decimal("1158.19")
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

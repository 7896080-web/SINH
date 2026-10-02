"""Расчёт цены: экономика заказчика и ограничители.

базовая ₽ = себестоимость $ × курс (руками не правится); цена = базовая ×
коэффициент; к получению = цена × (1 − комиссия); маржинальность = к получению /
себестоимость ₽.
"""
from decimal import Decimal

from priceapp.models import PlatformRule, PriceChange, ProductPrice
from priceapp.pricing import (BLOCK_FLOOR, BLOCK_MAX_CHANGE, Facts, Target, approve, base_price, coef_for, decide,
                              item_facts, load_inputs, markup, payout, recalculate_account, round_price)
from tests import factories as f

RATE = Decimal("81.5")


def T(value, kind="coef", src="article"):
    return Target(kind, Decimal(str(value)), src)


def _rule(commission=25, **kw):
    base = dict(round_step=10, round_minus=1, min_markup_coef=Decimal("1.3"), max_change_percent=20,
                commission_percent=commission, commission_extra=0, tariff_model="fbs")
    base.update(kw)
    return PlatformRule(**base)


def test_customer_example_sweatshirt_16_24_usd():
    """Свитшот 39681 GRI MELANG: «Цена СС» 16,24 $, WB комиссия 25%, коэффициент 2,5."""
    assert base_price(Decimal("16.24"), RATE) == Decimal("1323.56")      # 16,24 × 81,5
    d = decide(Decimal("16.24"), RATE, _rule(), T("2.5"), None, None)
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
                   T(coef), None, None)
        assert d.new_price == 10 * coef and d.markup_coef == Decimal(str(coef)).quantize(Decimal("0.01"))


def test_rounding_up_never_eats_markup():
    assert round_price(Decimal("1201"), 100, 1) == 1299
    assert round_price(Decimal("1300"), 100, 1) == 1399
    assert round_price(Decimal("1000.01"), 1, 0) == 1001


def test_floor_blocks_low_coef_and_manual_price():
    d = decide(Decimal("16.24"), RATE, _rule(), T("1.5"), None, None)
    assert d.block_reason == BLOCK_FLOOR and "2295" in d.note       # 1323,56 × 1,3 / 0,75 = 2294,17
    d = decide(Decimal("16.24"), RATE, _rule(), T("2.5"), 1500, None)
    assert d.source == "manual" and d.block_reason == BLOCK_FLOOR and d.markup_rub < 0


def test_no_commission_no_price():
    d = decide(Decimal("10"), RATE, _rule(None), T(2), None, None)
    assert d.new_price is None and "комиссия" in d.note


def test_no_rate_no_price():
    d = decide(Decimal("10"), None, _rule(), T(2), None, None)
    assert d.new_price is None and "курса" in d.note


def test_no_cost_no_price_but_manual_allowed_with_warning():
    assert decide(None, RATE, _rule(), T(2), None, None).new_price is None
    d = decide(None, RATE, _rule(), T(2), 990, None)
    assert d.new_price == 990 and d.block_reason is None and "пол не проверен" in d.note


def test_no_coef_no_price():
    d = decide(Decimal("10"), RATE, _rule(), None, None, None)
    assert d.new_price is None and "наценка не задана" in d.note


def test_large_change_blocked_first_price_not():
    assert decide(Decimal("16.24"), RATE, _rule(), T("2.5"), None, 2000).block_reason == \
        BLOCK_MAX_CHANGE
    assert decide(Decimal("16.24"), RATE, _rule(max_change_percent=1), T("2.5"),
                  None, None).block_reason is None


def test_commission_changes_margin_not_price():
    wb = decide(Decimal("16.24"), RATE, _rule(25), T("2.5"), None, None)
    oz = decide(Decimal("16.24"), RATE, _rule(30), T("2.5"), None, None)
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


# --- маржинальность, тарифы, скидка продавца ------------------------------------------

class _Row:
    """Строка каталога кабинета — только то, что читает `item_facts`."""
    def __init__(self, fbs=None, fbo=None, price=None, sale=None, category=""):
        self.tariff_fbs, self.tariff_fbo = (Decimal(str(fbs)) if fbs is not None else None,
                                            Decimal(str(fbo)) if fbo is not None else None)
        self.current_price, self.current_sale_price, self.category = price, sale, category


def test_customer_formula_price_minus_seller_discount_minus_commission():
    """Пример заказчика: цена 2649, скидка продавца 30%, комиссия 25%, себестоимость 1323,56."""
    rule = _rule(25, round_step=1, round_minus=0)
    facts = item_facts(rule, [_Row(price=2649, sale=1854)])          # 1854 / 2649 = скидка ≈ 30%
    d = decide(Decimal("16.24"), RATE, rule, T("2.0015"), None, None, facts)
    assert d.new_price == 2650 and d.discount > Decimal("0.29")
    assert d.markup_coef == Decimal("1.05")      # 2650 × (1854/2649) × 0,75 / 1323,56
    assert d.block_reason == BLOCK_FLOOR         # без скидки было бы 1,50 — выше пола 1,3


def test_margin_target_follows_tariff_and_discount():
    """Маржинальность 1,8: при тарифе 15% + надбавка 3 п.п. и скидке 20% цена такая, что
    к получению / себестоимость = 1,8; площадка подняла тариф — цена поднялась, маржинальность та же."""
    rule = _rule(25, round_step=1, round_minus=0, commission_extra=Decimal("3"))
    f1 = item_facts(rule, [_Row(fbs=15, price=4000, sale=3200)])
    assert f1.commission == Decimal("18") and f1.tariff == Decimal("15") and f1.discount == Decimal("0.2")
    d1 = decide(Decimal("16.24"), RATE, rule, T("1.8", "margin"), None, None, f1)
    # 1323,56 × 1,8 / (0,82 × 0,8) = 3631,72 → 3632
    assert d1.new_price == 3632 and d1.markup_coef == Decimal("1.80")
    f2 = item_facts(rule, [_Row(fbs=20, price=4000, sale=3200)])
    d2 = decide(Decimal("16.24"), RATE, rule, T("1.8", "margin"), None, None, f2)
    assert d2.new_price > d1.new_price and d2.markup_coef == Decimal("1.80")


def test_unknown_tariff_falls_back_to_rule_plus_extra_and_fbo_switch():
    rule = _rule(25, commission_extra=Decimal("2"))
    assert item_facts(rule, [_Row()]).commission == Decimal("27")
    rule.tariff_model = "fbo"
    assert item_facts(rule, [_Row(fbs=15, fbo=17)]).commission == Decimal("19")


def test_category_target_sits_between_article_and_default(db):
    a1 = f.account(db, "wb", "ИП Яворская")
    a2 = f.account(db, "wb", "ИП Ребрик")
    f.rule(db, a1, base_coef="2.2")
    f.sku(db, "u1", "39681", "L", barcodes=["b1"])
    f.sku(db, "u3", "4033", "XL", barcodes=["b3"])
    from priceapp.models import CategoryTarget
    db.add(CategoryTarget(platform="wb", account_id=0, category="Свитшоты", kind="margin", value=Decimal("1.7")))
    db.add(CategoryTarget(platform="wb", account_id=a2.id, category="Свитшоты", kind="margin", value=Decimal("1.9")))
    db.commit()
    f.coef(db, "4033", "wb", "2.5")
    from priceapp.pricing import target_for
    inp = load_inputs(db, "wb")
    t = target_for(inp, "u1", a1.id, "Свитшоты")
    assert (t.kind, t.value, t.source) == ("margin", Decimal("1.7"), "category")
    assert target_for(inp, "u1", a2.id, "Свитшоты").value == Decimal("1.9")          # кабинет главнее
    assert target_for(inp, "u3", a2.id, "Свитшоты").source == "article"              # артикул главнее категории
    assert target_for(inp, "u1", a1.id, "Джемперы").source == "rule"

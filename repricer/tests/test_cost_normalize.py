"""Себестоимость из 1С выравнивается по «артикул + цвет»: самое частое значение, при равенстве — медиана."""
from decimal import Decimal

from priceapp import onec, settings
from priceapp.models import OnecCost
from tests import factories as f


def _sku(db, item, article, color, size, cost=None):
    f.sku(db, item, article, size, color=color, barcodes=[f"b-{item}"], cost_usd=cost)


def _costs(db):
    return {c.item_id: float(c.cost_usd) for c in db.query(OnecCost)}


def test_outlier_size_gets_the_most_frequent_cost(db):
    for i, (size, cost) in enumerate([("3XL", "15.01"), ("4XL", "28.88"), ("5XL", "15.01")]):
        _sku(db, f"u{i}", "32481", "BEYAZ", size)
    st = onec.load_costs(db, {"u0": Decimal("15.01"), "u1": Decimal("28.88"), "u2": Decimal("15.01")})
    db.commit()
    assert _costs(db) == {"u0": 15.01, "u1": 15.01, "u2": 15.01} and st["normalized"] == 1


def test_colors_are_separate_groups_and_ties_use_median(db):
    _sku(db, "a1", "T1", "RED", "L"); _sku(db, "a2", "T1", "RED", "XL")
    _sku(db, "b1", "T1", "BLUE", "L"); _sku(db, "b2", "T1", "BLUE", "XL"); _sku(db, "b3", "T1", "BLUE", "XXL")
    onec.load_costs(db, {"a1": Decimal("8"), "a2": Decimal("10"),                 # поровну → медиана 9
                         "b1": Decimal("20"), "b2": Decimal("20"), "b3": Decimal("30")})
    db.commit()
    assert _costs(db) == {"a1": 9.0, "a2": 9.0, "b1": 20.0, "b2": 20.0, "b3": 20.0}


def test_sku_missing_from_file_is_aligned_and_switch_turns_it_off(db):
    _sku(db, "u0", "A", "X", "S"); _sku(db, "u1", "A", "X", "M"); _sku(db, "u2", "A", "X", "L")
    onec.load_costs(db, {"u0": Decimal("5"), "u1": Decimal("5"), "u2": Decimal("5")}); db.commit()
    onec.load_costs(db, {"u2": Decimal("9")}); db.commit()          # в файле только один размер: большинство прежнее
    assert _costs(db) == {"u0": 5.0, "u1": 5.0, "u2": 5.0}
    settings.put(db, settings.COST_NORMALIZE, "0"); db.commit()
    onec.load_costs(db, {"u2": Decimal("9")}); db.commit()
    assert _costs(db)["u2"] == 9.0


def test_renormalize_stored_and_untouched_without_article(db):
    _sku(db, "u0", "", "", "S"); _sku(db, "u1", "", "", "M")
    _sku(db, "v0", "B", "X", "S"); _sku(db, "v1", "B", "X", "M"); _sku(db, "v2", "B", "X", "L")
    for i, c in (("u0", 1), ("u1", 2), ("v0", 7), ("v1", 7), ("v2", 12)):
        db.add(OnecCost(item_id=i, cost_usd=Decimal(c)))
    db.commit()
    assert onec.renormalize_stored(db) == 1
    db.commit()
    assert _costs(db) == {"u0": 1.0, "u1": 2.0, "v0": 7.0, "v1": 7.0, "v2": 7.0}

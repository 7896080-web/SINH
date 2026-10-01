"""Сопоставление по правилам sync_admin и правила артикулов."""
from priceapp import article_matching as am, mapping
from priceapp.models import ManualLink
from tests import factories as f


def test_barcode_statuses(db):
    acc = f.account(db)
    f.sku(db, "u1", "A", barcodes=["b1"])
    f.sku(db, "u2", "B", barcodes=["shared"])
    f.sku(db, "u3", "C", barcodes=["shared"])
    f.item(db, acc, "b1", external_id="nm:1")
    f.item(db, acc, "b1alt", external_id="nm:1")          # тот же размер — по пулу
    f.item(db, acc, "shared")
    f.item(db, acc, "none")
    st = {r.item.barcode: (r.status, r.item_id) for r in mapping.build(db, acc.id)}
    assert st["b1"] == ("ok", "u1") and st["b1alt"] == ("pool", "u1")
    assert st["shared"][0] == "ambiguous" and st["none"] == ("not_in_1c", "")
    assert set(mapping.account_items(db, acc.id)) == {"u1"}


def test_pool_not_merged_when_neighbours_disagree(db):
    acc = f.account(db)
    f.sku(db, "u1", "A", barcodes=["b1"])
    f.sku(db, "u2", "B", barcodes=["b2"])
    for b in ("b1", "b2", "b3"):
        f.item(db, acc, b, external_id="nm:1")
    st = {r.item.barcode: r.status for r in mapping.build(db, acc.id)}
    assert st["b3"] == "not_in_1c"


def test_manual_link_never_overrides_dictionary(db):
    acc = f.account(db)
    f.sku(db, "u1", "A", barcodes=["b1"])
    f.item(db, acc, "b1")
    db.add(ManualLink(account_id=acc.id, barcode="b1", item_id="u9"))
    db.commit()
    assert mapping.build(db, acc.id)[0].item_id == "u1"


def test_article_rules_learned_and_candidates(db):
    acc = f.account(db, "ozon")
    f.sku(db, "u1", "JN-100", "48", barcodes=["b1"])
    f.sku(db, "u2", "JN-100", "50")
    db.add_all([])
    from priceapp.models import OnecBarcode
    db.add(OnecBarcode(barcode="dummy-u2", item_id="u2", article="JN-100", size="50"))
    db.commit()
    f.item(db, acc, "b1", "jn100_48")
    f.item(db, acc, "n2", "JN100-50")
    an = am.analyze(db, acc.id)
    assert an.counts["size"] == 1 and "size" in an.suggested_kinds
    am.get_rule(db, acc.id).kinds = "size"
    [c] = am.candidates(db, acc.id)
    assert c.status == "unique" and c.products[0].uid_1c == "u2"
    created, refused = am.confirm(db, acc.id, [("n2", "u2"), ("n2", "u2")], "op")
    db.commit()
    assert created == 1 and sum(refused.values()) == 1
    assert {r.item.barcode: r.status for r in mapping.build(db, acc.id)}["n2"] == "manual"


def test_wb_size_disambiguates(db):
    acc = f.account(db)
    from priceapp.models import OnecBarcode
    db.add_all([OnecBarcode(barcode="x1", item_id="u1", article="JN", size="48"),
                OnecBarcode(barcode="x2", item_id="u2", article="JN", size="50")])
    db.commit()
    f.item(db, acc, "n1", "jn", size="50")
    f.item(db, acc, "n2", "jn", size="52")
    am.get_rule(db, acc.id).kinds = "exact"
    by = {c.item.barcode: c for c in am.candidates(db, acc.id)}
    assert by["n1"].status == "unique" and by["n1"].products[0].uid_1c == "u2"
    assert by["n2"].status == "size_mismatch"


def test_normalize():
    assert am.normalize("jn-100 / 48") == am.normalize("JN100_48")
    assert am.normalize("А100") == am.normalize("A100")

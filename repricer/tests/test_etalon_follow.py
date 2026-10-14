"""Все площадки следуют за эталоном WB (priceapp/etalon.py, страница «Эталонные цены»)."""
from decimal import Decimal

from priceapp import etalon, settings
from priceapp.models import PlatformItem, PriceChange, ReferencePrice
from tests import factories as F


def _setup(db, cost_usd=None):
    w = F.account(db, "wb", "ИП 1", commission=25)
    kit = F.account(db, "kit", "КИТ", commission=10)
    lam = F.account(db, "lamoda", "Ламода", commission=40)
    F.manual_rate(db, "100")
    F.sku(db, "s1", "100", "M", barcodes=["b1"], cost_usd=cost_usd)
    F.item(db, w, "b1", "100", external_id="111:1")
    F.item(db, kit, "b1", "100", external_id="v1")
    F.item(db, lam, "b1", "100", external_id="MP1:s1")
    cur = {w.id: (10000, 5000), kit.id: (6000, 4000), lam.id: (9000, 4500)}
    for it in db.query(PlatformItem):
        it.current_price, it.current_sale_price = cur[it.account_id]
    db.add(ReferencePrice(account_id=w.id, item_id="s1", ref_price=10000, discount=Decimal("0.5")))
    db.commit()
    return w, kit, lam


def test_targets_follow_etalon_with_integer_discount_in_corridor(db):
    w, kit, lam = _setup(db)
    ets = etalon.etalons(db)
    assert ets["100"].sale == 5000
    plan = {c.platform: c for c in etalon.follower_plan(db, ets)}
    assert set(plan) == {"kit", "lamoda"}                     # Ozon: сдвиг пуст — не следует
    k = plan["kit"]        # 5000 − 45% = 2750; скидка 33% вне коридора -> 35%; 2750 / 0,65 = 4230,8 -> 4231
    assert (k.target, k.new_list, k.disc) == (2750, 4231, 35)
    m = plan["lamoda"]     # 5000 − 31% = 3450; скидка 50% в коридоре — остаётся; 3450 / 0,5 = 6900
    assert (m.target, m.new_list, m.disc) == (3450, 6900, 50)


class Fake:
    def __init__(self, log):
        self.log = log

    def _post(self, url, body):
        self.log.append(("post", url, body))
        return {"successCount": 1, "errorCount": 0, "errors": [], "fraudValidationResults": []}

    def push_prices(self, items):
        self.log.append(("push", items))
        return {"ok": [i.barcode for i in items], "errors": [], "sent_prices": {}}


def test_set_etalon_records_and_sends_everywhere(db):
    w, kit, lam = _setup(db)
    log = []
    rep = etalon.set_etalon(db, "100", 4000, "оператор", client_factory=lambda p, c: Fake(log))
    db.expire_all()
    ref = db.query(ReferencePrice).filter_by(account_id=w.id, item_id="s1").one()
    assert (ref.ref_price, ref.discount, ref.source) == (8000, Decimal("0.5"), "manual")
    wb_body = next(x[2] for x in log if x[0] == "post" and "upload/task" in x[1])
    assert wb_body["data"] == [{"nmID": 111, "price": 8000, "discount": 50}]
    kit_items = next(x[1] for x in log if x[0] == "push")
    assert (kit_items[0].price, kit_items[0].sale) == (3385, 2200)   # 4000 − 45% = 2200, скидка 35%
    lam_body = next(x[2] for x in log if x[0] == "post" and "nomenclatures-prices" in x[1])
    p = lam_body["prices"][0]
    assert (p["price"]["amount"], p["salePrice"]["amount"]) == (552000, 276000) and p["saleEnd"] > p["saleStart"]
    assert any("WB ИП 1 111" in r for r in rep)
    kit_row = db.query(PlatformItem).filter_by(account_id=kit.id).one()
    assert (kit_row.current_price, kit_row.current_sale_price) == (3385, 2200)


def test_big_drop_goes_in_steps_through_the_queue(db):
    w, kit, lam = _setup(db)
    log = []
    etalon.set_etalon(db, "100", 2000, "оператор", client_factory=lambda p, c: Fake(log))
    wb_body = next(x[2] for x in log if x[0] == "post" and "upload/task" in x[1])
    # 5000 −33% = 3350 со скидкой 50% -> 6700 сейчас; цель 2000 -> 4000 — очередью, шагами
    assert wb_body["data"][0]["price"] == 6700
    ch = db.query(PriceChange).filter_by(account_id=w.id).one()
    assert (ch.status, ch.new_price, ch.source) == ("approved", 4000, "etalon")
    ref = db.query(ReferencePrice).filter_by(account_id=w.id).one()
    assert ref.ref_price == 4000                              # эталон — сразу финальный


def test_below_floor_is_refused_and_nothing_changes(db):
    w, kit, lam = _setup(db, cost_usd=100)                     # себестоимость 10 000 ₽
    log = []
    rep = etalon.set_etalon(db, "100", 1000, "оператор", client_factory=lambda p, c: Fake(log))
    assert "не записан" in rep[0] and "ниже пола" in rep[0] and not log
    assert db.query(ReferencePrice).filter_by(account_id=w.id).one().ref_price == 10000


def test_auto_follow_only_when_enabled_and_small_steps(db):
    w, kit, lam = _setup(db)
    log = []
    st = etalon.follow_all(db, client_factory=lambda p, c: Fake(log))
    assert st["enabled"] is False and not log
    settings.put(db, settings.ETALON_FOLLOW_AUTO, "1")
    db.commit()
    st = etalon.follow_all(db, client_factory=lambda p, c: Fake(log))
    # Kit: 4000 -> 2750 (−31%) — в пределах 50%, уходит; Ламода 4500 -> 3450 (−23%) — тоже
    assert st["big"] == 0 and set(st["sent"]) == {"КИТ", "Ламода"}
    log.clear()
    st = etalon.follow_all(db, client_factory=lambda p, c: Fake(log))
    assert not st["sent"] and not log                          # всё уже держит эталон


def test_page_preview_and_settings(client, db):
    _setup(db)
    page = client.get("/etalon").text
    assert "Эталонные цены" in page and ">100<" in page.replace("<b>", ">").replace("</b>", "<")
    prev = client.get("/etalon?art=100&new=4000").text
    assert "Применить на все площадки" in prev and "8 000" in prev and "2 200" in prev
    r = client.post("/etalon/settings", data={"shift_kit": "40", "shift_lamoda": "31", "shift_ozon": "",
                                               "lo": "35", "hi": "50", "auto": "1"})
    assert "Сохранено" in r.text
    assert settings.get(db, settings.ETALON_SHIFT_KIT) == "40" and etalon.auto_enabled(db)
    nav = page.split("<nav>", 1)[1].split("</nav>", 1)[0]
    assert "/etalon" in nav


def test_excel_import_sets_new_etalons(client, db):
    import io
    from openpyxl import Workbook
    from priceapp.routers import etalon as page
    _setup(db)
    wb = Workbook()
    ws = wb.active
    ws.append(page.HEAD)
    ws.append(["100", "x", 5000, 4000])
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/etalon/import", files={"file": ("e.xlsx", buf.getvalue())})
    assert "новый эталон у 1 артикулов" in r.text
    db.expire_all()
    assert db.query(ReferencePrice).one().ref_price == 8000

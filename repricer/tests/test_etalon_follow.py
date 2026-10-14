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


def test_excel_import_sets_new_etalons(client, db, monkeypatch):
    import io
    from openpyxl import Workbook
    from priceapp import accounts
    from priceapp.routers import etalon as page
    _setup(db)
    monkeypatch.setattr(accounts, "client_for", lambda db, a, factory=None: Fake([]))
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


def test_difference_within_tolerance_is_not_a_divergence(client, db):
    w, kit, lam = _setup(db)
    row = db.query(PlatformItem).filter_by(account_id=lam.id).one()
    row.current_price, row.current_sale_price = 6900, 3451            # цель 3450 — разница 1 ₽
    db.commit()
    plan = {c.platform: c for c in etalon.follower_plan(db)}
    assert plan["lamoda"].follows and not plan["lamoda"].same
    settings.put(db, settings.ETALON_TOLERANCE_RUB, "0")
    db.commit()
    assert not {c.platform: c for c in etalon.follower_plan(db)}["lamoda"].follows
    settings.put(db, settings.ETALON_TOLERANCE_RUB, "100")
    settings.put(db, settings.ETALON_FOLLOW_AUTO, "1")
    db.commit()
    log = []
    st = etalon.follow_all(db, client_factory=lambda p, c: Fake(log))
    assert set(st["sent"]) == {"КИТ"}                                   # Ламода в допуске — не трогаем


def test_scheduled_wb_poll_at_0005_once_per_night(db):
    from datetime import datetime
    from priceapp.workers import jobs
    assert jobs.parse_poll_at("00:05, 12:30; x") == [(0, 5), (12, 30)]
    assert jobs.scheduled_wb_poll_due(datetime(2026, 10, 15, 0, 3)) is False      # ещё рано
    assert jobs.scheduled_wb_poll_due(datetime(2026, 10, 15, 0, 6)) is True       # наступило
    assert jobs.scheduled_wb_poll_due(datetime(2026, 10, 15, 0, 7)) is False      # уже было
    assert jobs.scheduled_wb_poll_due(datetime(2026, 10, 15, 15, 0)) is False     # опоздали больше 3 ч
    assert jobs.scheduled_wb_poll_due(datetime(2026, 10, 16, 0, 5)) is True       # следующая ночь


def test_poll_now_button_requests_wb_poll(client, db):
    _setup(db)
    r = client.post("/etalon/poll-now")
    assert "Опрос WB начнётся" in r.text
    db.expire_all()
    assert settings.get(db, settings.REF_POLL_NOW) == "1"
    assert "в 00:05" in client.get("/etalon").text


class WbDown(Fake):
    def _post(self, url, body):
        if "upload/task" in url:
            raise RuntimeError("429 Too Many Requests")
        return super()._post(url, body)


def test_wb_refusal_changes_nothing_and_followers_wait(db):
    """Аудит 14.10: WB не принял снижение — раньше эталон записывался, а Kit и Ламода уходили
    вниз; вернуть WB было нечем (возврат эталона — только вверх)."""
    w, kit, lam = _setup(db)
    log = []
    rep = etalon.set_etalon(db, "100", 4000, "оператор", client_factory=lambda p, c: WbDown(log))
    db.expire_all()
    assert db.query(ReferencePrice).filter_by(account_id=w.id).one().ref_price == 10000
    assert not [x for x in log if x[0] == "push" or "nomenclatures" in str(x)]
    assert any("НЕ записан" in r for r in rep) and any("НЕ отправлены" in r for r in rep)
    assert (db.query(PlatformItem).filter_by(account_id=kit.id).one().current_sale_price) == 4000


def test_no_rate_or_bad_commission_means_floor_unknown_and_nothing_goes(db):
    from priceapp import settings as st
    w, kit, lam = _setup(db, cost_usd=10)
    st.put(db, st.RATE_MANUAL, "")
    db.commit()
    plan = etalon.follower_plan(db)
    assert all("нет курса" in c.skip for c in plan)
    F.manual_rate(db, "100")
    from priceapp.pricing import get_rule
    get_rule(db, "kit").commission_percent = Decimal(100)
    db.commit()
    plan = {c.platform: c for c in etalon.follower_plan(db)}         # раньше — DivisionByZero
    assert "комиссия" in plan["kit"].skip and not plan["lamoda"].skip


def test_auto_follow_waits_when_current_price_is_unknown(db):
    w, kit, lam = _setup(db)
    row = db.query(PlatformItem).filter_by(account_id=kit.id).one()
    row.current_price = row.current_sale_price = None
    settings.put(db, settings.ETALON_FOLLOW_AUTO, "1")
    db.commit()
    log = []
    st = etalon.follow_all(db, client_factory=lambda p, c: Fake(log))
    assert "КИТ" not in st["sent"] and st["big"] == 1


def test_new_etalon_cancels_queued_follower_price(db):
    w, kit, lam = _setup(db)
    db.add(PriceChange(item_id="s1", account_id=kit.id, barcode="b1", new_price=5200, status="approved",
                       source="follow"))
    db.commit()
    etalon.set_etalon(db, "100", 4000, "оператор", client_factory=lambda p, c: Fake([]))
    ch = db.query(PriceChange).filter_by(account_id=kit.id).one()
    assert ch.status == "rejected" and "новый эталон" in ch.note


def test_new_etalon_is_recorded_as_our_last_sent_on_wb(db):
    from priceapp.models import ProductPrice
    w, kit, lam = _setup(db)
    etalon.set_etalon(db, "100", 4000, "оператор", client_factory=lambda p, c: Fake([]))
    pp = db.query(ProductPrice).filter_by(account_id=w.id, item_id="s1").one()
    assert pp.last_sent_price == 8000

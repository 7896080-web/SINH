"""Акции WB: календарь в базу, наши карточки в обычной акции — цена при входе, «% от эталона для
вхождения», порог, пометка; порог «для акции» до её конца; «Добавить в акцию»; строка во «Внимании»;
Excel. Эталон 2009 ₽ без скидки, пол 1387 ₽ (себестоимость 800 ₽ × 1,3 / 0,75)."""
import io
from datetime import timedelta
from decimal import Decimal

from openpyxl import Workbook, load_workbook

from priceapp import accounts as acc_mod, reference, wb_discount as wd, wb_promo as wp
from priceapp.models import AuditLog, PlatformItem, WbDiscountRule, WbPromotion, WbPromotionItem
from priceapp.timeutils import now_utc
from tests import factories as f

FMT = "%Y-%m-%dT%H:%M:%SZ"


def _setup(db, name="ИП 1", nm="5", art="A1", item="u1", barcode="b1", acc=None, category="Шорты"):
    from priceapp.models import OnecCost
    if acc is None:
        f.manual_rate(db, "80")
        acc = f.account(db, "wb", name, commission=25)
        f.rule(db, acc, base_coef="2.5", min_markup_coef="1.3", max_change_percent=1000)
        acc_mod.set_credential(db, acc, "token", "t")
    f.sku(db, item, art, "S", barcodes=[barcode], cost_usd=None if db.get(OnecCost, item) else "10")
    f.item(db, acc, barcode, art, external_id=f"{nm}:1", size="S")
    row = db.query(PlatformItem).filter_by(barcode=barcode).one()
    row.current_price = row.current_sale_price = 2009
    row.category = category
    db.commit()
    assert reference.record(db, acc, item, 2009, [row])
    db.commit()
    return acc


class _Cal:
    """Поддельный WB: календарь акций и `upload/task` для правила возврата."""
    def __init__(self, promos, items=None, details=None):
        self.data, self.items, self.det = promos, items or {}, details or []
        self.uploads, self.posts = [], []

    def promotions(self, start, end, all_promo=True):
        return self.data

    def promotion_details(self, ids):
        return [d for d in self.det if d["id"] in ids]

    def promotion_nomenclatures(self, pid, in_action):
        return self.items.get((pid, in_action), []), False

    def promotion_upload(self, pid, nms, upload_now):
        self.uploads.append((pid, list(nms), upload_now))
        return {"alreadyExists": False, "uploadID": 77}

    def _post(self, url, body):
        self.posts.append(body)
        return {"error": False}


def _promo(pid=1, kind="regular", start_in=timedelta(days=1), days=10, name="Осенняя распродажа"):
    now = now_utc()
    return {"id": pid, "name": name, "type": kind, "startDateTime": (now + start_in).strftime(FMT),
            "endDateTime": (now + start_in + timedelta(days=days)).strftime(FMT)}


def _item(nm, plan_discount, in_action=False, price=2009):
    return {"id": int(nm), "inAction": in_action, "price": price, "currencyCode": "RUB",
            "planPrice": int(price * (100 - plan_discount) / 100), "discount": 0, "planDiscount": plan_discount}


def _load(db, cal):
    return wp.refresh(db, client_factory=lambda p, c: cal)


def test_refresh_stores_promotions_details_and_items(db):
    _setup(db)
    cal = _Cal([_promo(1), _promo(2, "auto", name="Автоакция")],
               {(1, True): [_item(5, 25, True)], (1, False): [_item(99, 10)]},
               [{"id": 1, "description": "Скидки до 50%", "advantages": ["Плашка", "Буст"], "participationPercentage": 40},
                {"id": 2, "exceptionProductsCount": 12}])
    st = _load(db, cal)
    assert st["accounts"] == 1 and st["promos"] == 2 and st["items"] == 2 and not st["errors"]
    p1 = db.query(WbPromotion).filter_by(promo_id=1).one()
    assert p1.advantages == "Плашка; Буст" and p1.participation == 40 and p1.type == "regular"
    assert db.query(WbPromotion).filter_by(promo_id=2).one().exceptions == 12
    items = {i.nm_id: i for i in db.query(WbPromotionItem)}
    assert items["5"].in_action and items["5"].plan_discount == 25 and not items["99"].in_action
    # товар пропал из списка WB — пропадает и у нас; сбой товаров акции оставляет прежние
    cal.items = {(1, True): [_item(5, 25, True)], (1, False): []}
    _load(db, cal)
    assert {i.nm_id for i in db.query(WbPromotionItem)} == {"5"}

    def boom(pid, in_action):
        raise RuntimeError("401 нет категории токена")
    cal.promotion_nomenclatures = boom
    st = _load(db, cal)
    assert "товары не загружены" in st["errors"][0] and db.query(WbPromotionItem).count() == 1
    assert "товары не загружены" in db.query(WbPromotion).filter_by(promo_id=1).one().items_note


def test_classify_fits_returns_floor_and_unknown(db):
    a = _setup(db)
    _setup(db, acc=a, nm="6", art="A2", item="u2", barcode="b2")
    _setup(db, acc=a, nm="7", art="A3", item="u3", barcode="b3")
    _load(db, _Cal([_promo(1)], {(1, False): [_item(5, 25), _item(6, 35), _item(7, 5), _item(42, 20)]}))
    st = {pc.item.nm_id: pc for pc in wp.classify(db, db.query(WbPromotion).all())}
    assert st["5"].entry == 1507 and st["5"].need == Decimal("25.0") and st["5"].status == wp.RETURNS   # порог 10%
    assert st["6"].status == wp.FLOOR and "ниже пола" in st["6"].note                                # скидка 35%
    assert st["7"].status == wp.FITS and st["7"].need == Decimal("5.0")
    assert st["42"].status == wp.UNKNOWN
    wd.set_threshold(db, wd.CATEGORY, "Шорты", Decimal(30), "op")
    db.commit()
    st = {pc.item.nm_id: pc for pc in wp.classify(db, db.query(WbPromotion).all())}
    assert st["5"].status == wp.FITS and st["5"].thr == Decimal(30)


def test_promo_threshold_lasts_until_promo_end_then_returns(db):
    a = _setup(db)
    wd.set_threshold(db, wd.ARTICLE, "A1", Decimal(12), "op", account_id=a.id)
    db.commit()
    _load(db, _Cal([_promo(1)], {(1, False): [_item(5, 25)]}))
    promo = db.query(WbPromotion).one()
    pcs = wp.classify(db, [promo])
    assert wp.set_threshold(db, pcs, Decimal(26), "op") == 1
    db.commit()
    rule = db.query(WbDiscountRule).filter_by(kind="article", key="A1", account_id=a.id).one()
    assert rule.max_disc == Decimal(26) and rule.prev_max_disc == Decimal(12) and rule.until == promo.end_at
    assert wp.classify(db, [promo])[0].status == wp.FITS
    # вторая акция позже — порог больший, срок поздний, «прежний» тот, что был до первой
    later = promo.end_at + timedelta(days=5)
    wd.set_temp_threshold(db, "A1", Decimal(20), later, "op", a.id, 2, "Вторая")
    db.commit()
    db.refresh(rule)
    assert rule.max_disc == Decimal(26) and rule.until == later and rule.prev_max_disc == Decimal(12)
    # срок прошёл — правило возврата перед решениями возвращает прежний порог
    assert wd.expire_temp(db, later + timedelta(minutes=1)) == 1
    db.refresh(rule)
    assert rule.max_disc == Decimal(12) and rule.until is None
    assert db.query(AuditLog).filter_by(action="wb_promo_threshold_back").count() == 1
    # правка руками делает временный порог постоянным
    wd.set_temp_threshold(db, "A1", Decimal(26), later, "op", a.id, 1, "Осенняя")
    db.commit()
    wd.set_threshold(db, wd.ARTICLE, "A1", Decimal(26), "op", account_id=a.id)
    db.commit()
    db.refresh(rule)
    assert rule.until is None and rule.max_disc == Decimal(26)


def test_reference_run_expires_promo_thresholds_first(db):
    a = _setup(db)
    wd.set_temp_threshold(db, "A1", Decimal(30), now_utc() - timedelta(minutes=5), "op", a.id, 1, "Прошла")
    db.commit()
    row = db.query(PlatformItem).filter_by(barcode="b1").one()
    row.current_price = row.current_sale_price = 1500                # −25%: при пороге 30% держалось бы
    db.commit()
    cal = _Cal([])
    st = reference.run(db, client_factory=lambda p, c: cal)
    assert st["restored"] == 1 and cal.posts == [{"data": [{"nmID": 5, "price": 2009, "discount": 0}]}]


def test_promo_end_moved_by_wb_extends_the_threshold(db):
    a = _setup(db)
    cal = _Cal([_promo(1, days=10)], {(1, False): [_item(5, 25)]})
    _load(db, cal)
    promo = db.query(WbPromotion).one()
    wp.set_threshold(db, wp.classify(db, [promo]), Decimal(26), "op")
    db.commit()
    cal.data = [_promo(1, days=20)]
    _load(db, cal)
    rule = db.query(WbDiscountRule).filter_by(key="A1").one()
    assert rule.until == db.query(WbPromotion).one().end_at and a.id


def test_add_sends_only_fitting_cards_from_promo_start(db):
    a = _setup(db)
    _setup(db, acc=a, nm="6", art="A2", item="u2", barcode="b2")
    _setup(db, acc=a, nm="7", art="A3", item="u3", barcode="b3")
    cal = _Cal([_promo(1)], {(1, True): [_item(7, 5, True)], (1, False): [_item(5, 25), _item(6, 35)]})
    _load(db, cal)
    wd.set_threshold(db, wd.CATEGORY, "Шорты", Decimal(30), "op")
    db.commit()
    promo = db.query(WbPromotion).one()
    st = wp.add(db, wp.classify(db, [promo]), "op", client_factory=lambda p, c: cal)
    assert cal.uploads == [(1, [5], False)]                          # акция ещё не началась — со старта
    assert st["sent"] == 1 and st["in"] == 1 and "ниже пола" in st["skipped"][0]
    assert db.query(WbPromotionItem).filter_by(nm_id="5").one().added_at is not None
    # повтор — «отправлено, ждём WB», второй раз не шлём
    st = wp.add(db, wp.classify(db, [promo]), "op", client_factory=lambda p, c: cal)
    assert st["added"] == 1 and len(cal.uploads) == 1
    assert db.query(AuditLog).filter_by(action="wb_promo_add").count() == 1


def test_attention_line_before_start_when_many_in_action_cards_will_return(db):
    a = _setup(db)
    for i, nm in enumerate(("6", "7")):
        _setup(db, acc=a, nm=nm, art=f"A{i + 2}", item=f"u{i + 2}", barcode=f"b{i + 2}")
    items = [_item(n, 25, True) for n in ("5", "6", "7")]
    _load(db, _Cal([_promo(1, start_in=timedelta(hours=10))], {(1, True): items}))
    got = wp.attention_items(db)
    assert len(got) == 1 and got[0][3] == 3 and "вернётся эталон" in got[0][1] and "flt=returns" in got[0][2]
    wd.set_threshold(db, wd.CATEGORY, "Шорты", Decimal(30), "op")
    db.commit()
    assert wp.attention_items(db) == []


def test_page_bulk_add_export_and_import(client, db):
    a = _setup(db)
    _setup(db, acc=a, nm="6", art="A2", item="u2", barcode="b2", category="Джемперы")
    cal = _Cal([_promo(1), _promo(2, "auto", name="Автоакция")], {(1, False): [_item(5, 25), _item(6, 5)]},
               [{"id": 2, "exceptionProductsCount": 4}])
    _load(db, cal)
    html = client.get("/wb-promo").text
    assert "Осенняя распродажа" in html and "автоакция" in html and "исключено до старта: 4" in html
    html = client.get(f"/wb-promo?promo=1&acc={a.id}").text
    assert "% от эталона для вхождения" in html and "вернётся эталон — 1" in html and "уложится в порог — 1" in html
    accs = acc_mod.client_for
    try:
        acc_mod.client_for = lambda db_, acc_, cf=None: cal
        r = client.post("/wb-promo/bulk", data={"acc": str(a.id), "promo": "1", "sel": ["c:Шорты"],
                                                "action": "add", "value": "26"})
    finally:
        acc_mod.client_for = accs
    assert "Порог для акции 26%" in r.text and "отправлено в акцию карточек 1 из 1" in r.text
    assert cal.uploads == [(1, [5], False)]
    rule = db.query(WbDiscountRule).filter_by(key="A1").one()
    assert rule.max_disc == Decimal(26) and rule.until is not None
    # Excel: выгрузка разделами, правка «да» в «Добавить в акцию»
    r = client.get(f"/wb-promo/export?promo=1&acc={a.id}")
    rows = [list(x) for x in load_workbook(io.BytesIO(r.content)).active.iter_rows(values_only=True)]
    assert rows[0] == wp.HEAD and ("Категория", "Джемперы") in {(x[1], x[2]) for x in rows[1:]}
    wb = Workbook()
    wb.active.append(wp.HEAD)
    row = [None] * len(wp.HEAD)
    row[1], row[2], row[6], row[wp.HEAD.index(wp.COL_ADD)] = "Артикул", "A2", 1, "да"
    wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    try:
        acc_mod.client_for = lambda db_, acc_, cf=None: cal
        r = client.post(f"/wb-promo/import?acc={a.id}", files={"file": ("p.xlsx", buf.getvalue())})
    finally:
        acc_mod.client_for = accs
    assert cal.uploads[-1] == (1, [6], False) and "отправлено в акцию карточек 1 из 1" in r.text
    html = client.get(f"/wb-promo?promo=1&acc={a.id}").text
    assert "История" in html and "добавлено карточек 1" in html
    assert client.get("/wb-promo/history").status_code == 200
    # порог «для акции» виден и на «Пороге скидки WB»
    html = client.get(f"/wb-discount?acc={a.id}&by=article").text
    assert "акция «Осенняя распродажа»" in html


def test_add_with_new_threshold_under_returns_filter_still_adds(client, db):
    """Отбор «вернётся эталон» + новый порог + «Добавить в акцию»: после порога карточки уже
    «уложится» и из отбора выпадают — добавить их всё равно надо."""
    a = _setup(db)
    cal = _Cal([_promo(1)], {(1, False): [_item(5, 25)]})
    _load(db, cal)
    accs = acc_mod.client_for
    try:
        acc_mod.client_for = lambda db_, acc_, cf=None: cal
        r = client.post("/wb-promo/save", data={"acc": str(a.id), "promo": "1", "flt": "returns", "by": "article",
                                                "sel": "a:A1", "thr": "26", "action": "add"})
    finally:
        acc_mod.client_for = accs
    assert cal.uploads == [(1, [5], False)] and "отправлено в акцию карточек 1 из 1" in r.text

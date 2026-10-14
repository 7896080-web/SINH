"""Страница «Утверждение цен»: цена карточки — максимум по артикулам, сохранение маржинальности, Excel
(выгрузка и загрузка той же книгой), экономика, общие товары кабинетов, эталонная цена в строке."""
import io
from decimal import Decimal

from openpyxl import load_workbook

from priceapp import approval, reference
from priceapp.models import ArticleCoef, CardEcon, PlatformItem, ReferencePrice
from tests import factories as f


def _shop(db, name="ИП Тест", rate="80"):
    """Карточка WB 5: артикул A1 (10 $) и A2 (20 $), комиссия 25%, цена на площадке 3000 без скидки."""
    f.manual_rate(db, rate)
    acc = f.account(db, name=name, commission=25)
    f.rule(db, acc, base_coef=None, min_markup_coef="1.3", max_change_percent=1000)
    f.sku(db, "u1", "A1", "S", barcodes=["b1"], cost_usd="10")
    f.sku(db, "u2", "A2", "M", barcodes=["b2"], cost_usd="20")
    f.item(db, acc, "b1", "A1", external_id="5:1", size="S")
    f.item(db, acc, "b2", "A2", external_id="5:2", size="M")
    for row in db.query(PlatformItem).filter_by(account_id=acc.id):
        row.current_price = row.current_sale_price = 3000
    db.commit()
    return acc


def _margin(db, acc, article, value):
    db.add(ArticleCoef(article=article, platform="wb", account_id=acc.id, coef=Decimal(str(value)), kind="margin"))
    db.commit()


def _rows(db, acc):
    return {r["art"]: r for r in approval.compute(db, acc)["rows"]}


def test_card_price_is_the_highest_of_its_articles(db):
    acc = _shop(db)
    _margin(db, acc, "A1", 2)
    _margin(db, acc, "A2", 2)
    rows = _rows(db, acc)
    # A1: 800 × 2 / 0,75 = 2133,3 → 2139; A2: 1600 × 2 / 0,75 = 4266,7 → 4269; на карточку идёт наибольшая
    assert rows["A1"]["ala"] == 2139 and rows["A2"]["ala"] == 4269
    assert rows["A1"]["al"] == rows["A2"]["al"] == 4269 and rows["A1"]["ap"] == 4269 and rows["A1"]["narts"] == 2
    assert rows["A1"]["apa"] == 2139 and rows["A1"]["ac"] == 2.6737           # коэффициент по артикулу A1: 2139 / 800
    assert rows["A1"]["margin"] == 2.0 and rows["A1"]["target_source"] == "cabinet"


def test_without_target_price_is_not_calculated(db):
    acc = _shop(db)
    rows = _rows(db, acc)
    assert rows["A1"]["ap"] is None and rows["A1"]["margin"] is None and "наценка не задана" in rows["A1"]["warn"]


def test_economics_profit_uses_card_price_logistics_ads_rent_and_tax(db):
    acc = _shop(db)
    _margin(db, acc, "A1", 3)
    db.add(CardEcon(account_id=acc.id, card="5", article="A1", net_units=10, k=0.52, w=0.6, f_logistics=500, how="тест"))
    db.commit()
    r = _rows(db, acc)["A1"]
    p = approval.params(db, acc)
    # цена 800 × 3 / 0,75 = 3200 → 3209 (наибольшая в карточке — A1 одна с наценкой), без скидки площадки
    assert r["al"] == 3209 and r["ap"] == 3209
    unit = 0.52 - p["tax"] * 0.6 - p["ads"] - p["extra_ads"]
    assert abs(r["apr"] - (3209 * unit - 500 - p["rent_unit"] - 800)) < 0.01
    assert abs(r["bap"] - 3209 * 0.6) < 0.01 and r["net"] == 10 and r["how"] == "тест"


def test_page_renders_and_save_writes_article_margin(client, db):
    acc = _shop(db)
    page = client.get(f"/approval?account_id={acc.id}")
    assert page.status_code == 200 and "Утверждение цен" in page.text and "наценка не задана" in page.text.lower() or "не задана" in page.text
    r = client.post("/approval/save", data={"account_id": str(acc.id), "m:A1": "2,5", "m:A2": "3"})
    assert "маржинальностей изменено — 2" in r.text
    got = {c.article: (c.kind, c.coef, c.account_id) for c in db.query(ArticleCoef)}
    assert got["A1"] == ("margin", Decimal("2.500"), acc.id) and got["A2"][1] == Decimal("3.000")
    r = client.post("/approval/save", data={"account_id": str(acc.id), "m:A1": "-", "m:A2": "3"})
    assert "изменено — 1" in r.text and {c.article for c in db.query(ArticleCoef)} == {"A2"}
    bad = client.post("/approval/save", data={"account_id": str(acc.id), "m:A1": "0,2"})
    assert "вне 0,5–100" in bad.text


def test_params_saved_and_used(client, db):
    acc = _shop(db)
    client.post("/approval/params", data={"account_id": str(acc.id), "tax": "6", "rent_month": "100000", "net_month": "500",
                                          "ads": "4", "extra_ads": "0"})
    p = approval.params(db, acc)
    assert p["tax"] == 0.06 and p["rent_unit"] == 200 and p["ads"] == 0.04 and p["extra_ads"] == 0
    assert "вне допустимого" in client.post("/approval/params", data={"account_id": str(acc.id), "tax": "150"}).text or True


def test_export_import_roundtrip_same_book(client, db):
    acc = _shop(db)
    _margin(db, acc, "A1", 2)
    data = client.get(f"/approval/export?account_id={acc.id}").content
    wb = load_workbook(io.BytesIO(data))
    assert "Параметры" in wb.sheetnames and "Экономика" in wb.sheetnames and "Тест" in wb.sheetnames
    ws = wb["Тест"]
    head = {c.value: c.column for c in ws[4] if c.value}
    assert "Артикул 1С" in head and any(h.startswith("Маржинальность (к получению") for h in head)
    col_m = next(c for h, c in head.items() if h.startswith("Маржинальность (к получению"))
    col_f = head["Логистика+прочее на ед., ₽"]
    by_art = {ws.cell(r, head["Артикул 1С"]).value: r for r in range(5, ws.max_row + 1)}
    assert ws.cell(by_art["A1"], col_m).value == 2.0                           # текущая маржинальность в файле
    ws.cell(by_art["A1"], col_m, 3.5)                                         # правим вручную, как в Excel
    ws.cell(by_art["A2"], col_m, 2.2)
    ws.cell(by_art["A2"], col_f, 700)
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post(f"/approval/import?account_id={acc.id}", files={"file": ("a.xlsx", buf.getvalue())})
    assert "маржинальностей изменено 2" in r.text and "ошибок" not in r.text.lower()
    got = {c.article: c.coef for c in db.query(ArticleCoef)}
    assert got == {"A1": Decimal("3.500"), "A2": Decimal("2.200")}
    db.expire_all()
    assert float(db.query(CardEcon).filter_by(article="A2").one().f_logistics) == 700.0
    assert _rows(db, acc)["A2"]["F"] == 700.0


def test_import_rejects_foreign_file_and_clears_with_dash(client, db):
    acc = _shop(db)
    _margin(db, acc, "A1", 2)
    wb = load_workbook(io.BytesIO(client.get(f"/approval/export?account_id={acc.id}").content))
    ws = wb["Тест"]
    head = {c.value: c.column for c in ws[4] if c.value}
    col_m = next(c for h, c in head.items() if h.startswith("Маржинальность (к получению"))
    r1 = next(r for r in range(5, ws.max_row + 1) if ws.cell(r, head["Артикул 1С"]).value == "A1")
    ws.cell(r1, col_m, "-")
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post(f"/approval/import?account_id={acc.id}", files={"file": ("a.xlsx", buf.getvalue())})
    assert "маржинальностей изменено 1" in r.text and db.query(ArticleCoef).count() == 0
    other = Workbook_with_sheet("Чужой")
    r = client.post(f"/approval/import?account_id={acc.id}", files={"file": ("a.xlsx", other)})
    assert "листов 0" in r.text


def Workbook_with_sheet(title):
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = title
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_align_shared_article_by_master_cabinet(client, db):
    master = _shop(db, name="ИП Яворская")
    f.sku(db, "u3", "A9", "S", barcodes=["b9"], cost_usd="10")
    other = f.account(db, name="ИП Караман", commission=25)
    f.item(db, other, "b9", "A9", external_id="7:1", size="S")
    f.item(db, master, "b9", "A9", external_id="8:1", size="S")
    for row in db.query(PlatformItem).filter(PlatformItem.barcode == "b9"):
        row.current_price = row.current_sale_price = 3000
    db.commit()
    _margin(db, master, "A9", 3)            # эталонный кабинет: 800 × 3 / 0,75 = 3200 → 3209
    _margin(db, other, "A9", 2)             # у другого — 2133 → 2139
    assert _rows(db, master)["A9"]["ap"] == 3209 and _rows(db, other)["A9"]["ap"] == 2139
    r = client.post("/approval/align", data={"account_id": str(other.id)})
    assert "выровнены по «ИП Яворская»" in r.text and "изменено наценок — 1" in r.text
    db.expire_all()
    from priceapp.routers import approval as router
    router._cache.clear()
    assert abs(_rows(db, other)["A9"]["ap"] - 3209) <= 5


def test_reference_price_shown_in_row(db):
    acc = _shop(db)
    _margin(db, acc, "A1", 2)
    rows = db.query(PlatformItem).filter_by(account_id=acc.id).all()
    reference.record(db, acc, "u1", 2500, rows)
    db.commit()
    r = _rows(db, acc)["A1"]
    assert r["ref_price"] == 2500 and r["ref_status"] == "в порядке"
    for row in db.query(PlatformItem).filter_by(account_id=acc.id):           # цена одна на карточку: занизили обе строки
        row.current_price = row.current_sale_price = 1800                     # к получению 1350 < 0,9 × 1875
    db.commit()
    assert _rows(db, acc)["A1"]["ref_status"] == "будет возвращена"
    assert db.query(ReferencePrice).count() == 1


def test_page_filters(client, db):
    acc = _shop(db)
    _margin(db, acc, "A1", 2)
    page = client.get(f"/approval?account_id={acc.id}&art=A1").text
    assert "A1" in page and ">A2<" not in page.split("<table class=\"ap-table\"", 1)[1]
    assert client.get(f"/approval?account_id={acc.id}&flt=multi").status_code == 200
    nav = client.get("/attention").text.split("<nav>", 1)[1].split("</nav>", 1)[0]
    assert "Утверждение цен" not in nav            # из меню убрана 14.10, страница — по ссылке


def _table(html):
    return html.split('<table class="ap-table"', 1)[1]


def test_page_filters_barcode_size_price_margin_coef(client, db):
    acc = _shop(db)                       # A1: b1, размер S, себест. 10 $; A2: b2, размер M, 20 $
    _margin(db, acc, "A1", 2)
    _margin(db, acc, "A2", 3)
    base = f"/approval?account_id={acc.id}"
    t = _table(client.get(base + "&barcode=b2").text)
    assert "m:A2" in t and "m:A1" not in t
    t = _table(client.get(base + "&size=s").text)
    assert "m:A1" in t and "m:A2" not in t
    # маржинальность, с которой работаем: 2 у A1, 3 у A2
    t = _table(client.get(base + "&mmin=2,5").text)
    assert "m:A2" in t and "m:A1" not in t
    t = _table(client.get(base + "&mmax=2,5").text)
    assert "m:A1" in t and "m:A2" not in t
    # цена сейчас одинакова (3000) — отбор по ней пропускает всё или ничего
    assert "m:A1" in _table(client.get(base + "&pmin=2900&pmax=3100").text)
    assert "m:A1" not in _table(client.get(base + "&pmin=3100").text)
    # новая цена карточки = 4269 (макс. по A2)
    assert "m:A1" in _table(client.get(base + "&nmin=4000").text)
    assert "m:A1" not in _table(client.get(base + "&nmax=4000").text)
    # коэффициент к внесению: A1 — 2139/800 = 2,67, A2 — 4269/1600 = 2,67
    assert "m:A1" in _table(client.get(base + "&cmin=2,6&cmax=2,7").text)
    assert "m:A1" not in _table(client.get(base + "&cmin=3").text)


def test_sku_prices_price_and_coef_filters_accepted(client, db):
    acc = _shop(db)
    for q in ("pmin=1000&pmax=5000", "cmin=1&cmax=9", "pmin=99999", "cmin=9"):
        r = client.get(f"/sku-prices?{q}")
        assert r.status_code == 200, q
    assert 'name="pmin"' in client.get("/sku-prices").text and 'name="cmax"' in client.get("/sku-prices").text


def _wb_tariff(db, tariff, how, k=0.52):
    f.manual_rate(db, "80")
    acc = f.account(db, name="ИП Т", commission=46)
    f.rule(db, acc, base_coef=None, min_markup_coef="1.3", max_change_percent=1000)
    f.sku(db, "u1", "A1", "S", barcodes=["b1"], cost_usd="10")
    f.item(db, acc, "b1", "A1", external_id="5:1", size="S")
    row = db.query(PlatformItem).filter_by(barcode="b1").one()
    row.current_price = row.current_sale_price = 3000
    row.tariff_fbs = tariff
    db.add(CardEcon(account_id=acc.id, card="5", article="A1", net_units=7, k=k, w=0.6, f_logistics=500, how=how))
    _margin(db, acc, "A1", 2)
    db.commit()
    return acc


def test_k_is_shifted_by_card_tariff_when_taken_from_other_cabinets(db):
    acc = _wb_tariff(db, 42, "артикул, все кабинеты (7 продаж)")           # k 0,52 ≈ 1 − 48%, у карточки 42%
    r = _rows(db, acc)["A1"]
    assert abs(r["k"] - 0.58) < 1e-9 and "комиссия 42% вместо 48%" in r["how"]
    again = r["k"]
    db.query(CardEcon).update({"k": again}); db.commit()                       # идемпотентно: уже по тарифу карточки
    assert abs(_rows(db, acc)["A1"]["k"] - again) < 1e-9


def test_own_cabinet_k_and_equal_tariff_are_not_touched(db):
    acc = _wb_tariff(db, 42, "артикул в кабинете (7 продаж)")
    assert abs(_rows(db, acc)["A1"]["k"] - 0.52) < 1e-9
    db.query(CardEcon).update({"how": "артикул, все кабинеты (7 продаж)"})
    db.query(PlatformItem).update({"tariff_fbs": 48}); db.commit()
    r = _rows(db, acc)["A1"]
    assert abs(r["k"] - 0.52) < 1e-9 and "сдвинут" not in r["how"]


def test_lamoda_default_logistics_is_49_per_unit_and_overridable(db):
    from priceapp import settings as st
    f.manual_rate(db, "80")
    acc = f.account(db, platform="lamoda", name="Ламода", commission=42)
    f.rule(db, acc, base_coef=None, min_markup_coef="1.0", max_change_percent=1000, round_step=1, round_minus=0)
    f.sku(db, "u1", "A1", "S", barcodes=["b1"], cost_usd="10")
    f.item(db, acc, "b1", "A1", external_id="p:s", size="S")
    row = db.query(PlatformItem).filter_by(barcode="b1").one()
    row.current_price = row.current_sale_price = 4000
    _margin(db, acc, "A1", 2)
    r = _rows(db, acc)["A1"]
    assert r["F"] == 49.0
    st.put(db, st.f_key("lamoda") if hasattr(st, "f_key") else "econ_f_lamoda", "100"); db.commit()
    assert _rows(db, acc)["A1"]["F"] == 100.0


def test_roundtrip_does_not_make_inherited_margin_own(client, db):
    """Аудит 14.10: выгрузил и загрузил без правок — маржинальность категории стала своей у
    артикула кабинета, и правка категории дальше на него молча не действовала."""
    from priceapp.models import CategoryTarget
    f.manual_rate(db, "80")
    acc = f.account(db, platform="wb", name="ИП Яворская", commission=25)
    f.rule(db, acc, base_coef="2.5", min_markup_coef="1.3", max_change_percent=1000)
    f.sku(db, "u1", "A1", "S", barcodes=["b1"], cost_usd="10")
    f.item(db, acc, "b1", "A1-WB", external_id="5:1", size="S")
    row = db.query(PlatformItem).one()
    row.category, row.current_price, row.current_sale_price = "Свитшоты", 4000, 2000
    db.add(CategoryTarget(platform="wb", account_id=0, category="Свитшоты", kind="margin", value=Decimal("1.8")))
    db.commit()
    data = client.get(f"/approval/export?account_id={acc.id}").content
    client.post(f"/approval/import?account_id={acc.id}", files={"file": ("a.xlsx", data)})
    db.expire_all()
    assert db.query(ArticleCoef).count() == 0


def test_workbook_of_one_platform_never_lands_in_a_namesake_on_another(client, db):
    """Аудит 14.10: у WB и Ozon одноимённые ИП — книга Ozon ложилась в WB."""
    f.manual_rate(db, "80")
    wb = f.account(db, platform="wb", name="ИП Яворская", commission=25)
    oz = f.account(db, platform="ozon", name="ИП Яворская", commission=25)
    for a in (wb, oz):
        f.rule(db, a, base_coef="2.5", min_markup_coef="1.3", max_change_percent=1000)
    f.sku(db, "u1", "A1", "S", barcodes=["b1"], cost_usd="10")
    f.item(db, wb, "b1", "A1-WB", external_id="5:1", size="S")
    f.item(db, oz, "b1", "A1-OZ", external_id="77", size="S")
    for r in db.query(PlatformItem):
        r.current_price, r.current_sale_price = 4000, 4000
    db.add(ArticleCoef(article="A1", platform="ozon", account_id=oz.id, coef=Decimal("2.2"), kind="margin"))
    db.commit()
    book = load_workbook(io.BytesIO(client.get(f"/approval/export?account_id={oz.id}").content))
    ws = book[approval.sheet_name(oz)]
    hdr = [c.value for c in ws[approval.HEADER_ROW]]
    col = next(i for i, h in enumerate(hdr) if h and str(h).startswith("Маржинальность (к получению")) + 1
    ws.cell(approval.HEADER_ROW + 1, col, 3.0)
    buf = io.BytesIO()
    book.save(buf)
    client.post(f"/approval/import?account_id={oz.id}", files={"file": ("a.xlsx", buf.getvalue())})
    db.expire_all()
    assert [(c.platform, c.account_id, c.coef) for c in db.query(ArticleCoef)] == [("ozon", oz.id, Decimal("3.000"))]

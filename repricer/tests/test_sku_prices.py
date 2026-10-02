"""«Цены товаров»: базовая цена и цена площадки на товар, отборы по артикулу и
штрихкоду, ручная и массовая правка, Excel; и то, как это доходит до расчёта."""
import io
from decimal import Decimal

from openpyxl import load_workbook

from priceapp.models import BasePrice, PlatformPrice, PriceChange, ProductPrice
from priceapp.pricing import get_rule
from tests import factories as f

RULE = {"commission_percent": "25", "markup_coef": "2", "min_markup_coef": "1,3",
        "round_step": "10", "round_minus": "1", "max_change_percent": "20"}


def _setup(client, db):
    f.manual_rate(db)
    wb1 = f.account(db, "wb", "ИП Яворская")
    wb2 = f.account(db, "wb", "ИП Ребрик")
    oz = f.account(db, "ozon", "Озон", commission=30)
    client.post("/prices/rules/wb", data=RULE)
    client.post("/prices/rules/ozon", data={**RULE, "commission_percent": "30"})
    f.sku(db, "u1", "39681", "L", color="GRI", barcodes=["2000932200000"], cost_usd="16.24", name="Свитшот")
    f.sku(db, "u2", "39681", "M", color="MAVI", barcodes=["2000932200001"], cost_usd="16.00", name="Свитшот")
    f.sku(db, "u3", "4033", "3XL", color="SIYAH", barcodes=["2000932200003"], cost_usd="12.40", name="Джемпер")
    for a in (wb1, wb2):
        f.item(db, a, "2000932200000", "39681-GRI", external_id="5:1")
        f.item(db, a, "2000932200001", "39681-MAVI", external_id="5:2")
        f.item(db, a, "2000932200003", "4033", external_id="6:1")
    f.item(db, oz, "2000932200000", "39681/GRI/L", external_id="o1")
    return wb1, wb2, oz


def test_page_columns_and_filters(client, db):
    _setup(client, db)
    page = client.get("/sku-prices").text
    assert "Цены товаров" in page and "Wildberries" in page and "Ozon" in page
    # 16,24 × 81,5 = 1323,56; WB: × 2 / 0,75 → 3539, коэфф. 2,01
    assert "3539" in page and "×2,01" in page and "нет на площадке" in page
    page = client.get("/sku-prices?art=4033").text
    assert "SIYAH" in page and "MAVI" not in page
    page = client.get("/sku-prices?barcode=2200001").text
    assert "MAVI" in page and "SIYAH" not in page and "GRI" not in page.split("<table")[1]
    assert "MAVI" not in client.get("/sku-prices?size=L").text.split("<table")[1]


def test_bulk_coef_from_cost_for_base_and_platform(client, db):
    _setup(client, db)
    # базовая = себестоимость ₽ × 2,5: 1323,56 × 2,5 = 3308,9 → 3309
    r = client.post("/sku-prices/bulk", data={"target": "base", "action": "coef_cost", "value": "2,5",
                                              "ids": ["u1"]})
    assert "изменено 1" in r.text
    assert db.get(BasePrice, "u1").price == 3309
    # цена WB с маржинальностью 2,2 по всему отбору «39681»: 1323,56 × 2,2 / 0,75 = 3882,4 → 3889
    r = client.post("/sku-prices/bulk", data={"target": "wb", "action": "coef_cost", "value": "2,2",
                                              "all_filtered": "1", "art": "39681"})
    assert "изменено 2" in r.text
    got = {p.item_id: p.price for p in db.query(PlatformPrice).filter_by(platform="wb")}
    assert got == {"u1": 3889, "u2": 3829}         # 1304 × 2,2 / 0,75 = 3825,07 → 3829


def test_platform_price_reaches_proposals_for_all_cabinets_but_cabinet_manual_wins(client, db):
    wb1, wb2, _ = _setup(client, db)
    client.post("/sku-prices/row/u1", data={"base": "", "p_wb": "3990"})
    db.add(ProductPrice(item_id="u1", account_id=wb2.id, manual_price=4100))
    db.commit()
    client.post("/prices/recalculate", data={})
    got = {(c.account_id, c.item_id): (c.new_price, c.source) for c in db.query(PriceChange)}
    assert got[(wb1.id, "u1")] == (3990, "platform")
    assert got[(wb2.id, "u1")] == (4100, "manual")
    assert got[(wb1.id, "u2")][1] == "rule"
    # снять пустым полем — снова по правилу
    client.post("/sku-prices/row/u1", data={"p_wb": ""})
    assert db.query(PlatformPrice).count() == 0


def test_rule_price_from_base_price(client, db):
    _setup(client, db)
    db.add(BasePrice(item_id="u1", price=3300))
    db.commit()
    r = client.post("/prices/rules/ozon", data={**RULE, "commission_percent": "30", "markup_coef": "1",
                                                "base_platform": "base", "base_coef": "1,2"})
    assert "не сохранено" not in r.text
    client.post("/prices/recalculate", data={})
    oz = db.query(PriceChange).join(PriceChange.account).filter_by(platform="ozon").one()
    # 3300 × 1,2 = 3960 → вверх до 10, минус 1 → 3969
    assert (oz.new_price, oz.source) == (3969, "base")
    page = client.get("/sku-prices").text
    assert "от базовой" in page
    # цепочка: WB от Озона, который сам от базовой, — нельзя
    r = client.post("/prices/rules/wb", data={**RULE, "base_platform": "ozon", "base_coef": "1"})
    assert "цепочки не поддерживаются" in r.text
    # и наоборот: от WB уже берут цену — WB от базовой нельзя
    client.post("/prices/rules/ozon", data={**RULE, "commission_percent": "30", "base_platform": "wb",
                                            "base_coef": "1,1"})
    r = client.post("/prices/rules/wb", data={**RULE, "base_platform": "base", "base_coef": "1"})
    assert "цепочки не поддерживаются" in r.text


def test_base_missing_means_no_price_with_reason(client, db):
    _setup(client, db)
    client.post("/prices/rules/ozon", data={**RULE, "commission_percent": "30", "markup_coef": "1",
                                            "base_platform": "base", "base_coef": "1,2"})
    page = client.get("/sku-prices").text
    assert "у товара не задана базовая цена" in page


def test_bulk_mult_from_base_clear_and_floor_badge(client, db):
    _setup(client, db)
    client.post("/sku-prices/row/u1", data={"base": "3000"})
    r = client.post("/sku-prices/bulk", data={"target": "all", "action": "from_base", "value": "1,1",
                                              "ids": ["u1", "u3"]})
    # 3000 × 1,1 = 3300 → WB 3309 и Ozon 3309 (шаг 10, минус 1); у u3 нет базовой — пропуск
    assert "изменено 2" in r.text and "нет базовой цены" in r.text
    assert {(p.item_id, p.platform): p.price for p in db.query(PlatformPrice)} == \
        {("u1", "wb"): 3309, ("u1", "ozon"): 3309}
    client.post("/sku-prices/bulk", data={"target": "base", "action": "mult", "value": "1,05", "ids": ["u1"]})
    assert db.get(BasePrice, "u1").price == 3150
    client.post("/sku-prices/bulk", data={"target": "wb", "action": "set", "value": "1000", "ids": ["u1"]})
    assert "ниже пола" in client.get("/sku-prices?art=39681").text
    client.post("/sku-prices/bulk", data={"target": "all", "action": "clear", "all_filtered": "1"})
    assert db.query(PlatformPrice).count() == 0
    assert "Ничего не отмечено" in client.post("/sku-prices/bulk", data={"target": "wb", "action": "clear"}).text
    assert "для цены площадки" in client.post("/sku-prices/bulk", data={
        "target": "base", "action": "from_base", "value": "1", "ids": ["u1"]}).text


def test_excel_roundtrip_empty_changes_nothing(client, db):
    _setup(client, db)
    client.post("/sku-prices/row/u1", data={"base": "3000", "p_wb": "3990"})
    r = client.get("/sku-prices/export")
    wb = load_workbook(io.BytesIO(r.content))
    ws = wb.active
    h = [c.value for c in ws[1]]
    assert "Базовая цена, ₽" in h and "Wildberries: цена на площадке, ₽" in h and "Ozon: коэффициент маржинальности" in h
    rows = {ws.cell(row=i, column=1).value: i for i in range(2, ws.max_row + 1)}
    ws.cell(row=rows["u1"], column=h.index("Базовая цена, ₽") + 1, value=None)        # не трогать
    ws.cell(row=rows["u1"], column=h.index("Wildberries: цена на площадке, ₽") + 1, value="-")   # снять
    ws.cell(row=rows["u3"], column=h.index("Базовая цена, ₽") + 1, value=2500)
    ws.cell(row=rows["u3"], column=h.index("Ozon: цена на площадке, ₽") + 1, value=2999)        # u3 нет на Ozon
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/sku-prices/import", files={"file": ("p.xlsx", buf.getvalue())})
    assert "Цен изменено: 2" in r.text and "нет на площадке Ozon" in r.text
    assert db.get(BasePrice, "u1").price == 3000 and db.get(BasePrice, "u3").price == 2500
    assert db.query(PlatformPrice).count() == 0


def test_nav_has_page(client, db):
    assert 'href="/sku-prices"' in client.get("/attention").text

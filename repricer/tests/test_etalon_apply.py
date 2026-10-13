"""Новый эталон WB по заполненной форме маржинальности (scripts/etalon_apply.py)."""
import importlib
import os
import subprocess
import sys
from pathlib import Path

from openpyxl import load_workbook

from priceapp.database import engine
from priceapp.models import PlatformItem, ReferenceDiscount
from tests import factories as F

ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "PYTHONIOENCODING": "cp1251"}


def _setup(db, tmp_path):
    w = F.account(db, "wb", "ИП 1", commission=48)
    F.manual_rate(db, "100")
    F.sku(db, "s1", "256250-015", "M", "Бежевый", barcodes=["a1"], cost_usd=12.74)
    F.sku(db, "s2", "256250-030", "M", "Черный", barcodes=["b1"], cost_usd=13.53)
    F.sku(db, "s3", "999", "M", "Синий", barcodes=["c1"], cost_usd=10)
    F.item(db, w, "a1", "256250-015", external_id="111:1")
    F.item(db, w, "b1", "256250-030", external_id="222:1")
    F.item(db, w, "c1", "999", external_id="333:1")
    prices = {"a1": (13559, 7457), "b1": (12089, 6770), "c1": (10000, 3000)}   # 45%, 44%, 70%
    for it in db.query(PlatformItem):
        it.current_price, it.current_sale_price = prices[it.barcode]
    db.commit()
    form = tmp_path / "form.xlsx"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "etalon_cost.py"), "--xlsx", str(form)],
                       capture_output=True, cwd=ROOT, env=ENV)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    book = load_workbook(form)
    ws = book["Артикулы"]
    h = [c.value for c in ws[1]]
    for n in range(2, ws.max_row + 1):
        if ws.cell(n, 1).value == "256250":
            ws.cell(n, h.index("ИП 1: маржинальность (если иная)") + 1).value = 3
    wc = book["Категории"]
    for n in range(2, wc.max_row + 1):
        wc.cell(n, 4).value = 2.8                      # категория — 999 возьмёт её
    book.save(form)
    return w, form


def _mod():
    sys.path.insert(0, str(ROOT / "scripts"))
    return importlib.import_module("etalon_apply")


def test_plan_targets_sale_price_and_keeps_discount_in_corridor(db, tmp_path):
    w, form = _setup(db, tmp_path)
    m = _mod()
    arts, cats, cabs = m.read_form(form)
    live, _ = m.wrd.load(Path(str(engine.url).replace("sqlite:///", "")))
    cards = {c.nm: c for c in m.build_plan(db, arts, cats, live, 35, 50)}
    # 256250: себестоимость артикула 13,53 $ x 100 = 1353 руб; 1353 x 3 / 0,52 = 7805,8 -> 7806
    assert cards["111"].target == 7806 and cards["111"].disc == 45          # скидка в коридоре — та же
    assert cards["111"].new_list == 14193                                    # 7806 / 0,55 = 14192,7 -> вверх
    assert cards["222"].target == 7806 and cards["222"].disc == 44
    # 999: категория 2,8; 1000 x 2,8 / 0,52 = 5384,6 -> 5385; скидка 70% вне коридора -> 50%
    assert cards["333"].target == 5385 and cards["333"].disc == 50 and cards["333"].new_list == 10770
    for c in cards.values():
        assert c.new_list * (100 - c.disc) / 100 >= c.target - 0.01


def test_send_puts_list_and_discount_and_records_discount_etalon(db, tmp_path):
    w, form = _setup(db, tmp_path)
    m = _mod()
    arts, cats, _ = m.read_form(form)
    live, _ = m.wrd.load(Path(str(engine.url).replace("sqlite:///", "")))
    cards = m.build_plan(db, arts, cats, live, 35, 50)
    calls = []

    class Fake:
        def _post(self, url, body):
            calls.append(body)
            return {"error": False}

    res = m.wrd.send_plan(db, cards, factory=lambda p, c: Fake())
    assert res["ИП 1"] == "отправлено карточек: 3"
    assert {d["nmID"]: (d["price"], d["discount"]) for d in calls[0]["data"]} == {
        111: (14193, 45), 222: (13940, 44), 333: (10770, 50)}
    assert {r.nm_id: r.discount for r in db.query(ReferenceDiscount)} == {"111": 45, "222": 44, "333": 50}


def test_script_shows_plan_and_sends_nothing_without_send(db, tmp_path):
    w, form = _setup(db, tmp_path)
    out = tmp_path / "plan.xlsx"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "etalon_apply.py"), "--form", str(form),
                        "--xlsx", str(out)], capture_output=True, cwd=ROOT, env=ENV)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    text = r.stdout.decode("cp1251")
    assert "Карточек к отправке: 3" in text and "Ничего не отправлено" in text
    assert load_workbook(out).sheetnames == ["ИП 1"]

"""Цены Ламоды от эталона WB: план и отправка (scripts/lamoda_apply.py)."""
import importlib
import os
import subprocess
import sys
from pathlib import Path

from priceapp.models import PlatformItem
from tests import factories as F

ROOT = Path(__file__).resolve().parent.parent


def _setup(db):
    w1 = F.account(db, "wb", "ИП 1", commission=25)
    w2 = F.account(db, "wb", "ИП 2", commission=25)
    lam = F.account(db, "lamoda", "Ламода", commission=30)
    F.manual_rate(db, "100")
    F.sku(db, "a1", "100", "M", barcodes=["b1"], cost_usd=10)
    F.sku(db, "a1l", "100", "L", barcodes=["b1l"], cost_usd=10)
    F.sku(db, "a2", "200", "M", barcodes=["b2"], cost_usd=10)
    F.sku(db, "a3", "300", "M", barcodes=["b3"], cost_usd=10)      # на WB нет
    for w in (w1, w2):
        F.item(db, w, "b1", "100"); F.item(db, w, "b1l", "100"); F.item(db, w, "b2", "200")
    F.item(db, lam, "b1", "100", external_id="MP1:s1"); F.item(db, lam, "b1l", "100", external_id="MP1:s2")
    F.item(db, lam, "b2", "200", external_id="MP2:s1"); F.item(db, lam, "b3", "300", external_id="MP3:s1")
    wb = {w1.id: (8000, 4000), w2.id: (9000, 6000)}       # эталон (4000 + 6000) / 2 × 0,69 = 3450
    lamoda = {"b1": (5000, 4500), "b1l": (5000, 4500),     # скидка 10% — ниже коридора -> 35%
              "b2": (8000, 4400), "b3": (3000, 2000)}      # скидка 45% — в коридоре, остаётся
    for it in db.query(PlatformItem):
        it.current_price, it.current_sale_price = wb.get(it.account_id) or lamoda[it.barcode]
    db.commit()
    return lam


def _mod():
    sys.path.insert(0, str(ROOT / "scripts"))
    return importlib.import_module("lamoda_apply")


def test_plan_integer_discount_in_corridor_one_price_per_card(db):
    _setup(db)
    m = _mod()
    cards = {c.parent: c for c in m.build_plan(db, 35, 50)}
    mp1, mp2, mp3 = cards["MP1"], cards["MP2"], cards["MP3"]
    # цель 3450; MP1: скидка 10% вне коридора -> 35%, цена до скидки 3450 / 0,65 = 5307,7 -> 5308
    assert (mp1.target, mp1.new_list, mp1.disc, len(mp1.rows)) == (3450, 5308, 35, 2)
    # MP2: скидка 45% в коридоре — остаётся; 3450 / 0,55 = 6272,7 -> 6273
    assert (mp2.target, mp2.new_list, mp2.disc) == (3450, 6273, 45)
    assert round((1 - mp2.target / mp2.new_list) * 100) == 45          # на Ламоде видна целая скидка
    assert mp3.skip.startswith("нет эталона WB")


def test_send_posts_price_and_sale_with_dates_and_skips_failed(db):
    lam = _setup(db)
    m = _mod()
    cards = [c for c in m.build_plan(db, 35, 50) if not c.skip]
    calls = []

    class Fake:
        def _post(self, path, body):
            calls.append((path, body))
            return {"successCount": 1, "errorCount": 1, "errors": [], "fraudValidationResults": [
                {"parentSku": "MP2", "status": "RESTRICTION", "promotions": [{"name": "Осень"}],
                 "invalidValue": 3450}]}

    res = m.send(db, cards, 365, factory=lambda p, c: Fake())
    path, body = calls[0]
    assert path == "/v2/nomenclatures-prices" and body["force"] is False
    item = {p["parentSku"]: p for p in body["prices"]}["MP1"]
    assert item["price"] == {"amount": 530800, "currency": "RUB"}
    assert item["salePrice"] == {"amount": 345000, "currency": "RUB"}
    assert item["saleStart"].endswith(".000Z") and item["saleEnd"] > item["saleStart"]
    assert res["Ламода"].startswith("отправлено карточек: 1 из 2")
    assert "акции «Осень»" in res["Ламода"]
    db.expire_all()
    got = {i.external_id: (i.current_price, i.current_sale_price)
           for i in db.query(PlatformItem).filter(PlatformItem.account_id == lam.id)}
    assert got["MP1:s1"] == got["MP1:s2"] == (5308, 3450)
    assert got["MP2:s1"] == (8000, 4400)                                 # отказ — у нас прежняя


def test_card_at_plan_is_not_sent_and_verify_reports(db, tmp_path):
    lam = _setup(db)
    for it in db.query(PlatformItem).filter(PlatformItem.account_id == lam.id, PlatformItem.barcode.in_(["b1", "b1l"])):
        it.current_price, it.current_sale_price = 5308, 3450
    db.commit()
    env = {**os.environ, "PYTHONIOENCODING": "cp1251"}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lamoda_apply.py"), "--xlsx", str(tmp_path / "l.xlsx")],
                       capture_output=True, env=env, cwd=ROOT)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    text = r.stdout.decode("cp1251")
    assert "к отправке 1; уже по плану 1" in text and "Ничего не отправлено" in text
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lamoda_apply.py"), "--verify"],
                       capture_output=True, env=env, cwd=ROOT)
    assert r.returncode == 1
    assert "Держит план: 1; НЕ держит: 1" in r.stdout.decode("cp1251")

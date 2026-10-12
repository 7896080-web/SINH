"""Возврат скидки WB по копии базы (scripts/wb_restore_discount.py)."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

from openpyxl import load_workbook

from priceapp.database import engine
from priceapp.models import AuditLog, PlatformItem
from tests import factories as F

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("wbrd", ROOT / "scripts" / "wb_restore_discount.py")
wbrd = importlib.util.module_from_spec(spec)
sys.modules["wbrd"] = wbrd
spec.loader.exec_module(wbrd)


def _set(db, prices):
    for it in db.query(PlatformItem):
        it.current_price, it.current_sale_price = prices[it.barcode]
    db.commit()


def _two_dbs(db, tmp_path):
    w = F.account(db, "wb", "ИП 1")
    F.item(db, w, "a1", "100", external_id="111:1")
    F.item(db, w, "a2", "100", external_id="111:2")     # тот же nmID — одна карточка
    F.item(db, w, "b1", "200", external_id="222:1")
    F.item(db, w, "c1", "300", external_id="333:1")
    F.item(db, w, "d1", "400", external_id="444:1")
    _set(db, {"a1": (10000, 5000), "a2": (10000, 5000), "b1": (8000, 4800), "c1": (6000, 3000), "d1": (5000, 2500)})
    engine.dispose()
    before = tmp_path / "before.db"
    shutil.copy(Path(str(engine.url).replace("sqlite:///", "")), before)
    # WB сменил скидку у 111 (50 -> 70) и 222 (40 -> 55); у 333 сменилась и цена; 444 не тронут
    _set(db, {"a1": (10000, 3000), "a2": (10000, 3000), "b1": (8000, 3600), "c1": (7000, 2100), "d1": (5000, 2500)})
    return w, before


def test_plan_restores_only_the_discount_and_skips_cards_whose_price_moved(db, tmp_path):
    _, before = _two_dbs(db, tmp_path)
    after = Path(str(engine.url).replace("sqlite:///", ""))
    restore, moved = wbrd.plan(wbrd.load(before)[0], wbrd.load(after)[0])
    assert [(b.nm, b.discount, a.discount) for b, a in restore] == [("111", 50, 70), ("222", 40, 55)]
    assert [b.nm for b, _ in moved] == ["333"]


def test_report_only_by_default_and_file_per_cabinet(db, tmp_path):
    _, before = _two_dbs(db, tmp_path)
    out = tmp_path / "d.xlsx"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "wb_restore_discount.py"), "--before", str(before),
                        "--xlsx", str(out)], capture_output=True, cwd=ROOT,
                       env={**__import__("os").environ, "PYTHONIOENCODING": "cp1251"})
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    text = r.stdout.decode("cp1251")
    assert "вернуть: 2 карточек" in text and "Ничего не отправлено" in text
    rows = list(load_workbook(out)["ИП 1"].iter_rows(values_only=True))
    got = {x[0]: x for x in rows[1:]}
    assert got[111][5] == 50 and got[222][5] == 40 and got[333][5] is None and 444 not in got


def test_send_puts_only_nm_and_discount(db, tmp_path):
    w, before = _two_dbs(db, tmp_path)
    after = Path(str(engine.url).replace("sqlite:///", ""))
    restore, _ = wbrd.plan(wbrd.load(before)[0], wbrd.load(after)[0])
    calls = []

    class Fake:
        def _post(self, url, body):
            calls.append((url, body))
            return {"data": {"id": 1}, "error": False}

    res = wbrd.send(db, restore, factory=lambda platform, creds: Fake())
    assert res["ИП 1"].startswith("скидка возвращена: 2")
    url, body = calls[0]
    assert url.endswith("/api/v2/upload/task")
    assert body == {"data": [{"nmID": 111, "discount": 50}, {"nmID": 222, "discount": 40}]}
    assert db.query(AuditLog).filter(AuditLog.action == "wb_restore_discount").count() == 1

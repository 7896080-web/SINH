from datetime import datetime
from decimal import Decimal
from pathlib import Path
import xml.etree.ElementTree as ET

from upd_constructor.calc import compute_line, compute_totals
from upd_constructor.lamoda_xlsx import read_supply
from upd_constructor.builder import build_upd

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "upd"
REF1 = next((ROOT / "reference").glob("*_20260729_*.xml"))   # УПД №12100, 551 поз.
REF2 = next((ROOT / "reference").glob("*_20260701_*.xml"))   # УПД №1270, 282 поз.
XLSX = next((ROOT / "lamoda").glob("*.xlsx"))                # поставка 12100 от 31.07.2026
JS_12100 = ROOT / "generated/js_12100_before_address_fix.xml"


def parse(path_or_bytes):
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    return ET.fromstring(data)


def build_from_xlsx(totals_mode="rows", price_mode="gross", now=None):
    sup = read_supply(XLSX)
    rate = Decimal(5)
    lines = [compute_line(r.name, r.gtin, r.kiz, r.qty, r.price, rate, price_mode) for r in sup.rows]
    totals = compute_totals(lines, rate, totals_mode, price_mode)
    return build_upd(lines, totals, doc_number="12100", doc_date="29.07.2026", ttn_number="12100",
                     ttn_date="31.07.2026", transfer_date="31.07.2026",
                     now=now or datetime(2026, 7, 29, 13, 48, 47))


def shape(root):
    """Множество (путь, имена атрибутов) — структура документа без значений."""
    out = set()
    def walk(el, path):
        p = f"{path}/{el.tag}"
        out.add((p, tuple(sorted(el.attrib))))
        for ch in el:
            walk(ch, p)
    walk(root, "")
    return out

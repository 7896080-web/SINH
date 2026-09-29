"""pytest tests/ — «КОМИССИЯ»/«АГЕНТ» по дате поставки; вёрстка не меняется."""
import tempfile
from pathlib import Path
import openpyxl

from lamoda_stickers import gen_stickers as G


def _texts(date, n=3, scheme=None):
    out = Path(tempfile.mkdtemp()) / "s.xlsx"
    G.build(n, "12600", date, out, scheme)
    ws = openpyxl.load_workbook(out).active
    return [c.value for r in ws.iter_rows() for c in r if isinstance(c.value, str)]


def test_scheme_by_date():
    assert G.scheme_for("30.09.2026") == "commission"
    assert G.scheme_for("01.10.2026") == "agency"


def test_commission_until_30_09():
    t = _texts("30.09.2026")
    assert t.count("КОМИССИЯ") == 3 and t.count('Получатель: ООО "КУПИШУЗ" Комиссия') == 3


def test_agency_from_01_10_no_komis():
    t = _texts("01.10.2026")
    assert t.count("АГЕНТ") == 3 and t.count('Получатель: ООО "КУПИШУЗ" Агент') == 3
    assert not [x for x in t if "комис" in x.lower()]


def test_layout_identical_except_words():
    """Кроме двух строк, файл с «АГЕНТ» совпадает с файлом «КОМИССИЯ» ячейка в ячейку."""
    a, c = _texts("01.10.2026", 5), _texts("01.10.2026", 5, "commission")
    swap = {"АГЕНТ": "КОМИССИЯ", 'Получатель: ООО "КУПИШУЗ" Агент': 'Получатель: ООО "КУПИШУЗ" Комиссия'}
    assert [swap.get(x, x) for x in a] == c

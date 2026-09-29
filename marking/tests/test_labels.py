"""Этикетки на настоящих кодах поставки 12550."""
import pytest

from conftest import FIXTURES, fixture_bytes
from markapp import gtin as G
from markapp import labels as L
from markapp import settings
from markapp.upd_service import read_fbo

CODES_TEXT = (FIXTURES / "codes_12550.txt").read_bytes().decode("utf-8")


def _codes():
    codes, errors = L.parse_codes(CODES_TEXT)
    assert not errors
    return codes


def test_all_338_codes_parse_despite_gs_inside():
    """splitlines() резал бы каждый код на три куска по GS."""
    codes = _codes()
    assert len(codes) == 338
    assert all(len(c) == 85 and c.count(L.GS) == 2 for c in codes)


def test_xlsx_style_gs_is_accepted_and_short_code_is_refused():
    code = _codes()[0]
    codes, errors = L.parse_codes(code.replace(L.GS, "_x001D_") + "\n" + code[:31])
    assert codes == [code]
    assert errors and "короткий код" in errors[0]


def test_duplicate_code_is_refused():
    code = _codes()[0]
    _, errors = L.parse_codes(code + "\r\n" + code)
    assert errors and "повторяется" in errors[0]


def test_every_code_of_12550_encodes_and_reads_back():
    for code in _codes():
        m = L.matrix(code)
        assert len(m) == 36          # 36×36 модулей для кодов одежды
        L.verify(code, m)


def test_verify_catches_a_wrong_symbol():
    a, b = _codes()[:2]
    with pytest.raises(L.LabelError):
        L.verify(a, L.matrix(b))


def test_title_is_supplier_sku_with_color_and_size(db):
    fbo = read_fbo(fixture_bytes("lamoda_postavki_fbo_12550.xlsx"))
    G.import_fbo_pairs(db, fbo.rows, "fbo")
    db.commit()
    codes = _codes()
    data = L.label_values(db, codes, settings.lamoda_org(db), "29.09.2026", "12550")
    assert not any(d.warnings for d in data)
    titles = {d.values["артикул"] for d in data}
    assert "3030 KAHVE 4XL АВЕР Рубашка Д/р сатин" in titles
    assert data[0].values["изготовитель"] == "ИП Яворская Т.Н."
    assert data[-1].values["номер"] == "338" and data[0].values["всего"] == "338"


def test_unmapped_gtin_is_warned_before_printing(db):
    data = L.label_values(db, _codes()[:1], settings.lamoda_org(db), "29.09.2026")
    assert data[0].warnings and "не сопоставлен" in data[0].warnings[0]
    assert data[0].values["артикул"].startswith("GTIN ")


def test_template_lines():
    vals = {"артикул": "A", "цвет": "", "размер": "L", "дата": "29.09.2026"}
    assert L.render_lines("{артикул}\nЦвет: {цвет}\nРазмер: {размер}\nСостав: хлопок", vals) == \
        ["A", "Размер: L", "Состав: хлопок"]          # строка с пустым цветом не печатается
    assert L.check_template("{артикул} {опечатка}") == ["неизвестная подстановка {опечатка}"]
    assert L.check_template(settings.DEFAULTS[settings.LABEL_BOTTOM]) == []


def test_pdf_pages_are_58x40_and_codes_read_from_the_pdf(db):
    pdfium = pytest.importorskip("pypdfium2")
    zxingcpp = pytest.importorskip("zxingcpp")
    fbo = read_fbo(fixture_bytes("lamoda_postavki_fbo_12550.xlsx"))
    G.import_fbo_pairs(db, fbo.rows, "fbo")
    db.commit()
    codes = _codes()[:3]
    pdf, warnings = L.build_pdf(db, L.label_values(db, codes, settings.lamoda_org(db), "29.09.2026"))
    assert warnings == []
    doc = pdfium.PdfDocument(pdf)
    assert len(doc) == 3
    for i, code in enumerate(codes):
        page = doc[i]
        w, h = page.get_size()
        assert (round(w / 72 * 25.4), round(h / 72 * 25.4)) == (58, 40)
        text = page.get_textpage().get_text_range()
        assert "GTIN " + code[2:16] in text
        img = page.render(scale=600 / 72).to_pil()
        found = zxingcpp.read_barcodes(img, text_mode=zxingcpp.TextMode.Plain)
        assert found and found[0].text == code and found[0].symbology_identifier == "]d2"

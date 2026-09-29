import re
from decimal import Decimal
from pathlib import Path
from upd_helpers import *
from upd_constructor.check import check_file
from upd_constructor.calc import compute_line
from upd_constructor.lamoda_xlsx import read_supply
from upd_constructor.calc import compute_totals
from upd_constructor.builder import build_upd
from upd_constructor import config as C
from datetime import datetime


def _lines(root):
    return root.findall("Документ/ТаблСчФакт/СведТов")


def test_xlsx_reader():
    s = read_supply(XLSX)
    assert (s.number, s.date) == ("12100", "31.07.2026")
    assert len(s.rows) == 551 and not s.problems
    assert s.rows[0].name.startswith("2403(7302)BIEGE 3XL") and s.rows[0].gtin == "2000932076553"


def test_structure_matches_reference():
    _, data = build_from_xlsx()
    assert shape(parse(data)) == shape(parse(REF1))


def test_every_line_matches_reference():
    """Все 551 строки: наименование, ГТИН, КИЗ и все денежные значения — как в принятом эталоне."""
    mine, ref = _lines(parse(build_from_xlsx()[1])), _lines(parse(REF1))
    assert len(mine) == len(ref) == 551
    for i, (m, r) in enumerate(zip(mine, ref), 1):
        assert m.attrib == r.attrib, f"строка {i}"
        assert m.findtext("ДопСведТов/НомСредИдентТов/КИЗ") == r.findtext("ДопСведТов/НомСредИдентТов/КИЗ")
        assert m.find("ДопСведТов").get("КодТов") == r.find("ДопСведТов").get("КодТов")
        assert m.findtext("СумНал/СумНал") == r.findtext("СумНал/СумНал")


def test_formula_on_second_reference():
    """Второй эталон (нет xlsx): пересчёт каждой строки из СтТовУчНал даёт те же ЦенаТов/СтТовБезНДС/СумНал."""
    for it in _lines(parse(REF2)):
        l = compute_line("", "", "", it.get("КолТов"), it.get("СтТовУчНал"), Decimal(5), "gross")
        assert f"{l.unit_bez:.2f}" == it.get("ЦенаТов") and f"{l.bez:.2f}" == it.get("СтТовБезНДС")
        assert f"{l.nal:.2f}" == it.findtext("СумНал/СумНал")


def test_totals_reference_mode_equals_reference():
    ref = parse(REF1).find("Документ/ТаблСчФакт/ВсегоОпл")
    mine = parse(build_from_xlsx(totals_mode="reference")[1]).find("Документ/ТаблСчФакт/ВсегоОпл")
    assert mine.attrib == ref.attrib
    assert mine.findtext("СумНалВсего/СумНал") == ref.findtext("СумНалВсего/СумНал")


def test_totals_rows_mode_header_equals_row_sum():
    doc = parse(build_from_xlsx(totals_mode="rows")[1]).find("Документ/ТаблСчФакт")
    assert sum(Decimal(i.get("СтТовБезНДС")) for i in doc.findall("СведТов")) == Decimal(doc.find("ВсегоОпл").get("СтТовБезНДСВсего"))
    assert doc.find("ВсегоОпл").get("СтТовУчНалВсего") == "8029500.00"


def test_buyer_blocks_match_reference():
    mine, ref = parse(build_from_xlsx()[1]), parse(REF1)
    for tag in ("ГрузПолуч", "СвПокуп"):
        assert [dict(e.attrib) for e in mine.iter() if e in list(mine.find("Документ/СвСчФакт/" + tag).iter())] == \
               [dict(e.attrib) for e in ref.iter() if e in list(ref.find("Документ/СвСчФакт/" + tag).iter())]
    g = mine.find("Документ/СвСчФакт/ГрузПолуч/Адрес/АдрРФ").attrib
    p = mine.find("Документ/СвСчФакт/СвПокуп/Адрес/АдрРФ").attrib
    assert g["Индекс"] == "140150" and p["Индекс"] == "121614" and g != p


def test_encoding_crlf_no_trailing_newline():
    data = build_from_xlsx()[1]
    assert data.startswith(b'<?xml version="1.0" encoding="windows-1251"?>\r\n')
    text = data.decode("cp1251")
    assert "Общество с ограниченной ответственностью &quot;Купишуз&quot;" in text
    assert "\r\n" in text and not text.endswith("\n") and text.endswith("</Файл>")


def test_id_file_format_like_reference():
    idf, _ = build_from_xlsx()
    ref_id = parse(REF1).get("ИдФайл")
    pat = r"^ON_NSCHFDOPPR_(.+)_(.+)_(\d{8})_[0-9a-f\-]{36}_0_1_0_0_0_00$"
    assert re.match(pat, idf) and re.match(pat, ref_id)
    assert idf.split("_")[2:4] == ref_id.split("_")[2:4] and idf.split("_")[4] == "20260729"


def test_header_time_is_real_not_zero():
    assert parse(build_from_xlsx()[1]).find("Документ").get("ВремИнфПр") == "13.48.47"


def test_parity_with_web_version_rows():
    """Python и веб-версия (после фикса округления) дают одинаковые строки для 12100."""
    mine, js = _lines(parse(build_from_xlsx()[1])), _lines(parse(JS_12100))
    assert [m.attrib for m in mine] == [j.attrib for j in js]


def test_check_ok_on_generated(tmp_path=None):
    import tempfile
    d = Path(tempfile.mkdtemp())
    p = d / "u.xml"; p.write_bytes(build_from_xlsx()[1])
    assert not [f for f in check_file(p) if f.level == "ERROR"]


def test_check_detects_wrong_buyer_address():
    errs = [f for f in check_file(JS_12100) if f.level == "ERROR"]
    assert any("СвПокуп" in f.msg for f in errs)


def test_check_accepts_both_references():
    for r in (REF1, REF2):
        assert not [f for f in check_file(r) if f.level == "ERROR"]


# --- Агентская модель Lamoda с 01.10.2026 -----------------------------------------------------------------

def _build_on(doc_date, **kw):
    sup = read_supply(XLSX)
    rate = Decimal(5)
    lines = [compute_line(r.name, r.gtin, r.kiz, r.qty, r.price, rate, "gross") for r in sup.rows]
    totals = compute_totals(lines, rate, "rows", "gross")
    return build_upd(lines, totals, doc_number="12100", doc_date=doc_date, ttn_number="12100",
                     ttn_date=doc_date, transfer_date=doc_date, now=datetime(2026, 10, 1, 10, 0, 0),
                     id_file="ON_NSCHFDOPPR_X_Y_20261001_00000000-0000-0000-0000-000000000000_0_1_0_0_0_00", **kw)[1]


def _sp(data):
    return parse(data).find("Документ/СвПродПер/СвПер")


def test_scheme_by_doc_date():
    assert C.scheme_for("30.09.2026") is C.COMMISSION
    assert C.scheme_for("01.10.2026") is C.AGENCY
    assert C.scheme_for("29.07.2026") is C.COMMISSION


def test_commission_until_30_09_unchanged():
    sp = _sp(_build_on("30.09.2026"))
    assert sp.get("ВидОпер") == "ПродажаКомиссия" and sp.find("ОснПер").get("РеквНаимДок") == "Договор комиссии"


def test_agency_from_01_10_no_komis_anywhere():
    data = _build_on("01.10.2026")
    sp = _sp(data)
    assert sp.get("ВидОпер") == "Реализация по агентскому договору"
    assert sp.find("ОснПер").get("РеквНаимДок") == "Агентский договор"
    assert sp.find("ОснПер").get("РеквНомерДок") == "б/н"
    assert "комис" not in data.decode("cp1251").lower()
    assert parse(data).find("Документ/СвСчФакт/ДопСвФХЖ1").get("СпОбстФДОП") == "00005"


def test_agency_differs_from_commission_only_in_two_attributes():
    """Кроме ВидОпер и РеквНаимДок — байт-в-байт тот же документ (даты одинаковые, чтобы сравнивать)."""
    agency = _build_on("01.10.2026").decode("cp1251")
    comm = _build_on("01.10.2026", scheme=C.COMMISSION).decode("cp1251")
    assert agency != comm
    assert agency.replace('ВидОпер="Реализация по агентскому договору"', 'ВидОпер="ПродажаКомиссия"') \
                 .replace('РеквНаимДок="Агентский договор"', 'РеквНаимДок="Договор комиссии"') == comm


def test_check_rejects_komis_in_agency_period():
    import tempfile
    d = Path(tempfile.mkdtemp())
    bad = d / "bad.xml"; bad.write_bytes(_build_on("01.10.2026", scheme=C.COMMISSION))
    errs = [f.msg for f in check_file(bad) if f.level == "ERROR"]
    assert any("ВидОпер" in m for m in errs) and any("РеквНаимДок" in m for m in errs)
    good = d / "good.xml"; good.write_bytes(_build_on("01.10.2026"))
    assert not [f for f in check_file(good) if f.level == "ERROR"]


def test_cli_auto_scheme(tmp_path=None):
    import tempfile
    from upd_constructor.cli import main
    d = Path(tempfile.mkdtemp())
    assert main(["build", str(XLSX), "-o", str(d), "--doc-date", "01.10.2026"]) == 0
    assert [f.name for f in d.glob("*.xml")] == ["12100.xml"]   # имя файла = номер документа
    text = next(d.glob("*.xml")).read_bytes().decode("cp1251")
    assert 'ВидОпер="Реализация по агентскому договору"' in text and "комис" not in text.lower()


def test_manual_switch_commission_after_01_10_is_warning_not_error():
    """Ручное переключение: комиссия в УПД от 01.10 — предупреждение, а не ошибка (переключатель должен работать)."""
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "c.xml"; f.write_bytes(_build_on("01.10.2026", scheme=C.COMMISSION))
    res = check_file(f, expected_scheme=C.COMMISSION)
    assert not [x for x in res if x.level == "ERROR"]
    assert any(x.level == "WARN" and "выбрана" in x.msg for x in res)


def test_manual_switch_agency_before_01_10():
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "a.xml"; f.write_bytes(_build_on("30.09.2026", scheme=C.AGENCY))
    res = check_file(f, expected_scheme=C.AGENCY)
    assert not [x for x in res if x.level == "ERROR"] and any(x.level == "WARN" for x in res)
    assert _sp(f.read_bytes()).get("ВидОпер") == "Реализация по агентскому договору"


def test_check_catches_mismatch_with_chosen_scheme():
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "x.xml"; f.write_bytes(_build_on("01.10.2026", scheme=C.COMMISSION))
    assert any(x.level == "ERROR" for x in check_file(f, expected_scheme=C.AGENCY))


def test_cli_manual_scheme():
    import tempfile
    from upd_constructor.cli import main
    d = Path(tempfile.mkdtemp())
    assert main(["build", str(XLSX), "-o", str(d), "--doc-date", "01.10.2026", "--scheme", "commission"]) == 0
    assert 'ВидОпер="ПродажаКомиссия"' in (d / "12100.xml").read_bytes().decode("cp1251")

import pytest

from conftest import fixture_bytes
from markapp import codes_txt
from markapp.labels import GS, parse_codes


def test_supply_codes_file_matches_suz_format_byte_for_byte():
    original = fixture_bytes("codes_12550.txt")
    codes, errors = parse_codes(original.decode("ascii"))
    assert not errors and len(codes) == 338
    assert codes_txt.build(codes) == original


def test_xlsx_style_gs_becomes_real_gs():
    code = parse_codes(fixture_bytes("codes_12550.txt").decode("ascii"))[0][0]
    out = codes_txt.build([code.replace(GS, "_x001D_")])
    assert out == code.encode("ascii") + b"\r\n" and b"_x001D_" not in out


def test_short_or_repeated_code_is_refused():
    code = parse_codes(fixture_bytes("codes_12550.txt").decode("ascii"))[0][0]
    with pytest.raises(codes_txt.CodesTxtError):
        codes_txt.build([code[:31]])
    with pytest.raises(codes_txt.CodesTxtError):
        codes_txt.build([code, code])


def test_filename():
    assert codes_txt.filename("12560") == "12560_коды.txt"

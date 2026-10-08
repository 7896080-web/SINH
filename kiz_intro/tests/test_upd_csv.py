"""CSV кодов для УПД (Диадок) — по шаблону загрузки кодов маркировки.

Шаблон: `номер,наименование,цена,количество,ОКЕИ,НДС,КИЗ,код,код,…`, UTF-8 без
BOM, LF; код с запятой — в кавычках. Цена и НДС пустые (решение 08.10.2026).
"""
import csv
import io

import pytest

from kizapp import service as S
from kizapp.crypto import encrypt
from kizapp.models import Batch, Card, Code, Org
from test_kizapp import GTIN, org

GTIN_2 = "04630688315989"


def _batch(db, o, codes, status="INTRODUCED"):
    b = Batch(org_id=o.id, title="партия")
    db.add(b)
    db.flush()
    for gtin, cis in codes:
        db.add(Code(batch_id=b.id, cis=cis, full_enc=encrypt(cis + "\x1d91EE10\x1d92" + "A" * 44),
                    gtin=gtin, status=status))
    db.commit()
    return b


def _cis(n, gtin=GTIN, serial=None):
    return f"01{gtin}21{serial or f'{n:013d}'}"


def test_one_line_per_gtin_with_every_code_in_its_own_column(db):
    o = org(db)
    db.add(Card(org_id=o.id, gtin=GTIN, name="27643 TABA L базовый свитшот"))
    b = _batch(db, o, [(GTIN, _cis(1)), (GTIN_2, _cis(3, GTIN_2)), (GTIN, _cis(2))])
    data = S.upd_csv(db, b)
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data and not data.endswith(b"\n")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    assert rows == [
        ["1", "27643 TABA L базовый свитшот", "", "2", "796", "", "КИЗ", _cis(1), _cis(2)],
        ["2", "", "", "1", "796", "", "КИЗ", _cis(3, GTIN_2)],
    ]


def test_codes_are_short_ki_without_crypto_tail(db):
    o = org(db)
    b = _batch(db, o, [(GTIN, _cis(1))])
    data = S.upd_csv(db, b).decode("utf-8")
    assert _cis(1) in data and "\x1d" not in data and "91EE10" not in data


def test_code_with_comma_or_quote_is_quoted_like_the_template(db):
    """В шаблоне: "0104655555555555215KK,kKKkKkKKK" — серийный номер может
    содержать запятую и кавычку."""
    o = org(db)
    tricky = _cis(0, serial='KK,kKK"kKkKK')
    b = _batch(db, o, [(GTIN, tricky)])
    line = S.upd_csv(db, b).decode("utf-8")
    assert line.endswith('"0104630688315972' + '21KK,kKK""kKkKK"')
    assert list(csv.reader(io.StringIO(line)))[0][7] == tricky


def test_refused_until_every_code_is_introduced(db):
    o = org(db)
    b = _batch(db, o, [(GTIN, _cis(1))], status="APPLIED")
    with pytest.raises(S.KizError, match="не все коды партии в обороте"):
        S.upd_csv(db, b)


def test_name_comes_from_this_ip_card_only(db):
    a = org(db)
    other = Org(name="ИП Ребрик", inn="910223073099")
    db.add(other)
    db.flush()
    db.add(Card(org_id=other.id, gtin=GTIN, name="чужая карточка"))
    b = _batch(db, a, [(GTIN, _cis(1))])
    assert "чужая" not in S.upd_csv(db, b).decode("utf-8")


def test_button_downloads_the_file(client, db):
    o = org(db)
    b = _batch(db, o, [(GTIN, _cis(1))])
    r = client.post(f"/batch/{b.id}/upd-csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.content.decode("utf-8").startswith("1,,,1,796,,КИЗ," + _cis(1))
    page = client.get(f"/batch/{b.id}").text
    assert "Коды для УПД (.csv)" in page

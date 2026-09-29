import io
import sqlite3
from datetime import date, datetime
from decimal import Decimal

import openpyxl
import pytest

from conftest import fixture_bytes
from markapp import backup, settings, stickers
from markapp import supplies as S
from markapp import upd_service as U
from markapp.catalog import import_catalog

FBO = "lamoda_postavki_fbo_12550.xlsx"


@pytest.fixture
def supply12550(db):
    import_catalog(db, fixture_bytes("lamoda_catalog_full_2026-09-28.xlsx"))
    parsed = S.parse_input(fixture_bytes("lamoda_shipment_input_typical.xlsx"))
    s = S.create_supply(db, parsed, organization_id=settings.lamoda_org(db).id, number="12550",
                        doc_number="12550", supply_date=date(2026, 10, 2), filename="in.xlsx", username="t")
    s.status = "moved"
    db.commit()
    return s


def test_fbo_12550_matches_supply(db, supply12550):
    res = U.check_fbo_against_supply(fixture_bytes(FBO), supply12550)
    assert res.ok, res.problems
    assert (res.rows, res.units, res.total) == (338, 338, Decimal("4337500.00"))


def test_fbo_of_other_supply_is_refused(db, supply12550):
    supply12550.number = "12560"
    res = U.check_fbo_against_supply(fixture_bytes(FBO), supply12550)
    assert not res.ok and any("12550" in p for p in res.problems)


def test_fbo_with_changed_quantity_is_refused(db, supply12550):
    supply12550.rows[0].qty += 1
    res = U.check_fbo_against_supply(fixture_bytes(FBO), supply12550)
    assert not res.ok and any("расходится" in p for p in res.problems)


@pytest.mark.parametrize("doc_date, scheme", [(date(2026, 9, 30), "commission"),
                                             (date(2026, 10, 1), "agency")])
def test_upd_scheme_by_upd_date(db, supply12550, doc_date, scheme):
    built = U.build_for_supply(db, supply12550, fixture_bytes(FBO), doc_date)
    assert built.scheme == scheme
    assert built.positions == 338
    assert built.total_with_vat == Decimal("4337500.00")
    assert not U.has_errors(built.findings), U.findings_text(built.findings)
    text = built.xml.decode("cp1251")
    assert 'НомерДок="12550"' in text
    if scheme == "agency":
        assert "комис" not in text.lower()
        assert 'ВидОпер="Реализация по агентскому договору"' in text
    else:
        assert 'ВидОпер="ПродажаКомиссия"' in text


def test_upd_seller_comes_from_organization(db, supply12550):
    org = supply12550.organization
    built = U.build_for_supply(db, supply12550, fixture_bytes(FBO), date(2026, 10, 1))
    text = built.xml.decode("cp1251")
    assert f'ИННФЛ="{org.inn}"' in text and org.edo_sender_id in built.id_file


def test_manual_scheme_is_kept_but_named(db, supply12550):
    supply12550.scheme_choice = "commission"
    built = U.build_for_supply(db, supply12550, fixture_bytes(FBO), date(2026, 10, 1))
    assert built.scheme == "commission"
    assert "вручную" in built.scheme_warning


def test_agency_date_is_a_setting(db, supply12550):
    settings.put(db, settings.AGENCY_FROM, "01.11.2026")
    db.commit()
    assert U.scheme_by_date(db, date(2026, 10, 15)) == "commission"


def _sticker_texts(data):
    ws = openpyxl.load_workbook(io.BytesIO(data)).active
    return [c.value for r in ws.iter_rows() for c in r if isinstance(c.value, str)]


def test_stickers_follow_planned_upd_date(db, supply12550):
    supply12550.planned_upd_date = date(2026, 9, 30)
    data, scheme = stickers.build(db, supply12550, 3)
    assert scheme == "commission"
    t = _sticker_texts(data)
    assert t.count("КОМИССИЯ") == 3
    assert t.count("Отправитель: ИП Яворская Т.Н") == 3
    supply12550.planned_upd_date = date(2026, 10, 2)
    data, scheme = stickers.build(db, supply12550, 3)
    assert scheme == "agency" and not [x for x in _sticker_texts(data) if "комис" in x.lower()]


def test_stickers_filename():
    class _S:
        number = "12560"
    assert stickers.filename(_S(), 27) == "Маркировка_короба_Лемода_12560_27шт.xlsx"


# --- резервная копия --------------------------------------------------------------

def test_backup_is_one_verified_file(db, tmp_path):
    db.execute(__import__("sqlalchemy").text("SELECT 1"))
    res = backup.make_backup(directory=tmp_path)
    assert res.checked and not res.error, res.error
    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == 1 and files[0].startswith("marking-") and files[0].endswith(".db")
    con = sqlite3.connect(tmp_path / files[0])
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


def test_backup_keeps_everything_younger_than_a_day():
    now = datetime(2026, 9, 29, 12, 0, 0)
    names = [f"marking-20260929-{h:02d}0000.db" for h in range(0, 12)]
    assert backup.names_to_drop(names, now=now) == []


def test_backup_keeps_one_per_day_after_that_and_never_foreign_names():
    now = datetime(2026, 12, 31, 12, 0, 0)
    names = [f"marking-202609{d:02d}-{h:02d}0000.db" for d in range(1, 29) for h in (1, 13)]
    names.append("marking-руками.db")
    drop = backup.names_to_drop(names, now=now)
    assert "marking-руками.db" not in drop
    kept = set(names) - set(drop) - {"marking-руками.db"}
    assert len(kept) <= backup.KEEP_DAILY + backup.KEEP_WEEKLY


def test_remote_off_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "RCLONE_REMOTE", "")
    f = tmp_path / "marking-20260929-120000.db"
    f.write_bytes(b"x")
    assert backup.upload_to_remote(f) == ""


def test_remote_upload_is_checked_by_listing(tmp_path, monkeypatch):
    f = tmp_path / "marking-20260929-120000.db"
    f.write_bytes(b"12345")
    monkeypatch.setattr(backup, "RCLONE_REMOTE", "yandex:marking_backups")
    calls = []

    def fake(args):
        calls.append(args[0])
        if args[0] == "lsjson":
            return True, '[{"Name": "marking-20260929-120000.db", "Size": 4}]'
        return True, ""
    monkeypatch.setattr(backup, "_run_rclone", fake)
    assert "размер не сошёлся" in backup.upload_to_remote(f)
    assert calls[:2] == ["copyto", "lsjson"]

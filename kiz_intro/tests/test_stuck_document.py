"""Документ ввода, застрявший в «проверяется ЧЗ» (08.10.2026: 200 кодов)."""
from datetime import timedelta

import pytest

from kizapp import chz, service as S
from kizapp.crypto import encrypt
from kizapp.models import Batch, Code, Doc, now_utc
from test_kizapp import GTIN, chz_fake, logged, org  # noqa: F401 (фикстура)


def _sent(db, n=3, sent_ago=timedelta(minutes=5)):
    o = org(db)
    logged(db, o)
    b = Batch(org_id=o.id, title="партия")
    db.add(b)
    db.flush()
    d = Doc(batch_id=b.id, document="x", codes_count=n, status="sent", chz_doc_id="e3ec4dee-34b7",
            sent_at=now_utc() - sent_ago)
    db.add(d)
    db.flush()
    for i in range(n):
        db.add(Code(batch_id=b.id, cis=f"01{GTIN}21{i:013d}", full_enc=encrypt("x"), gtin=GTIN,
                    status="APPLIED", doc_id=d.id))
    db.commit()
    return b, d


def test_sent_document_closes_when_all_its_codes_are_introduced(db, chz_fake, monkeypatch):
    monkeypatch.setattr(chz, "document_status", lambda d, t: ("IN_PROGRESS", ""))
    b, d = _sent(db)
    chz_fake["cises"] = {c.cis: "INTRODUCED" for c in db.query(Code)}
    S.refresh_statuses(db, b)
    assert d.status == "CHECKED_OK"


def test_unknown_document_status_is_shown_not_swallowed(db, chz_fake, monkeypatch):
    monkeypatch.setattr(chz, "document_status", lambda d, t: ("WAIT_FOR_CONTINUATION", ""))
    b, d = _sent(db)
    note = S.refresh_statuses(db, b)
    assert d.status == "sent" and "WAIT_FOR_CONTINUATION" in note and "WAIT_FOR_CONTINUATION" in d.error


def test_status_error_is_shown_and_other_docs_still_checked(db, chz_fake, monkeypatch):
    def boom(d, t):
        raise chz.ChzError("статус документа: HTTP 404: not found", 404)
    monkeypatch.setattr(chz, "document_status", boom)
    b, d = _sent(db)
    assert "HTTP 404" in S.refresh_statuses(db, b)


def test_a_day_without_result_hands_the_document_to_a_human(db, chz_fake, monkeypatch):
    monkeypatch.setattr(chz, "document_status", lambda d, t: ("IN_PROGRESS", ""))
    b, d = _sent(db, sent_ago=timedelta(days=2))
    chz_fake["cises"] = {c.cis: "APPLIED" for c in db.query(Code)}
    S.refresh_statuses(db, b)
    assert d.status == "unknown" and "проверьте документ в ЛК ЧЗ" in d.error
    S.release_unknown(db, d)                       # статусы кодов свежие, ни один не в обороте
    assert d.status == "error" and S.ready_codes(db, b)


def test_release_refused_with_stale_statuses_or_introduced_codes(db, chz_fake):
    b, d = _sent(db)
    d.status = "unknown"
    with pytest.raises(S.KizError, match="Проверить статусы"):
        S.release_unknown(db, d)
    c = db.query(Code).first()
    c.status, c.status_at = "INTRODUCED", now_utc()
    with pytest.raises(S.KizError, match="уже в обороте"):
        S.release_unknown(db, d)

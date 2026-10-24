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


# --- Удаление партии, загруженной по ошибке (24.10.2026: коды другого ИП) -------------------

def _loaded(db, n=3, status=""):
    from kizapp.models import Org
    o = db.query(Org).first() or org(db)
    b = Batch(org_id=o.id, title="джинсы.pdf")
    db.add(b)
    db.flush()
    for i in range(n):
        db.add(Code(batch_id=b.id, cis=f"01{GTIN}21{i:013d}", full_enc=encrypt("x"), gtin=GTIN,
                    status=status))
    db.commit()
    return b


def test_batch_loaded_by_mistake_is_deleted_with_its_codes(db):
    b = _loaded(db)
    d = Doc(batch_id=b.id, document="x", codes_count=3, status="CHECKED_NOT_OK")   # ЧЗ не принял
    db.add(d)
    db.commit()
    assert S.delete_refusal(db, b) == ""
    assert S.delete_batch(db, b) == 3
    db.commit()
    assert db.query(Batch).count() == 0 and db.query(Code).count() == 0 and db.query(Doc).count() == 0


def test_codes_can_be_loaded_again_after_deletion(db):
    """Удаление и есть способ перенести коды к нужному ИП: cis уникален."""
    b = _loaded(db)
    S.delete_batch(db, b)
    db.commit()
    again = _loaded(db)                    # те же коды — повторная загрузка не упирается в уникальность
    assert db.query(Code).filter(Code.batch_id == again.id).count() == 3


@pytest.mark.parametrize("status", ["sending", "unknown", "sent", "CHECKED_OK"])
def test_batch_with_a_document_that_reached_chz_is_not_deleted(db, status):
    b = _loaded(db)
    db.add(Doc(batch_id=b.id, document="x", codes_count=3, status=status))
    db.commit()
    assert "документ ввода" in S.delete_refusal(db, b)
    with pytest.raises(S.KizError, match="удалить нельзя"):
        S.delete_batch(db, b)
    assert db.query(Code).count() == 3


def test_batch_with_introduced_codes_is_not_deleted(db):
    b = _loaded(db, status="INTRODUCED")
    assert "в обороте" in S.delete_refusal(db, b)


def test_delete_button_and_refusal_on_pages(client, db):
    b = _loaded(db)
    assert f'action="/batch/{b.id}/delete"' in client.get("/").text
    r = client.post(f"/batch/{b.id}/delete")
    assert r.status_code == 200 and "удалена, кодов 3" in r.text
    db.expire_all()
    b2 = _loaded(db, status="INTRODUCED")
    page = client.get("/").text
    assert f'action="/batch/{b2.id}/delete"' not in page
    r = client.post(f"/batch/{b2.id}/delete")
    assert "удалить нельзя" in r.text and db.query(Code).count() == 3

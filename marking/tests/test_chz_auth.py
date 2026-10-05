"""Вход в ЧЗ сертификатом: токены, их срок, сверка ИНН, страница входа."""
import base64
import json
import time
from datetime import timedelta

from markapp import chz_auth, settings
from markapp.models import NkCard, Organization
from markapp.timeutils import now_utc


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


def _jwt(exp):
    part = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{part}.s"


def test_expiry_is_read_from_the_token():
    got = chz_auth.expires_at(_jwt(int(time.time()) + 3 * 3600))
    assert abs((got - (now_utc() + timedelta(hours=3))).total_seconds()) < 5
    # Не JWT — 10 часов от сейчас.
    fallback = chz_auth.expires_at("opaque")
    assert timedelta(hours=9, minutes=59) < fallback - now_utc() <= timedelta(hours=10)


def test_token_is_stored_encrypted_and_expires(db):
    org = settings.lamoda_org(db)
    chz_auth.store(db, org, "true_api", "opaque-token")
    assert "opaque-token" not in org.chz_token_enc
    assert chz_auth.token(org) == "opaque-token"
    org.chz_token_until = now_utc() + timedelta(minutes=2)      # меньше запаса — уже «нужен вход»
    assert chz_auth.token(org) is None
    assert chz_auth.token(org, "suz") is None


def test_login_requeues_cards_that_failed_without_token(db):
    db.add_all([NkCard(gtin="04620180403734", status="error", error="HTTP 401: ключ не принят"),
                NkCard(gtin="04630688318072", status="error", error="HTTP 500: сбой")])
    db.commit()
    chz_auth.store(db, settings.lamoda_org(db), "true_api", "t")
    db.commit()
    assert db.get(NkCard, "04620180403734").status == "pending"
    assert db.get(NkCard, "04630688318072").status == "error"


def test_sign_in_strips_line_breaks_and_uses_connection_path(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, proxies=None, timeout=None, allow_redirects=True):
        assert allow_redirects is False          # заголовки входа не уходят на чужой хост
        seen.update(url=url, body=json)
        return _Resp(200, {"token": "T"})
    monkeypatch.setattr(chz_auth.requests, "post", fake_post)
    assert chz_auth.sign_in("u-1", "AAA\r\nBBB\n", "conn-9") == "T"
    assert seen["url"].endswith("/auth/simpleSignIn/conn-9")
    assert seen["body"] == {"uuid": "u-1", "data": "AAABBB"}
    chz_auth.sign_in("u-2", "X")
    assert seen["url"].endswith("/auth/simpleSignIn")


def test_rejected_signature_is_named(monkeypatch):
    monkeypatch.setattr(chz_auth.requests, "post",
                        lambda *a, **k: _Resp(401, {"error_message": "Подпись невалидна"}))
    try:
        chz_auth.sign_in("u", "sig")
    except chz_auth.ChzAuthError as e:
        assert "Подпись невалидна" in str(e)
    else:
        raise AssertionError("ожидалась ошибка")


def test_inn_comparison_tolerates_leading_zeros():
    assert chz_auth.inn_matches("910223073620", "910223073620")
    assert chz_auth.inn_matches("7707083893", "007707083893")
    assert not chz_auth.inn_matches("910223073620", "910223073099")
    assert chz_auth.signer_inn("не подпись") is None


def test_login_endpoints(client, db, monkeypatch):
    org = settings.lamoda_org(db)
    monkeypatch.setattr(chz_auth, "challenge", lambda: {"uuid": "u", "data": "d"})
    monkeypatch.setattr(chz_auth, "sign_in", lambda uuid, sig, conn=None: "TOKEN")
    monkeypatch.setattr(chz_auth, "signer_inn", lambda sig: org.inn)
    assert "Войти выбранным сертификатом" in client.get(f"/organizations/{org.id}/chz-login").text
    assert client.post(f"/organizations/{org.id}/chz-login/challenge").json() == {"uuid": "u", "data": "d"}
    r = client.post(f"/organizations/{org.id}/chz-login/token",
                    json={"kind": "true_api", "uuid": "u", "signature": "S"})
    assert r.status_code == 200 and r.json()["ok"]
    db.expire_all()
    assert chz_auth.token(db.get(Organization, org.id)) == "TOKEN"
    # СУЗ без ID соединения — отказ, а не запрос в ЧЗ.
    r = client.post(f"/organizations/{org.id}/chz-login/token",
                    json={"kind": "suz", "uuid": "u", "signature": "S"})
    assert r.status_code == 400


def test_certificate_of_another_inn_is_refused(client, db, monkeypatch):
    org = settings.lamoda_org(db)
    monkeypatch.setattr(chz_auth, "signer_inn", lambda sig: "910223073099")
    called = []
    monkeypatch.setattr(chz_auth, "sign_in", lambda *a, **k: called.append(1) or "T")
    r = client.post(f"/organizations/{org.id}/chz-login/token",
                    json={"kind": "true_api", "uuid": "u", "signature": "S"})
    assert r.status_code == 400 and "910223073099" in r.json()["error"]
    assert called == []

"""Вход в Честный знак сертификатом организации (ТЗ, 10.4).

Схема True API: `GET /auth/key` → {uuid, data}; строку `data` подписывает
КриптоПро (прикреплённая CAdES-BES от base64 строки); `POST /auth/simpleSignIn`
{uuid, data: подпись} → {token}. Токен СУЗ (`clientToken`) — тем же способом,
но на `/auth/simpleSignIn/{connectionId}`. Живут ~10 часов.

Подписывает браузер (плагин КриптоПро есть только там), а в ЧЗ ходит
программа: так нет CORS, и ИНН сертификата сверяется здесь, а не только
на странице. Методы Нацкаталога в True API без токена отвечают 401
«Токен не действителен» — одного API-ключа им мало (проверено 30.09.2026).
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy.orm import Session

from markapp.crypto import decrypt_value, encrypt_value
from markapp.models import NkCard, Organization
from markapp.nk import CHZ_PROXY, TRUE_API_URL
from markapp.timeutils import now_utc

TOKEN_LIFETIME = timedelta(hours=10)
# Токен, которому осталось меньше этого, считаем истёкшим: запрос, начатый
# за минуту до конца, упал бы на полпути.
EXPIRY_MARGIN = timedelta(minutes=5)

KINDS = {"true_api": "True API", "suz": "СУЗ"}

INN_OID = "1.2.643.3.131.1.1"


class ChzAuthError(Exception):
    pass


def _proxies():
    return {"http": CHZ_PROXY, "https": CHZ_PROXY} if CHZ_PROXY else None


def _error_text(resp: requests.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("error_message") or body.get("message") or body.get("error_description")
    except (ValueError, AttributeError):
        msg = None
    return f"HTTP {resp.status_code}: {msg or resp.text[:200]}"


def challenge(base_url: str = TRUE_API_URL) -> dict:
    """Строка для подписи: {uuid, data}."""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/auth/key", headers={"accept": "application/json"},
                            proxies=_proxies(), timeout=30)
    except requests.RequestException as e:
        raise ChzAuthError(f"ЧЗ недоступен: {type(e).__name__}")
    if resp.status_code != 200:
        raise ChzAuthError(f"ЧЗ не выдал строку для подписи — {_error_text(resp)}")
    try:
        body = resp.json()
    except ValueError:
        raise ChzAuthError(f"ЧЗ ответил не JSON: {resp.text[:200]}")
    if not isinstance(body, dict) or not body.get("uuid") or not body.get("data"):
        raise ChzAuthError("ЧЗ ответил без uuid/data")
    return {"uuid": body["uuid"], "data": body["data"]}


def sign_in(uuid: str, signature: str, connection_id: str | None = None,
            base_url: str = TRUE_API_URL) -> str:
    """Подпись → токен. С `connection_id` — токен СУЗ, без — True API."""
    path = "/auth/simpleSignIn" + (f"/{connection_id}" if connection_id else "")
    # Плагин отдаёт base64 блоками с переносами — ЧЗ такую подпись не разбирает.
    signature = re.sub(r"\s+", "", signature or "")
    try:
        resp = requests.post(f"{base_url.rstrip('/')}{path}", json={"uuid": uuid, "data": signature},
                             headers={"accept": "application/json"}, proxies=_proxies(), timeout=30)
    except requests.RequestException as e:
        raise ChzAuthError(f"ЧЗ недоступен: {type(e).__name__}")
    if resp.status_code != 200:
        raise ChzAuthError(f"ЧЗ не принял подпись — {_error_text(resp)}")
    try:
        token = (resp.json() or {}).get("token")
    except (ValueError, AttributeError):
        token = None
    if not token:
        raise ChzAuthError("ЧЗ ответил без токена")
    return token


def expires_at(token: str) -> datetime:
    """Срок из самого токена (JWT `exp`), иначе — 10 часов от сейчас.

    Подпись JWT не проверяем: срок нужен только чтобы вовремя попросить вход,
    а подлинность токена проверяет ЧЗ при каждом запросе.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return datetime.fromtimestamp(int(exp), tz=timezone.utc).replace(tzinfo=None)
    except Exception:
        return now_utc() + TOKEN_LIFETIME


def signer_inn(signature: str) -> str | None:
    """ИНН владельца из сертификата внутри подписи; None — если не разобрать.

    Страница сверяет ИНН сама, но проверка здесь не зависит от браузера:
    заказ не от того ИП — самая дорогая ошибка (ТЗ, 10.4).
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import pkcs7
        der = base64.b64decode(re.sub(r"\s+", "", signature))
        certs = pkcs7.load_der_pkcs7_certificates(der)
    except Exception:
        return None
    # В подписи бывает и цепочка (УЦ). Подписант — конечный сертификат: не УЦ
    # и не издатель ни одного другого из набора. Порядок в наборе — сортировка
    # DER, «первый с ИНН» мог бы оказаться УЦ.
    def is_ca(cert) -> bool:
        try:
            return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        except Exception:
            return False
    issuers = {c.issuer.rfc4514_string() for c in certs}
    leaves = [c for c in certs if not is_ca(c) and c.subject.rfc4514_string() not in issuers - {
        c.issuer.rfc4514_string()}]
    if len(leaves) != 1:
        return None
    for attr in leaves[0].subject:
        if attr.oid.dotted_string == INN_OID:
            value = re.sub(r"\D", "", str(attr.value))
            # ИНН физлица в сертификатах ФНС бывает дополнен нулями до 12.
            return value[-12:] if len(value) > 12 else value
    return None


def inn_matches(org_inn: str, cert_inn: str) -> bool:
    a, b = (org_inn or "").strip(), (cert_inn or "").strip()
    return bool(a) and (a == b or a.zfill(12) == b.zfill(12))


def store(db: Session, org: Organization, kind: str, token: str) -> datetime:
    until = expires_at(token)
    if kind == "true_api":
        org.chz_token_enc, org.chz_token_until = encrypt_value(token), until
        # Карточки, упавшие без токена, — снова в очередь, не ждать час повтора.
        (db.query(NkCard).filter(NkCard.status == "error", NkCard.error.like("HTTP 401%"))
         .update({NkCard.status: "pending"}, synchronize_session=False))
    elif kind == "suz":
        org.suz_token_enc, org.suz_token_until = encrypt_value(token), until
    else:
        raise ValueError(kind)
    return until


def token(org: Organization | None, kind: str = "true_api") -> str | None:
    """Действующий токен организации или None — «нужен вход»."""
    if org is None:
        return None
    enc, until = ((org.chz_token_enc, org.chz_token_until) if kind == "true_api"
                  else (org.suz_token_enc, org.suz_token_until))
    if not enc or until is None or until - EXPIRY_MARGIN <= now_utc():
        return None
    return decrypt_value(enc)


def forget(org: Organization, kind: str = "true_api", rejected: str | None = None) -> None:
    """ЧЗ отверг токен до срока — дальше только новый вход.

    `rejected` — тот токен, с которым шёл запрос: человек мог за это время
    войти заново, и новый токен стирать нельзя.
    """
    if rejected is not None and token(org, kind) not in (None, rejected):
        return
    if kind == "true_api":
        org.chz_token_enc, org.chz_token_until = None, None
    else:
        org.suz_token_enc, org.suz_token_until = None, None

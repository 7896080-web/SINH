"""Честный знак: вход сертификатом, статусы кодов, документ ввода, карточка НК.

Запросы шлёт программа, страница только подписывает плагином (CORS не нужен).
Схемы — как в «Маркировке» и рабочей программе заказчика (kiz-tool):
- вход: /auth/key → прикреплённая CAdES-BES от base64(data) → /auth/simpleSignIn;
- /cises/info — статусы по коротким КИ (до 1000 за запрос), по токену;
- /lk/documents/create?pg=lp — LP_INTRODUCE_GOODS, прикреплённая подпись;
- /doc/list?number= — итог документа;
- /nk/product?gtins= — карточка НК, только с токеном (одного apikey мало).
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone

import requests

from kizapp import config

CISES_PER_REQUEST = 1000
INN_OID = "1.2.643.3.131.1.1"
PERMIT_ATTRS = {"Декларация о соответствии": "CONFORMITY_DECLARATION",
                "Сертификат соответствия": "CONFORMITY_CERTIFICATE"}


class ChzError(Exception):
    def __init__(self, text: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(text)
        self.status, self.retry_after = status, retry_after

    @property
    def outcome_unknown(self) -> bool:
        """Сервер мог выполнить запрос: ответа нет или он 5xx."""
        return self.status is None or self.status >= 500


def _proxies():
    return {"http": config.CHZ_PROXY, "https": config.CHZ_PROXY} if config.CHZ_PROXY else None


def _call(method: str, path: str, what: str, token: str | None = None, **kw):
    headers = {"accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.request(method, f"{config.TRUE_API_URL}{path}", headers=headers, proxies=_proxies(),
                             timeout=60, allow_redirects=False, **kw)
    except requests.RequestException as e:
        raise ChzError(f"{what}: ЧЗ недоступен ({type(e).__name__})")
    if r.status_code in (200, 201, 202):
        try:
            return r.json() if r.text.strip() else None
        except ValueError:
            return r.text
    try:
        b = r.json()
        msg = b.get("error_message") or b.get("message") or b.get("errors") or b
    except (ValueError, AttributeError):
        msg = r.text[:300]
    retry = r.headers.get("Retry-After", "") if r.status_code == 429 else ""
    raise ChzError(f"{what}: HTTP {r.status_code}: {str(msg)[:400]}", r.status_code,
                   int(retry) if retry.isdigit() else None)


# --- Вход ------------------------------------------------------------------------------

def challenge() -> dict:
    b = _call("GET", "/auth/key", "строка для подписи")
    if not isinstance(b, dict) or not b.get("uuid") or not b.get("data"):
        raise ChzError("ЧЗ ответил без uuid/data")
    return {"uuid": b["uuid"], "data": b["data"]}


def sign_in(uuid: str, signature: str) -> str:
    b = _call("POST", "/auth/simpleSignIn", "вход", json={"uuid": uuid, "data": re.sub(r"\s+", "", signature)})
    token = b.get("token") if isinstance(b, dict) else None
    if not token:
        raise ChzError("ЧЗ ответил без токена")
    return token


def token_expires(token: str) -> datetime:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part))["exp"]
        return datetime.fromtimestamp(int(exp), tz=timezone.utc).replace(tzinfo=None)
    except Exception:
        return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=10)


def signer_inn(signature: str) -> str | None:
    """ИНН из КОНЕЧНОГО сертификата подписи (не УЦ); None — не разобрать."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import pkcs7
        certs = pkcs7.load_der_pkcs7_certificates(base64.b64decode(re.sub(r"\s+", "", signature)))
    except Exception:
        return None

    def is_ca(c) -> bool:
        try:
            return bool(c.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        except Exception:
            return False
    issuers = {c.issuer.rfc4514_string() for c in certs}
    leaves = [c for c in certs if not is_ca(c)
              and c.subject.rfc4514_string() not in issuers - {c.issuer.rfc4514_string()}]
    if len(leaves) != 1:
        return None
    for a in leaves[0].subject:
        if a.oid.dotted_string == INN_OID:
            v = re.sub(r"\D", "", str(a.value))
            return v[-12:] if len(v) > 12 else v
    return None


def inn_matches(a: str, b: str) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    return bool(a) and a.zfill(12) == b.zfill(12)


# --- Коды и документы ---------------------------------------------------------------------

def cises_info(cis: list[str], token: str) -> dict[str, str]:
    data = _call("POST", "/cises/info", "статусы кодов", token, json=cis)
    items = data if isinstance(data, list) else next(
        (data.get(k) for k in ("results", "items", "cises", "list")
         if isinstance(data, dict) and isinstance(data.get(k), list)), None)
    if items is None:
        raise ChzError(f"ответ /cises/info не распознан: {str(data)[:300]}")
    out = {}
    for item in items:
        info = (item or {}).get("cisInfo") or item or {}
        key = info.get("requestedCis") or info.get("cis") or info.get("uit") or info.get("code")
        if key:
            out[str(key).split("\x1d")[0]] = info.get("status") or info.get("statusEx") or "UNKNOWN"
    return out


def create_document(document_b64: str, signature: str, token: str) -> str:
    body = {"document_format": "MANUAL", "product_document": document_b64, "product_group": "lp",
            "type": "LP_INTRODUCE_GOODS", "signature": re.sub(r"\s+", "", signature)}
    data = _call("POST", "/lk/documents/create?pg=lp", "документ ввода в оборот", token, json=body)
    if isinstance(data, str):
        return data.strip().strip('"')
    if isinstance(data, dict):
        return str(data.get("id") or data.get("value") or data.get("docId") or "")
    return ""


def document_status(doc_id: str, token: str) -> tuple[str, str]:
    data = _call("GET", "/doc/list", "статус документа", token, params={"number": doc_id, "pg": "lp"})
    items = data if isinstance(data, list) else (data or {}).get("results") or (data or {}).get("items") or []
    doc = next((d for d in items if doc_id in (d.get("documentId"), d.get("id"), d.get("number"))), None)
    if doc is None:
        return "", ""
    errs = doc.get("errors") or doc.get("errorMessage") or doc.get("description") or ""
    return doc.get("documentStatus") or doc.get("status") or "", (
        errs if isinstance(errs, str) else json.dumps(errs, ensure_ascii=False))


# --- Нацкаталог ----------------------------------------------------------------------------

def _iso(t: str) -> str:
    t = (t or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", t) or re.match(r"^(\d{2})\.(\d{2})\.(\d{4})", t)
    if not m:
        return ""
    return "-".join(m.groups()) if len(m.group(1)) == 4 else f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


def parse_card(item: dict) -> dict:
    """Нужное из ответа /nk/product: название, ТН ВЭД, разрешительный документ.
    Документ в карточке — «номер:::ГГГГ-ММ-ДД»; несколько — самый новый."""
    attrs = {}
    permits = []
    for a in item.get("good_attrs") or []:
        name, value = str(a.get("attr_name") or "").strip(), str(a.get("attr_value") or "")
        attrs.setdefault(name, value)
        if name in PERMIT_ATTRS:
            for part in re.split(r"[;|\n]", value):
                number, _, rest = part.partition(":::")
                if number.strip():
                    permits.append((_iso(rest[:10]), PERMIT_ATTRS[name], number.strip()))
    day, kind, number = max(permits) if permits else ("", "", "")
    return {"name": item.get("good_name") or "", "tnved": attrs.get("Код ТНВЭД", "").strip(),
            "permit_type": kind, "permit_number": number, "permit_date": day}


def nk_products(gtins: list[str], token: str) -> dict[str, dict]:
    data = _call("GET", "/nk/product", "карточки НК", token, params={"gtins": ";".join(gtins)})
    out = {}
    for item in (data or {}).get("result") or [] if isinstance(data, dict) else []:
        for ident in item.get("identified_by") or []:
            if ident.get("type") == "gtin" and ident.get("value"):
                out[str(ident["value"]).zfill(14)] = parse_card(item)
    return out

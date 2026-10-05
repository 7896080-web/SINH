"""Запросы к СУЗ и True API для кодов (ТЗ, 7.1–7.2). Только HTTP, без базы.

Подписи сюда приходят готовыми от страницы (плагин КриптоПро есть только в
браузере, 10.3):
- СУЗ — каждый запрос с откреплённой подписью в `X-Signature`: у POST
  подписано тело, у GET — путь с query от `/api/v3` ✅ (kiz-tool). Поэтому
  тело и путь строит программа, страница подписывает РОВНО эти строки, и
  отправляются тоже ровно они — байт в байт.
- True API — токен входа (`chz_auth`); документ ввода — прикреплённая подпись.
"""
from __future__ import annotations

import json
import os
from urllib.parse import quote

import requests

from markapp.nk import CHZ_PROXY, TRUE_API_URL

SUZ_URL = os.environ.get("MARKING_SUZ_URL", "https://suzgrid.crpt.ru/api/v3")
SUZ_PATH_PREFIX = "/api/v3"
PRODUCT_GROUP = "lp"
TEMPLATE_ID = 10
CISES_PER_REQUEST = 1000


class ChzApiError(Exception):
    """`status` None — ответа нет (сеть, таймаут): запрос МОГ дойти."""

    def __init__(self, text: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(text)
        self.status = status
        self.retry_after = retry_after

    @property
    def outcome_unknown(self) -> bool:
        """Сервер мог выполнить запрос: ответа нет или он 5xx."""
        return self.status is None or self.status >= 500


# Редиректы запрещены во ВСЕХ запросах к ЧЗ и СУЗ: requests при переходе на
# другой хост снимает только Authorization, а `clientToken` и `X-Signature`
# унёс бы на новый адрес как есть. У ЧЗ законных редиректов в API нет.
def _proxies():
    return {"http": CHZ_PROXY, "https": CHZ_PROXY} if CHZ_PROXY else None


def _check(resp: requests.Response, what: str):
    if resp.status_code in (200, 201, 202):
        try:
            return resp.json() if resp.text.strip() else None
        except ValueError:
            return resp.text
    try:
        body = resp.json()
        msg = (body.get("error_message") or body.get("message") or body.get("errors")
               or body.get("globalErrors") or body.get("fieldErrors") or body)
    except (ValueError, AttributeError):
        msg = resp.text[:300]
    retry = resp.headers.get("Retry-After", "") if resp.status_code == 429 else ""
    raise ChzApiError(f"{what}: HTTP {resp.status_code}: {str(msg)[:400]}", resp.status_code,
                      int(retry) if retry.isdigit() else None)


# --- СУЗ ----------------------------------------------------------------------

def order_body(gtin: str, quantity: int) -> str:
    """Тело заказа как в kiz-tool (ТЗ, 7.1): lp, OPERATOR, шаблон 10, UNIT, произведено в РФ."""
    return json.dumps({
        "productGroup": PRODUCT_GROUP,
        "products": [{"gtin": gtin, "quantity": int(quantity), "serialNumberType": "OPERATOR",
                      "templateId": TEMPLATE_ID, "cisType": "UNIT"}],
        "attributes": {"releaseMethodType": "PRODUCTION"},
    }, ensure_ascii=False, separators=(",", ":"))


def status_path(oms_id: str, order_id: str, gtin: str) -> str:
    return (f"/order/status?omsId={quote(oms_id)}&orderId={quote(order_id)}&gtin={quote(gtin)}")


def codes_path(oms_id: str, order_id: str, gtin: str, quantity: int) -> str:
    return (f"/codes?omsId={quote(oms_id)}&orderId={quote(order_id)}&gtin={quote(gtin)}"
            f"&quantity={int(quantity)}")


def signed_text(path: str) -> str:
    """Что подписывает страница для GET: путь с query от /api/v3."""
    return SUZ_PATH_PREFIX + path


def _suz_headers(client_token: str, signature: str) -> dict:
    return {"accept": "application/json", "clientToken": client_token, "X-Signature": signature}


def create_order(oms_id: str, body: str, client_token: str, signature: str) -> str:
    headers = _suz_headers(client_token, signature) | {"Content-Type": "application/json"}
    try:
        resp = requests.post(f"{SUZ_URL}/order?omsId={quote(oms_id)}", data=body.encode("utf-8"),
                             headers=headers, proxies=_proxies(),
                            allow_redirects=False, timeout=60)
    except requests.RequestException as e:
        raise ChzApiError(f"СУЗ недоступен: {type(e).__name__}")
    data = _check(resp, "заказ в СУЗ")
    order_id = (data or {}).get("orderId") if isinstance(data, dict) else None
    if not order_id:
        raise ChzApiError(f"СУЗ не вернул номер заказа: {str(data)[:300]}")
    return order_id


def suz_get(path: str, client_token: str, signature: str):
    try:
        resp = requests.get(f"{SUZ_URL}{path}", headers=_suz_headers(client_token, signature),
                            proxies=_proxies(),
                            allow_redirects=False, timeout=120)
    except requests.RequestException as e:
        raise ChzApiError(f"СУЗ недоступен: {type(e).__name__}")
    return _check(resp, "СУЗ " + path.split("?")[0])


# --- True API ---------------------------------------------------------------------

def _bearer(token: str) -> dict:
    return {"accept": "application/json", "Authorization": f"Bearer {token}"}


def cises_info(short_codes: list[str], token: str) -> dict[str, str]:
    """Статусы кодов: {короткий КИ: статус}. Не больше 1000 кодов за запрос."""
    if len(short_codes) > CISES_PER_REQUEST:
        raise ValueError("не больше 1000 кодов за запрос")
    try:
        resp = requests.post(f"{TRUE_API_URL}/cises/info", json=short_codes, headers=_bearer(token),
                             proxies=_proxies(),
                            allow_redirects=False, timeout=60)
    except requests.RequestException as e:
        raise ChzApiError(f"True API недоступен: {type(e).__name__}")
    data = _check(resp, "статусы кодов")
    items = (data if isinstance(data, list) else
             next((data.get(k) for k in ("results", "items", "cises", "list") if isinstance(data, dict)
                   and isinstance(data.get(k), list)), None))
    if items is None:
        raise ChzApiError(f"ответ /cises/info не распознан: {str(data)[:300]}")
    out = {}
    for item in items:
        info = (item or {}).get("cisInfo") or item or {}
        key = info.get("requestedCis") or info.get("cis") or info.get("uit") or info.get("code")
        if key:
            out[str(key).split("\x1d")[0]] = info.get("status") or info.get("statusEx") or "UNKNOWN"
    return out


def create_document(document_b64: str, signature: str, token: str) -> str:
    body = {"document_format": "MANUAL", "product_document": document_b64,
            "product_group": PRODUCT_GROUP, "type": "LP_INTRODUCE_GOODS", "signature": signature}
    try:
        resp = requests.post(f"{TRUE_API_URL}/lk/documents/create?pg={PRODUCT_GROUP}", json=body,
                             headers=_bearer(token) | {"Content-Type": "application/json"},
                             proxies=_proxies(),
                            allow_redirects=False, timeout=60)
    except requests.RequestException as e:
        raise ChzApiError(f"True API недоступен: {type(e).__name__}")
    data = _check(resp, "документ ввода в оборот")
    if isinstance(data, str):
        return data.strip().strip('"')
    if isinstance(data, dict):
        return str(data.get("id") or data.get("value") or data.get("docId") or "")
    return ""


def document_status(doc_id: str, token: str) -> tuple[str, str]:
    """(статус, текст ошибок) документа по /doc/list — так проверял kiz-tool."""
    try:
        resp = requests.get(f"{TRUE_API_URL}/doc/list",
                            params={"number": doc_id, "pg": PRODUCT_GROUP},
                            headers=_bearer(token), proxies=_proxies(),
                            allow_redirects=False, timeout=60)
    except requests.RequestException as e:
        raise ChzApiError(f"True API недоступен: {type(e).__name__}")
    data = _check(resp, "статус документа")
    items = data if isinstance(data, list) else (data or {}).get("results") or (data or {}).get("items") or []
    doc = next((d for d in items if doc_id in (d.get("documentId"), d.get("id"), d.get("number"))), None)
    if doc is None:
        return "", ""
    status = doc.get("documentStatus") or doc.get("status") or ""
    errors = doc.get("errors") or doc.get("errorMessage") or doc.get("description") or ""
    return status, (json.dumps(errors, ensure_ascii=False) if not isinstance(errors, str) else errors)

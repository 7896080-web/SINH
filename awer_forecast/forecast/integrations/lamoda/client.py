"""HTTP-клиент Lamoda Seller Partner API v2.

Что делает сам, чтобы вызывающему коду не думать:
- токен: `POST /v2/auth-token` (тело в snake_case — единственное исключение в API),
  время жизни каждый раз берётся из `expires_in`, обновление ЗАРАНЕЕ (за 45 с до
  истечения) и по факту `401` — один перевыпуск и один повтор запроса;
- `sellerId` в query каждого запроса (так требует спецификация для GET-методов);
- пагинация по `meta.totalPages`; лимит страницы проверяется по ответу — API молча
  игнорирует незнакомые и «лишние» параметры;
- повторы с растущей паузой на 429/5xx и сетевых сбоях, с уважением `Retry-After`;
- единый разбор ошибок `{"error": {code, message, details}}` → `LamodaApiError`.

Незнакомые поля ответа просто не читаются — новые поля Lamoda добавляет без анонса.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timezone
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://public-api-seller.lamoda.ru/api"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
NOMENCLATURES_MAX_LIMIT = 25        # GET /v2/nomenclatures, /v2/minimal-prices, /v2/nomenclatures-sell-values
DEFAULT_MAX_LIMIT = 1000            # остальные списочные методы


class LamodaApiError(Exception):
    def __init__(self, http_status: int, code: str, message: str, details=None, path: str = ""):
        self.http_status = http_status
        self.code = code
        self.message = message
        self.details = details or []
        self.path = path
        super().__init__(str(self))

    def __str__(self) -> str:
        text = f"Lamoda {self.http_status} {self.code}: {self.message}"
        if self.details:
            parts = []
            for d in self.details[:5]:
                if isinstance(d, dict):
                    parts.append(f"{d.get('field', '')}: {d.get('issue', '')}".strip(": "))
                else:
                    parts.append(str(d))
            text += " [" + "; ".join(parts) + "]"
        if self.path:
            text += f" ({self.path})"
        if self.http_status == 403:
            text += " — чаще всего это sellerId, не совпадающий с ключами (LAMODA_SELLER_ID)"
        return text


class LamodaFilterIgnored(LamodaApiError):
    """Ответ показывает, что фильтр не применился (например, опечатка в имени параметра)."""


def parse_error(response: httpx.Response, path: str) -> LamodaApiError:
    code, message, details = "", "", []
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        code = str(err.get("code") or "")
        message = str(err.get("message") or "")
        details = err.get("details") or []
    if not code:
        code = {400: "VALIDATION_FAILED", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                429: "TOO_MANY_REQUESTS", 500: "INTERNAL_SERVER_ERROR",
                503: "SERVICE_UNAVAILABLE"}.get(response.status_code, "HTTP_ERROR")
    if not message:
        message = (response.text or "")[:300] or response.reason_phrase
    return LamodaApiError(response.status_code, code, message, details, path)


def format_datetime(value: datetime) -> str:
    """RFC 3339 в UTC с суффиксом Z — формат всех дат-фильтров v2."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_datetime(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class LamodaClient:
    def __init__(self, client_id: str, client_secret: str, seller_id: str, *,
                 base_url: str = DEFAULT_BASE_URL, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 max_retries: int = 5, refresh_margin_seconds: float = 45, timeout: float = 30,
                 request_pause_seconds: float = 0.0):
        if not (client_id and client_secret and seller_id):
            raise ValueError("Нужны все три: LAMODA_CLIENT_ID, LAMODA_CLIENT_SECRET, LAMODA_SELLER_ID")
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.seller_id = str(seller_id)
        self.http = http or httpx.Client(timeout=timeout)
        self.clock = clock
        self.sleep = sleep
        self.max_retries = max_retries
        self.refresh_margin = refresh_margin_seconds
        self.request_pause = request_pause_seconds
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.requests_made = 0

    # ---------------- токен ----------------
    def _token_valid(self) -> bool:
        return self._token is not None and self.clock() < self._token_expires_at - self.refresh_margin

    def _fetch_token(self) -> None:
        path = "/v2/auth-token"
        body = {"grant_type": "client_credentials", "client_id": self.client_id,
                "client_secret": self.client_secret}
        response = self._send("POST", path, json=body, authorized=False)
        if response.status_code >= 400:
            raise parse_error(response, path)
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise LamodaApiError(response.status_code, "BAD_TOKEN_RESPONSE", "в ответе нет access_token", path=path)
        try:
            expires_in = float(data.get("expires_in"))
        except (TypeError, ValueError):
            # Без срока жизни токен годится только на ближайший запрос: следующий
            # перевыпустит его заранее — это дороже, но не приведёт к серии 401.
            logger.warning("Lamoda: в ответе auth-token нет expires_in — токен будет обновляться перед каждым запросом")
            expires_in = 0.0
        self._token = token
        self._token_expires_at = self.clock() + expires_in

    def _ensure_token(self) -> None:
        if not self._token_valid():
            self._fetch_token()

    # ---------------- транспорт ----------------
    def _send(self, method: str, path: str, *, params=None, json=None, authorized: bool = True) -> httpx.Response:
        """Один HTTP-вызов с повторами на сетевых сбоях, 429 и 5xx."""
        url = self.base_url + path
        attempt = 0
        while True:
            headers = {"Accept": "application/json"}
            if authorized:
                headers["Authorization"] = f"Bearer {self._token}"
            try:
                if self.request_pause and self.requests_made:
                    self.sleep(self.request_pause)
                self.requests_made += 1
                response = self.http.request(method, url, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise LamodaApiError(0, "NETWORK_ERROR", f"{exc.__class__.__name__}: {exc}", path=path) from exc
                self.sleep(self._backoff(attempt, None))
                attempt += 1
                continue
            if response.status_code in RETRY_STATUSES and attempt < self.max_retries:
                delay = self._backoff(attempt, response.headers.get("Retry-After"))
                logger.info("Lamoda %s %s → %s, повтор через %.0f с", method, path, response.status_code, delay)
                self.sleep(delay)
                attempt += 1
                continue
            return response

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 120.0)
            except ValueError:
                pass
        return min(2.0 ** attempt, 60.0)

    def request(self, method: str, path: str, *, params: dict | None = None, json=None) -> dict:
        query = {"sellerId": self.seller_id}
        if params:
            query.update({k: v for k, v in params.items() if v is not None})
        refreshed = False
        while True:
            self._ensure_token()
            response = self._send(method, path, params=query, json=json)
            if response.status_code == 401 and not refreshed:
                # Токен мог истечь раньше расчётного (или отозван) — один перевыпуск и повтор.
                self._token = None
                refreshed = True
                continue
            if response.status_code >= 400:
                raise parse_error(response, path)
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise LamodaApiError(response.status_code, "BAD_JSON", "ответ не JSON", path=path) from exc

    def paginate(self, path: str, key: str, params: dict | None = None, *, limit: int = DEFAULT_MAX_LIMIT,
                 max_pages: int = 100_000) -> Iterator[dict]:
        page = 1
        while page <= max_pages:
            data = self.request("GET", path, params={**(params or {}), "page": page, "limit": limit})
            items = data.get(key)
            if items is None:
                raise LamodaApiError(200, "UNEXPECTED_RESPONSE", f"в ответе нет поля «{key}»", path=path)
            meta = data.get("meta") or {}
            if len(items) > limit:
                logger.warning("Lamoda %s: на странице %s записей при limit=%s — API не принял limit",
                               path, len(items), limit)
            yield from items
            total_pages = meta.get("totalPages")
            if not items:
                return
            if total_pages is None:
                if len(items) < limit:
                    return
            elif page >= int(total_pages):
                return
            page += 1
        raise LamodaApiError(200, "TOO_MANY_PAGES", f"больше {max_pages} страниц — остановлено", path=path)

    # ---------------- методы ----------------
    def iter_orders(self, updated_from: datetime, updated_to: datetime | None = None) -> Iterator[dict]:
        """Заказы, обновлённые в [updated_from, updated_to). В списке нет позиций —
        за ними `get_order(order["id"])`. Источник истины по заказам — этот опрос,
        вебхуки (Фаза 2) только ускоряют реакцию."""
        params = {"updatedAtFrom": format_datetime(updated_from), "sort": "updatedAt"}
        if updated_to is not None:
            params["updatedAtTo"] = format_datetime(updated_to)
        tolerance = updated_from.timestamp() - 1
        for order in self.paginate("/v2/orders", "orders", params, limit=DEFAULT_MAX_LIMIT):
            updated = parse_datetime(order.get("updatedAt"))
            if updated is not None and updated.timestamp() < tolerance:
                raise LamodaFilterIgnored(
                    200, "FILTER_IGNORED",
                    f"заказ {order.get('id')} обновлён {order.get('updatedAt')}, раньше updatedAtFrom — "
                    "фильтр не применился, опрос остановлен, чтобы не перекачать всю историю",
                    path="/v2/orders")
            yield order

    def get_order(self, order_id: str) -> dict:
        """Детали заказа по ВНУТРЕННЕМУ `id` из списка (по `orderId` будет 404)."""
        return self.request("GET", f"/v2/orders/{quote(str(order_id), safe='')}")

    def iter_fbo_stocks(self) -> Iterator[dict]:
        """Ликвидный остаток на FBO-складах (без резервов), включая нулевые позиции —
        ноль нужен как признак stockout."""
        return self.paginate("/v2/fbo/stocks", "stocks", {"withZeroQuantity": "true"}, limit=DEFAULT_MAX_LIMIT)

    def iter_fbo_shipments(self, planned_from: date | None = None) -> Iterator[dict]:
        params = {"from": planned_from.isoformat()} if planned_from else {}
        return self.paginate("/v2/fbo/shipments", "shipments", params, limit=DEFAULT_MAX_LIMIT)

    def iter_fbo_shipment_items(self, shipment_id: str) -> Iterator[dict]:
        path = f"/v2/fbo/shipments/{quote(str(shipment_id), safe='')}/items"
        return self.paginate(path, "items", limit=DEFAULT_MAX_LIMIT)

    def iter_nomenclatures(self, country: str = "RU") -> Iterator[dict]:
        """Каталог продавца на Lamoda: размерный `sku`, `parentSku`, `externalSku`, `barcode`.
        `country` обязателен (без него 400), страница — не больше 25."""
        return self.paginate("/v2/nomenclatures", "nomenclatures", {"country": country},
                             limit=NOMENCLATURES_MAX_LIMIT)

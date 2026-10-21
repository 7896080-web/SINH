"""Имитация Lamoda Seller API v2 для тестов и демо-режима (без сети, поверх httpx.MockTransport).

Повторяет то, на что опирается клиент: токен с `expires_in`, `sellerId` в query, 401/403/400
в едином формате ошибок, пагинацию по `meta`, фильтр `updatedAtFrom`/`updatedAtTo` у заказов,
список заказов без позиций и детали по внутреннему `id`. Структура ответов — по OpenAPI v2
(docs/lamoda/openapi-v2.yaml). Это НЕ проверка против настоящего API — см. README.
"""
from __future__ import annotations

import json
from collections import defaultdict
from urllib.parse import parse_qs, urlparse

import httpx


class FakeLamoda:
    """Минимальный Lamoda v2 поверх httpx.MockTransport: токен, пагинация по meta,
    заказы с фильтром updatedAtFrom, детали заказа, остатки и поставки FBO, каталог.

    Ведёт журнал запросов (`calls`), чтобы тесты проверяли, что ушло в API."""

    def __init__(self, *, seller_id: str = "777", expires_in: int = 900):
        self.seller_id = seller_id
        self.expires_in = expires_in
        self.tokens_issued = 0
        self.valid_tokens: set[str] = set()
        self.orders: list[dict] = []          # детали заказов (с items)
        self.stocks: list[dict] = []
        self.shipments: list[dict] = []
        self.shipment_items: dict[str, list[dict]] = defaultdict(list)
        self.nomenclatures: list[dict] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_next: list[httpx.Response] = []     # ответы, отдаваемые ДО обычной логики
        self.ignore_updated_filter = False

    # --- транспорт ---
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        path = url.path.removeprefix("/api")
        query = {k: v if len(v) > 1 else v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append((request.method, path, query))
        if self.fail_next:
            return self.fail_next.pop(0)
        if path == "/v2/auth-token":
            body = json.loads(request.content)
            if body.get("grant_type") != "client_credentials" or body.get("client_secret") != "secret":
                return httpx.Response(400, json={"error": {"code": "VALIDATION_FAILED", "message": "invalid_client"}})
            self.tokens_issued += 1
            token = f"tok{self.tokens_issued}"
            self.valid_tokens.add(token)
            return httpx.Response(201, json={"access_token": token, "expires_in": self.expires_in,
                                             "token_type": "bearer", "scope": ""})
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.valid_tokens:
            return httpx.Response(401, json={"error": {"code": "UNAUTHORIZED", "message": "Unauthorized"}})
        if query.get("sellerId") != self.seller_id:
            return httpx.Response(403, json={"error": {"code": "FORBIDDEN", "message": "Forbidden"}})
        if path == "/v2/orders":
            orders = self.orders
            if not self.ignore_updated_filter and "updatedAtFrom" in query:
                orders = [o for o in orders if o["updatedAt"] >= query["updatedAtFrom"]
                          and ("updatedAtTo" not in query or o["updatedAt"] < query["updatedAtTo"])]
            summaries = [{k: v for k, v in o.items() if k != "items"} for o in orders]
            return self._page(summaries, "orders", query)
        if path.startswith("/v2/orders/"):
            order_id = path.split("/")[3]
            for order in self.orders:
                if order["id"] == order_id:
                    return httpx.Response(200, json=order)
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "order not found"}})
        if path == "/v2/fbo/stocks":
            return self._page(self.stocks, "stocks", query)
        if path == "/v2/fbo/shipments":
            return self._page(self.shipments, "shipments", query)
        if path.startswith("/v2/fbo/shipments/") and path.endswith("/items"):
            return self._page(self.shipment_items[path.split("/")[4]], "items", query)
        if path == "/v2/nomenclatures":
            if "country" not in query:
                return httpx.Response(400, json={"error": {"code": "VALIDATION_FAILED",
                                                           "message": 'query: "country": field required'}})
            if int(query.get("limit", 25)) > 25:
                return httpx.Response(400, json={"error": {"code": "VALIDATION_FAILED", "message": "limit > 25"}})
            return self._page(self.nomenclatures, "nomenclatures", query)
        return httpx.Response(404, text="404 page not found")

    @staticmethod
    def _page(items: list, key: str, query: dict) -> httpx.Response:
        page = int(query.get("page", 1))
        limit = int(query.get("limit", 100))
        total_pages = max(1, -(-len(items) // limit))
        chunk = items[(page - 1) * limit: page * limit]
        return httpx.Response(200, json={key: chunk, "meta": {"total": len(items), "page": page, "limit": limit,
                                                               "totalPages": total_pages}})


def price(rubles: float) -> dict:
    return {"amount": int(round(rubles * 100)), "currency": "RUB"}


def order(order_id: str, updated_at: str, items: list[dict], *, number: str | None = None,
          created_at: str | None = None) -> dict:
    return {"id": order_id, "orderId": number or f"RU{order_id}", "status": "DELIVERED",
            "createdAt": created_at or updated_at, "updatedAt": updated_at, "items": items}


def item(item_id: str, sku: str, status: str, paid: float = 1999.0, external_sku: str | None = None) -> dict:
    return {"id": item_id, "sku": sku, "externalSku": external_sku or "", "status": status,
            "basePrice": price(paid * 1.2), "salePrice": price(paid), "paidPrice": price(paid),
            "totalDiscount": price(paid * 0.2), "size": "M"}

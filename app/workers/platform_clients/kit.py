import time

import requests

from app.workers.platform_clients.base import PlatformClient, PlatformOrder, StockPushItem, CatalogItem
from app.workers.http_retry import with_retry

BASE_URL = "https://api.kit.yandex.net"

# Kit жёстко лимитирует запросы (отвечает 429). Превентивная пауза перед каждым
# запросом держит нас под лимитом, чтобы историческая выборка (get_orders_since)
# и опрос не упирались в 429: with_retry ловит его реактивно, но медленно и с
# риском не добрать часть данных после исчерпания попыток.
_THROTTLE_SECONDS = 0.25

# Статусы заказа подтверждены по OpenAPI-спеке Kit (skill yandex-kit-cabinet).
# WAIT_FOR_CONFIRMATION — «ожидает подтверждения продавца» (наш триггер приёма).
CANCELLED_STATUSES = {"CANCELLED", "DELIVERY_CANCELLED", "FULL_REFUND"}
PARTIAL_REFUND_STATUS = "PARTIAL_REFUND"
# Заказ ПОДТВЕРЖДЁН и пошёл в доставку — наш триггер перемещения
# «<Площадка>.Ожидает» → «Склад <Площадка>». Всё, что после подтверждения из
# WAIT_FOR_CONFIRMATION и не отмена.
CONFIRMED_STATUSES = {
    "CREATING_INITIAL_RECEIPT", "SETUP_DELIVERY", "WAIT_FOR_DELIVERY",
    "CREATING_FINAL_RECEIPTS", "DELIVERED", "COMPLETED",
}


class KitClient(PlatformClient):
    name = "kit"

    def __init__(self, token: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        # Авторизация Bearer — подтверждена на живом API (вызовы /v1/orders,
        # /v1/warehouses, /v1/variants возвращают 200 с этим заголовком).
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _get(self, path: str, params=None):
        def call():
            time.sleep(_THROTTLE_SECONDS)
            r = self.session.get(f"{BASE_URL}{path}", params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        return with_retry(call)

    def _post(self, path: str, json_body=None):
        def call():
            time.sleep(_THROTTLE_SECONDS)
            r = self.session.post(f"{BASE_URL}{path}", json=json_body or {}, timeout=30)
            r.raise_for_status()
            return r.json() if r.content else {}
        return with_retry(call)

    def test_connection(self) -> tuple[bool, str]:
        """GET /v1/warehouses — лёгкий read-only вызов, список складов
        магазина, без побочных эффектов. Обязателен параметр `status`
        (без него Kit отвечает 400 VALIDATION_ERROR); валидное значение —
        `ACTIVE` (подтверждено на живом API)."""
        try:
            self._get("/v1/warehouses", params={"status": "ACTIVE"})
            return True, "Соединение установлено, токен действителен."
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 401:
                return False, "401 — токен недействителен."
            return False, f"Ошибка {status} при обращении к Kit."
        except requests.RequestException as e:
            return False, f"Не удалось связаться с Kit: {e}"

    def get_orders_awaiting_confirmation(self) -> list[PlatformOrder]:
        result = []
        variant_barcode_cache: dict[str, str] = {}

        page = 1
        while True:
            data = self._get("/v1/orders", params={"page": page, "per_page": 100})
            orders = data.get("orders", [])
            if not orders:
                break

            for o in orders:
                if o.get("status") != "WAIT_FOR_CONFIRMATION":
                    continue
                for chunk in o.get("delivery_chunks", []):
                    for item in chunk.get("items", []):
                        variant_id = item["product_variant_id"]

                        # У Kit заказ отдаёт product_variant_id, а не баркод
                        # напрямую (см. предупреждение в base.py — этот участок
                        # закрывает ту нестыковку). Резолвим через тот же
                        # объект Variant, где 'barcode' — подтверждённое поле.
                        barcode = variant_barcode_cache.get(variant_id)
                        if barcode is None:
                            barcode = self._resolve_variant_barcode(variant_id)
                            variant_barcode_cache[variant_id] = barcode or ""
                        if not barcode:
                            continue

                        result.append(PlatformOrder(
                            order_id=f"{o['id']}:{chunk['id']}:{item['id']}",
                            barcode=barcode,
                            quantity=item.get("quantity", 1),
                            raw_status="WAIT_FOR_CONFIRMATION",
                        ))
            if len(orders) < 100:
                break
            page += 1
        return result

    def get_orders_since(self, date_from):
        """FBS-заказы Kit с даты date_from. /v1/orders постранично; фильтруем
        по created_at на нашей стороне (в разобранном API нет параметра
        диапазона дат). order_date = дата заказа."""
        from datetime import datetime as _dt, date as _date
        threshold = date_from.date() if isinstance(date_from, _dt) else date_from

        result = []
        variant_barcode_cache: dict[str, str] = {}
        page = 1
        while page <= 200:  # защитный предел на число страниц
            data = self._get("/v1/orders", params={"page": page, "per_page": 100})
            orders = data.get("orders", [])
            if not orders:
                break
            for o in orders:
                order_date = None
                raw = o.get("created_at") or o.get("created")
                if raw:
                    try:
                        order_date = _dt.fromisoformat(str(raw).replace("Z", "+00:00")).date()
                    except ValueError:
                        order_date = None
                if order_date is not None and order_date < threshold:
                    continue
                for chunk in o.get("delivery_chunks", []):
                    for item in chunk.get("items", []):
                        variant_id = item["product_variant_id"]
                        barcode = variant_barcode_cache.get(variant_id)
                        if barcode is None:
                            barcode = self._resolve_variant_barcode(variant_id)
                            variant_barcode_cache[variant_id] = barcode or ""
                        if not barcode:
                            continue
                        result.append(PlatformOrder(
                            order_id=f"{o['id']}:{chunk['id']}:{item['id']}",
                            barcode=barcode, quantity=item.get("quantity", 1),
                            raw_status=str(o.get("status") or ""), order_date=order_date,
                        ))
            if len(orders) < 100:
                break
            page += 1
        return result

    def _resolve_variant_barcode(self, variant_id: str) -> str | None:
        try:
            variant = self._get(f"/v1/variants/{variant_id}")
            return variant.get("barcode") or None
        except requests.HTTPError:
            return None

    def get_cancelled_orders(self, order_ids: list[str]) -> list[PlatformOrder]:
        # Учитываем ТОЛЬКО НАШУ (продавцовскую) отмену до отгрузки. В разобранном
        # API Kit объект заказа не отдаёт инициатора отмены, поэтому отличить нашу
        # отмену от клиентского возврата/отказа нельзя: все статусы отмен
        # (CANCELLED / DELIVERY_CANCELLED / FULL_REFUND / PARTIAL_REFUND) — это уже
        # пост-отгрузка. Чтобы не откатывать чужие отмены (товар уже ушёл, у нас его
        # нет), авто-реверс по Kit отключён: возврат оформляется в 1С отдельно и
        # подтягивается часовой сверкой. Вернуть реверс, когда/если Kit начнёт
        # отдавать инициатора отмены и признак «до отгрузки».
        return []

    def get_confirmed_orders(self, order_ids: list[str]) -> list[PlatformOrder]:
        """Из ранее принятых заказов — те, что подтверждены и пошли в доставку
        (статус вышел из WAIT_FOR_CONFIRMATION в один из CONFIRMED_STATUSES).
        Триггер перемещения «<Площадка>.Ожидает» → «Склад <Площадка>».
        GET /v1/orders/{id} по корневому id (фильтра статуса у списка нет)."""
        if not order_ids:
            return []

        order_root_ids = {oid.split(":")[0] for oid in order_ids}
        confirmed = []
        for root_id in order_root_ids:
            try:
                o = self._get(f"/v1/orders/{root_id}")
            except requests.HTTPError:
                continue
            if o.get("status") in CONFIRMED_STATUSES:
                for oid in order_ids:
                    if oid.startswith(root_id + ":"):
                        confirmed.append(PlatformOrder(
                            order_id=oid, barcode="", quantity=0,
                            raw_status=o.get("status"),
                        ))
        return confirmed

    def confirm_order(self, order_id: str) -> bool:
        """Явное действие подтверждения — Kit требует активного вызова,
        не просто ждёт смены статуса (раздел 5 спецификации)."""
        root_id = order_id.split(":")[0]
        try:
            self.session.post(f"{BASE_URL}/v1/orders/{root_id}/confirm", timeout=30).raise_for_status()
            return True
        except requests.HTTPError:
            return False

    def push_stock(self, warehouse_id: str, items: list[StockPushItem]) -> dict:
        ok, errors = [], []
        for i in range(0, len(items), 5000):
            chunk = items[i:i + 5000]
            # Kit идентифицирует позицию по variant_id (UUID варианта), НЕ по
            # баркоду. Берём external_id (id варианта из каталога); если каталог
            # не загружен — падаем на баркод (для реального Kit не сработает).
            body = {"items": [
                {"variant_id": it.external_id or it.barcode, "warehouse_id": warehouse_id, "quantity": it.quantity}
                for it in chunk
            ]}

            def call():
                r = self.session.post(f"{BASE_URL}/v1/variants/stocks/bulk_update", json=body, timeout=30)
                r.raise_for_status()
                return r

            try:
                with_retry(call)
                ok.extend(it.barcode for it in chunk)
            except requests.HTTPError as e:
                detail = {}
                try:
                    detail = e.response.json()
                except Exception:
                    pass
                errors.append({"detail": str(e), "response": detail})
            except requests.RequestException as e:
                errors.append({"detail": str(e)})
        return {"ok": ok, "errors": errors}

    def get_catalog_items(self) -> list[CatalogItem]:
        result = []
        page = 1
        while True:
            data = self._get("/v1/variants", params={"page": page, "per_page": 100})
            result.extend(_parse_kit_variants(data))
            variants = data.get("variants", [])
            if len(variants) < 100:
                break
            page += 1
        return result


def _parse_kit_variants(data: dict) -> list[CatalogItem]:
    """Вынесено отдельно для тестирования без сети.

    external_id — идентификатор ВАРИАНТА (размер-цвет SKU), т.к. пул баркодов
    принадлежит размер-цвету, а не карточке товара. У варианта обычно один
    баркод; если их несколько — они образуют пул этого размер-цвета."""
    result = []
    for v in data.get("variants", []):
        barcode = v.get("barcode")
        if not barcode:
            continue
        result.append(CatalogItem(
            external_id=str(v.get("id", "")), barcode=barcode,
            article=v.get("sku", ""), name=v.get("name", ""),
        ))
    return result

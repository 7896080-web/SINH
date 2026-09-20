from datetime import datetime, timedelta, timezone

import requests

from app.workers.platform_clients.base import PlatformClient, PlatformOrder, StockPushItem, CatalogItem
from app.workers.http_retry import with_retry

BASE_URL = "https://api-seller.ozon.ru"

CANCELLED_STATUSES = {"cancelled"}


def _iso(dt: datetime) -> str:
    """RFC3339 в UTC, как ждёт Ozon: 2026-09-14T10:00:00.000Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class OzonClient(PlatformClient):
    name = "ozon"
    # Остаток Ozon адресует offer_id — артикулом продавца, не баркодом.
    stock_key = "article"

    def __init__(self, client_id: str, api_key: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "Client-Id": client_id,
            "Api-Key": api_key,
            "Content-Type": "application/json",
        })
        # Выдачу оборвал защитный предел страниц, а не конец данных — картина
        # неполная, и вызывающий обязан это знать (см. recalc.collect_orders).
        self.last_truncated = False

    def _post(self, path: str, json_body=None):
        def call():
            r = self.session.post(f"{BASE_URL}{path}", json=json_body or {}, timeout=30)
            r.raise_for_status()
            return r.json()
        return with_retry(call)

    def test_connection(self) -> tuple[bool, str]:
        """Нет отдельного /ping у Ozon — используем самый лёгкий read-only
        вызов: список товаров с лимитом 1, без побочных эффектов.
        `/v2/product/list` удалён Ozon (404) — актуальный метод `/v3/product/list`
        (подтверждено на живом API)."""
        try:
            self._post("/v3/product/list", {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1})
            return True, "Соединение установлено, ключи действительны."
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status in (401, 403):
                return False, f"{status} — Client-Id/Api-Key недействительны."
            return False, f"Ошибка {status} при обращении к Ozon."
        except requests.RequestException as e:
            return False, f"Не удалось связаться с Ozon: {e}"

    def get_orders_awaiting_confirmation(self) -> list[PlatformOrder]:
        # Ozon требует окно cutoff (срок упаковки) — обязательные cutoff_from/
        # cutoff_to, иначе 400 «mismatch between cutoff & delivery date».
        # awaiting_approve — новые заказы, дедлайн упаковки в будущем; берём
        # широкое окно, чтобы не потерять ни одного.
        now = datetime.now(timezone.utc)
        data = self._post("/v3/posting/fbs/unfulfilled/list", {
            "dir": "ASC",
            "filter": {
                "cutoff_from": _iso(now - timedelta(days=2)),
                "cutoff_to": _iso(now + timedelta(days=60)),
                "status": "awaiting_approve",
            },
            "limit": 100,
            "offset": 0,
            "with": {"barcodes": True},
        })

        result = []
        for posting in data.get("result", {}).get("postings", []):
            posting_number = posting.get("posting_number")
            for product in posting.get("products", []):
                barcode = product.get("barcode") or product.get("offer_id")
                if not barcode:
                    continue
                # Отдельная строка задания на каждую позицию отправления —
                # у одного posting_number может быть несколько товаров.
                result.append(PlatformOrder(
                    order_id=f"{posting_number}:{product.get('sku', barcode)}",
                    barcode=barcode,
                    quantity=int(product.get("quantity", 1)),
                    raw_status="awaiting_approve",
                ))
        return result

    def get_orders_since(self, date_from):
        """FBS-отправления с даты date_from через /v3/posting/fbs/list
        (since = начало date_from, to = сейчас). in_process_at → order_date.
        Статус не фильтруем — берём все, чтобы не потерять уже
        отгруженные/доставленные заказы периода (модель FBS)."""
        from datetime import datetime as _dt, date as _date
        if isinstance(date_from, _date):
            since = _dt(date_from.year, date_from.month, date_from.day, tzinfo=timezone.utc)
        else:
            since = date_from
        now = datetime.now(timezone.utc)

        result = []
        offset = 0
        for _ in range(200):  # защитный предел на число страниц
            data = self._post("/v3/posting/fbs/list", {
                "dir": "ASC",
                "filter": {"since": _iso(since), "to": _iso(now)},
                "limit": 1000, "offset": offset,
                "with": {"barcodes": True},
            })
            postings = data.get("result", {}).get("postings", [])
            if not postings:
                break
            for posting in postings:
                posting_number = posting.get("posting_number")
                order_date = None
                raw = posting.get("in_process_at") or posting.get("created_at")
                if raw:
                    try:
                        order_date = _dt.fromisoformat(str(raw).replace("Z", "+00:00")).date()
                    except ValueError:
                        order_date = None
                for product in posting.get("products", []):
                    barcode = product.get("barcode") or product.get("offer_id")
                    if not barcode:
                        continue
                    result.append(PlatformOrder(
                        order_id=f"{posting_number}:{product.get('sku', barcode)}",
                        barcode=barcode, quantity=int(product.get("quantity", 1)),
                        raw_status=str(posting.get("status") or ""), order_date=order_date,
                    ))
            # Конец данных определяет сам Озон полем has_next, а не длина
            # страницы: короткая страница при has_next=true у него бывает, и
            # ранний стоп молча съедал бы хвост ленты. Ровно на этом у WB
            # терялись заказы целыми неделями (см. wb.ORDERS_WINDOW_DAYS).
            has_next = data.get("result", {}).get("has_next")
            if has_next is None:
                has_next = len(postings) >= 1000
            if not has_next:
                break
            offset += len(postings)
        else:
            self.last_truncated = True
        return result

    def get_cancelled_orders(self, order_ids: list[str]) -> list[PlatformOrder]:
        if not order_ids:
            return []

        posting_numbers = {oid.split(":")[0] for oid in order_ids}
        cancelled = []

        # /v3/posting/fbs/list требует период since/to. Отмены отслеживаем по
        # недавним заказам — окна в 30 дней достаточно.
        now = datetime.now(timezone.utc)
        data = self._post("/v3/posting/fbs/list", {
            "dir": "ASC",
            "filter": {
                "since": _iso(now - timedelta(days=30)),
                "to": _iso(now),
                "status": "cancelled",
            },
            "limit": 1000,
            "offset": 0,
        })
        # Только НАША отмена: продавец отменил постинг ДО отгрузки (товар остался
        # у нас → возврат резерва на ЦС). Клиент/Ozon/система и любая отмена ПОСЛЕ
        # отгрузки (cancelled_after_ship) — это возврат, его не реверсим: он придёт
        # в 1С отдельно и подтянется сверкой. Поле cancellation.cancellation_initiator
        # ∈ {Seller, Client, Customer, Ozon, System, Delivery}.
        our_cancelled_numbers = set()
        for p in data.get("result", {}).get("postings", []):
            canc = p.get("cancellation") or {}
            initiator = (canc.get("cancellation_initiator") or "").strip().lower()
            if initiator == "seller" and not canc.get("cancelled_after_ship"):
                our_cancelled_numbers.add(p.get("posting_number"))

        for oid in order_ids:
            if oid.split(":")[0] in our_cancelled_numbers:
                cancelled.append(PlatformOrder(order_id=oid, barcode="", quantity=0,
                                                raw_status="cancelled", is_cancellation=True))
        return cancelled

    def push_stock(self, warehouse_id: str, items: list[StockPushItem]) -> dict:
        ok, errors = [], []
        # Ozon принимает до 100 позиций за запрос — режем на пачки
        for i in range(0, len(items), 100):
            chunk = items[i:i + 100]
            # Ozon идентифицирует товар по offer_id (артикул продавца), НЕ по
            # баркоду. Берём article; если каталог не загружен — падаем на
            # баркод (для реального Ozon это не сработает, каталог обязателен).
            def _offer_id(it: StockPushItem) -> str:
                return it.article or it.barcode
            offer_to_barcode = {_offer_id(it): it.barcode for it in chunk}
            body = {"stocks": [
                {"offer_id": _offer_id(it), "stock": it.quantity, "warehouse_id": warehouse_id}
                for it in chunk
            ]}
            try:
                resp = self._post("/v2/products/stocks", body)
                for result in resp.get("result", []):
                    # ok возвращаем в баркодах — так рассылка сматчит результат
                    if result.get("updated"):
                        ok.append(offer_to_barcode.get(result.get("offer_id"), result.get("offer_id")))
                    else:
                        errors.append(result)
            except requests.HTTPError as e:
                errors.append({"detail": str(e), "items": [it.barcode for it in chunk]})
        return {"ok": ok, "errors": errors}

    def get_catalog_items(self) -> list[CatalogItem]:
        """/v3/product/list отдаёт только offer_id/product_id без баркода —
        баркод достаём отдельным вызовом /v3/product/info/list (поле
        `barcodes` — список, подтверждено на живом API). `/v2/product/list`
        удалён Ozon (404), используется `/v3/product/list` с той же схемой
        ответа (result.items, result.last_id)."""
        result = []
        last_id = ""
        for _ in range(50):
            list_data = self._post("/v3/product/list", {
                "filter": {"visibility": "ALL"}, "last_id": last_id, "limit": 1000,
            })
            items = list_data.get("result", {}).get("items", [])
            if not items:
                break

            product_ids = [i["product_id"] for i in items if i.get("product_id")]
            if product_ids:
                info_data = self._post("/v3/product/info/list", {"product_id": product_ids})
                result.extend(_parse_ozon_product_info(info_data))

            last_id = list_data.get("result", {}).get("last_id", "")
            if not last_id:
                break
        return result


def _parse_ozon_product_info(data: dict) -> list[CatalogItem]:
    """Вынесено отдельно для тестирования без сети.

    `/v3/product/info/list` отдаёт баркоды списком в поле `barcodes`
    (подтверждено на живом API). Берём первый непустой. На всякий случай
    поддержан и устаревший singular `barcode`, если где-то встретится."""
    result = []
    for item in data.get("items", data.get("result", {}).get("items", [])):
        barcodes = item.get("barcodes") or ([] if not item.get("barcode") else [item["barcode"]])
        # Все баркоды одного product_id — пул размер-цвет SKU (общий external_id).
        for barcode in [b for b in barcodes if b]:
            result.append(CatalogItem(
                external_id=str(item.get("id", "")),
                barcode=barcode,
                article=item.get("offer_id", ""),
                name=item.get("name", ""),
            ))
    return result

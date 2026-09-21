from datetime import datetime, timedelta, timezone

import requests

from app.workers.platform_clients.base import PlatformClient, PlatformOrder, StockPushItem, CatalogItem
from app.workers.http_retry import with_retry

BASE_URL = "https://api-seller.ozon.ru"

CANCELLED_STATUSES = {"cancelled"}

# По сколько отправлений за страницу. Сто — предел ручки unfulfilled/list;
# тысяча — предел fbs/list. Отдельные имена нужны, чтобы условие «страница
# короче предела значит конец» считало по тому же числу, что ушло в запрос.
ORDERS_PAGE_SIZE = 100
CANCELLED_PAGE_SIZE = 1000
# Защитный предел на число страниц: столько же, сколько у выгрузки заказов.
MAX_PAGES = 200

# Коды отказа по КОНКРЕТНОЙ позиции, которые повтором не лечатся: карточки или
# склада у площадки нет, и через двадцать три минуты повторов ответ будет тот же.
# Всё остальное (лимиты, временные сбои) оставляем повторам.
OZON_TERMINAL_ITEM_CODES = frozenset({
    "NOT_FOUND_ERROR", "PRODUCT_NOT_FOUND", "OFFER_NOT_FOUND",
    "WAREHOUSE_NOT_FOUND", "INVALID_OFFER_ID",
})


def _error_codes(result: dict) -> set[str]:
    """Коды ошибок из строки ответа Ozon по одной позиции."""
    out = set()
    for err in result.get("errors") or []:
        if isinstance(err, dict):
            code = str(err.get("code") or "").strip().upper()
            if code:
                out.add(code)
    return out


def _error_text(result: dict) -> str:
    """Человеческая причина отказа: коды и сообщения площадки."""
    parts = []
    for err in result.get("errors") or []:
        if not isinstance(err, dict):
            continue
        code = str(err.get("code") or "").strip()
        message = str(err.get("message") or "").strip()
        parts.append(": ".join(p for p in (code, message) if p))
    return "; ".join(parts)[:300]


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
        # Лента листается ПО КУРСОРУ, как и в get_orders_since. Раньше здесь был
        # ровно один запрос с offset=0: как только у кабинета одновременно висит
        # больше сотни отправлений в awaiting_approve (выходные, распродажа,
        # задержка подтверждения), заказы со сто первого не видел никто. И не
        # увидел бы уже никогда: подтверждённое отправление уходит из
        # awaiting_approve, то есть остаток по нему не списывается, документа в
        # 1С нет, а наружу продолжает уходить завышенное число. Тот же класс
        # дефекта, что окно 29 дней у WB, только тише.
        now = datetime.now(timezone.utc)
        result = []
        offset = 0
        for _ in range(MAX_PAGES):         # защитный предел на число страниц
            data = self._post("/v3/posting/fbs/unfulfilled/list", {
                "dir": "ASC",
                "filter": {
                    "cutoff_from": _iso(now - timedelta(days=2)),
                    "cutoff_to": _iso(now + timedelta(days=60)),
                    "status": "awaiting_approve",
                },
                "limit": ORDERS_PAGE_SIZE,
                "offset": offset,
                "with": {"barcodes": True},
            })
            postings = data.get("result", {}).get("postings", [])
            if not postings:
                break
            for posting in postings:
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
            has_next = data.get("result", {}).get("has_next")
            if has_next is None:
                has_next = len(postings) >= ORDERS_PAGE_SIZE
            if not has_next:
                break
            offset += len(postings)
        else:
            # Выдачу оборвал наш предел — картина неполная, и молчать об этом
            # нельзя: `recalc.collect_orders` превращает этот признак в проблему
            # и не даёт поставить товару «актуализирован».
            self.last_truncated = True
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
        for _ in range(MAX_PAGES):  # защитный предел на число страниц
            data = self._post("/v3/posting/fbs/list", {
                "dir": "ASC",
                "filter": {"since": _iso(since), "to": _iso(now)},
                "limit": CANCELLED_PAGE_SIZE, "offset": offset,
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
                has_next = len(postings) >= CANCELLED_PAGE_SIZE
            if not has_next:
                break
            offset += len(postings)
        else:
            self.last_truncated = True
        return result

    def get_cancelled_orders(self, order_ids: list[str]) -> list[PlatformOrder]:
        if not order_ids:
            return []

        cancelled = []

        # /v3/posting/fbs/list требует период since/to. Отмены отслеживаем по
        # недавним заказам — окна в 30 дней достаточно.
        #
        # Лента листается ПО КУРСОРУ. Раньше запрос был один, `dir: ASC` и
        # `offset: 0`, то есть тысяча САМЫХ СТАРЫХ отмен за тридцать дней. У
        # кабинета, где их больше тысячи, свежие отмены — ровно те, которые ещё
        # можно отреверсить, — не доезжали вовсе: списанная под заказ единица не
        # возвращалась на остаток, перемещение в 1С не отменялось, товар при этом
        # физически лежал на ЦС. Наружу уходило заниженное число, то есть
        # недопродажи, а в 1С оставался лишний документ.
        now = datetime.now(timezone.utc)
        our_cancelled_numbers = set()
        offset = 0
        for _ in range(MAX_PAGES):
            data = self._post("/v3/posting/fbs/list", {
                "dir": "ASC",
                "filter": {
                    "since": _iso(now - timedelta(days=30)),
                    "to": _iso(now),
                    "status": "cancelled",
                },
                "limit": CANCELLED_PAGE_SIZE,
                "offset": offset,
            })
            postings = data.get("result", {}).get("postings", [])
            if not postings:
                break
            # Только НАША отмена: продавец отменил постинг ДО отгрузки (товар
            # остался у нас → возврат резерва на ЦС). Клиент/Ozon/система и любая
            # отмена ПОСЛЕ отгрузки (cancelled_after_ship) — это возврат, его не
            # реверсим: он придёт в 1С отдельно и подтянется сверкой. Поле
            # cancellation.cancellation_initiator ∈ {Seller, Client, Customer,
            # Ozon, System, Delivery}.
            for posting in postings:
                canc = posting.get("cancellation") or {}
                initiator = (canc.get("cancellation_initiator") or "").strip().lower()
                if initiator == "seller" and not canc.get("cancelled_after_ship"):
                    our_cancelled_numbers.add(posting.get("posting_number"))
            has_next = data.get("result", {}).get("has_next")
            if has_next is None:
                has_next = len(postings) >= CANCELLED_PAGE_SIZE
            if not has_next:
                break
            offset += len(postings)
        else:
            self.last_truncated = True

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
                    offer_id = result.get("offer_id")
                    barcode = offer_to_barcode.get(offer_id, offer_id)
                    if result.get("updated"):
                        ok.append(barcode)
                        continue
                    # Отказ ПО КОНКРЕТНОЙ ПОЗИЦИИ отдаём в том же виде, что WB и
                    # Kit: с ключом `sku` и признаком `terminal`. Раньше словарь
                    # уходил наверх сырым — без обоих полей, — и рассылка не
                    # могла отличить «этой карточки на складе нет» от обрыва
                    # связи: пять попыток по нарастающей паузе (около двадцати
                    # трёх минут) в заведомо неизменный ответ, а потом запись в
                    # `error` с текстом «не отправлено за 5 попыток». Отчёт по
                    # этому тексту относил её к «рассылка не доехала» (критично,
                    # «площадка продаёт то, чего нет») вместо «неизвестный sku»
                    # («продавать нечего, чинить мэппинг») — человека отправляли
                    # чинить связь вместо сопоставления.
                    codes = _error_codes(result)
                    detail = _error_text(result) or str(result)[:300]
                    errors.append({
                        "sku": barcode,
                        "terminal": bool(codes & OZON_TERMINAL_ITEM_CODES),
                        "detail": detail,
                    })
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

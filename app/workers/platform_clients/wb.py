import requests

from app.workers.platform_clients.base import PlatformClient, PlatformOrder, StockPushItem, CatalogItem
from app.workers.http_retry import with_retry

BASE_URL = "https://marketplace-api.wildberries.ru"
# Контентные методы (карточки товаров) WB отдаёт на отдельном хосте
# content-api, не на marketplace-api (подтверждено на живом API).
CONTENT_BASE_URL = "https://content-api.wildberries.ru"

# Заказы WB: supplierStatus мы двигаем сами, wbStatus двигает площадка.
# 'new' = ожидает подтверждения (раздел 5 спецификации).
# Реверсим ТОЛЬКО НАШУ отмену (supplierStatus=cancel) — продавец отменил заказ ДО
# отгрузки, товар остался у нас → возврат резерва на ЦС. Клиентские отмены/отказы
# (wbStatus canceled/canceled_by_client/declined_by_client) приходят ПОСЛЕ отгрузки:
# товар уже ушёл, у нас его нет; возврат оформляется в 1С отдельно и подтягивается
# сверкой — их НЕ реверсим.
CANCELLED_SUPPLIER_STATUSES = {"cancel"}

# `/api/v3/orders` отдаёт ОКНО фиксированной длины, начинающееся от `dateFrom`, а
# не «все заказы с этой даты». Установлено на живом кабинете 18.09.2026:
# dateFrom=07.08 вернул 520 заказов с датами 07.08 .. 05.09 и на этом закончился,
# хотя заказы после 05.09 существуют; dateFrom=01.07 вернул НОЛЬ при 244 от 07.08.
# Одним запросом от базовой даты мы теряли всё, что новее её плюс месяц: по одному
# кабинету недосчитывались 253 заказа, и расчёт при этом рапортовал «проведено 0,
# проблем нет» и ставил товару «актуализирован». Поэтому ленту берём окнами.
ORDERS_WINDOW_DAYS = 29


class WbClient(PlatformClient):
    name = "wb"

    def __init__(self, token: str, warehouse_id: str, session: requests.Session | None = None):
        self.token = token
        self.warehouse_id = warehouse_id
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": token})
        # Выдачу оборвал защитный предел, а не конец данных. Картина неполная —
        # см. get_orders_since.
        self.last_truncated = False

    def _get(self, path: str, **kwargs):
        def call():
            r = self.session.get(f"{BASE_URL}{path}", timeout=30, **kwargs)
            r.raise_for_status()
            return r.json()
        return with_retry(call)

    def _post(self, path: str, json_body=None, **kwargs):
        def call():
            r = self.session.post(f"{BASE_URL}{path}", json=json_body, timeout=30, **kwargs)
            r.raise_for_status()
            return r.json()
        return with_retry(call)

    def _post_content(self, path: str, json_body=None, **kwargs):
        """POST на контентный хост WB (content-api), а не marketplace-api."""
        def call():
            r = self.session.post(f"{CONTENT_BASE_URL}{path}", json=json_body, timeout=30, **kwargs)
            r.raise_for_status()
            return r.json()
        return with_retry(call)

    def test_connection(self) -> tuple[bool, str]:
        """/ping — штатный лёгкий метод раздела General (api-information),
        свой на каждый домен категории. Не требует прав ни на что, кроме
        валидного токена нужной категории (Marketplace)."""
        try:
            r = self.session.get(f"{BASE_URL}/ping", timeout=15)
            r.raise_for_status()
            return True, "Соединение установлено, токен действителен."
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 401:
                return False, "401 — токен недействителен или не той категории (нужна Marketplace)."
            return False, f"Ошибка {status} при обращении к WB."
        except requests.RequestException as e:
            return False, f"Не удалось связаться с WB: {e}"

    def get_orders_awaiting_confirmation(self) -> list[PlatformOrder]:
        data = self._get("/api/v3/orders/new")
        orders = data.get("orders", [])

        result = []
        for o in orders:
            # WB отдаёт баркод в поле skus (список) — берём первый непустой.
            skus = o.get("skus") or []
            barcode = skus[0] if skus else None
            if not barcode:
                continue
            result.append(PlatformOrder(
                order_id=str(o["id"]),
                barcode=barcode,
                quantity=1,  # заказ FBS у WB — одна позиция на строку задания сборки
                raw_status="new",
            ))
        return result

    def _orders_window(self, day) -> list[dict]:
        """Сырая выдача `/api/v3/orders` от одной даты, со всеми её страницами.

        Листаем ПО КУРСОРУ `next`, а не по размеру страницы. Соседний метод
        `get_catalog_items` эту же грабку уже прошёл: WB отдаёт меньше лимита за
        страницу, но данные при этом не кончились. Стоп — пустая страница, нет
        курсора или курсор не сдвинулся (защита от зацикливания).
        """
        from datetime import datetime, timezone

        ts = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        out, cursor, prev = [], 0, None
        for _ in range(200):          # защитный предел на число страниц
            data = self._get("/api/v3/orders",
                             params={"limit": 1000, "next": cursor, "dateFrom": ts})
            orders = data.get("orders", [])
            out.extend(orders)
            nxt = data.get("next", 0)
            if not orders or not nxt or nxt == prev:
                break
            prev, cursor = nxt, nxt
        else:
            # Вышли по пределу страниц, а не потому что данные кончились.
            self.last_truncated = True
        return out

    def get_orders_since(self, date_from):
        """FBS-заказы (сборочные задания) с даты date_from.

        Идём ОКНАМИ по `ORDERS_WINDOW_DAYS` дней от даты до сегодняшнего дня:
        `/api/v3/orders?dateFrom=` отдаёт окно, а не всё с даты (см. комментарий
        у константы). Окна перекрываются на день, поэтому склеиваем с
        дедупликацией по id заказа — иначе один заказ провёлся бы дважды.

        `last_truncated` — признак, что выдачу оборвал защитный предел, а не
        конец данных. Вызывающий обязан считать такую картину неполной:
        `recalc.collect_orders` превращает его в проблему и не даёт поставить
        товару «актуализирован» по заказам, которых не видел.
        """
        from datetime import date as _date, datetime, timedelta, timezone

        self.last_truncated = False

        if isinstance(date_from, datetime):
            start = date_from.date()
        elif isinstance(date_from, _date):
            start = date_from
        else:
            start = datetime.fromtimestamp(int(date_from), tz=timezone.utc).date()
        today = datetime.now(timezone.utc).date()

        result, seen = [], set()
        day = start
        while True:
            for o in self._orders_window(day):
                oid = str(o.get("id"))
                if not oid or oid in seen:
                    continue
                skus = o.get("skus") or []
                barcode = skus[0] if skus else None
                if not barcode:
                    continue
                seen.add(oid)
                order_date = None
                created = o.get("createdAt")
                if created:
                    try:
                        order_date = datetime.fromisoformat(str(created).replace("Z", "+00:00")).date()
                    except ValueError:
                        order_date = None
                result.append(PlatformOrder(
                    order_id=oid, barcode=barcode, quantity=1,
                    raw_status=str(o.get("supplierStatus") or "new"), order_date=order_date,
                ))
            if day >= today:
                break
            day = min(day + timedelta(days=ORDERS_WINDOW_DAYS), today)
        return result

    def get_cancelled_orders(self, order_ids: list[str]) -> list[PlatformOrder]:
        if not order_ids:
            return []

        # /api/v3/orders/status принимает пачками до 1000 int64 ID
        int_ids = [int(x) for x in order_ids]
        data = self._post("/api/v3/orders/status", {"orders": int_ids})

        cancelled = []
        for o in data.get("orders", []):
            supplier_status = o.get("supplierStatus")
            # Только НАША отмена (supplierStatus=cancel) = возврат резерва на ЦС.
            # Клиентские отмены (wbStatus) игнорируем — это возврат после отгрузки.
            if supplier_status in CANCELLED_SUPPLIER_STATUSES:
                cancelled.append(PlatformOrder(
                    order_id=str(o["id"]), barcode="", quantity=0,
                    raw_status=supplier_status or "", is_cancellation=True,
                ))
        return cancelled

    def push_stock(self, warehouse_id: str, items: list[StockPushItem]) -> dict:
        body = {"stocks": [{"sku": i.barcode, "amount": i.quantity} for i in items]}

        def call():
            resp = self.session.put(f"{BASE_URL}/api/v3/stocks/{warehouse_id}", json=body, timeout=30)
            resp.raise_for_status()
            return resp

        try:
            with_retry(call)
            return {"ok": [i.barcode for i in items], "errors": []}
        except requests.HTTPError as e:
            # WB возвращает детали невалидных элементов в errors — при желании
            # можно распарсить e.response.json()['errors'] для точечного репорта
            return {"ok": [], "errors": [{"detail": str(e)}]}
        except requests.RequestException as e:
            return {"ok": [], "errors": [{"detail": str(e)}]}

    def get_catalog_items(self) -> list[CatalogItem]:
        """Список карточек — content-api, метод v2 (`/content/v2/get/cards/list`).
        Старый `/content/v1/cards/cursor/list` на marketplace-api удалён WB (404).
        В v2 карточки и курсор — на верхнем уровне ответа, тело запроса — под
        ключом `settings` (подтверждено на живом API)."""
        result = []
        limit = 1000
        cursor = {"limit": limit}
        prev_key = None
        for _ in range(200):  # защитный предел на число страниц за один вызов
            data = self._post_content("/content/v2/get/cards/list", {
                "settings": {"cursor": cursor, "filter": {"withPhoto": -1}},
            })
            cards = data.get("cards", data.get("data", {}).get("cards", []))
            result.extend(_parse_wb_cards(data))

            resp_cursor = data.get("cursor", {})
            updated_at = resp_cursor.get("updatedAt")
            nm_id = resp_cursor.get("nmID")
            # Пагинация v2: листаем, ПОКА страница что-то вернула И курсор сдвигается.
            # НЕ останавливаемся на total<limit — WB отдаёт меньше лимита за страницу,
            # но карточек может быть больше (из-за раннего стопа каталог не догружался
            # и часть товаров не давала предложений). Стоп: пустая страница, нет
            # курсора, или курсор не сдвинулся (защита от зацикливания).
            key = (updated_at, nm_id)
            if not cards or not updated_at or not nm_id or key == prev_key:
                break
            prev_key = key
            cursor = {"limit": limit, "updatedAt": updated_at, "nmID": nm_id}
        return result


def _parse_wb_cards(data: dict) -> list[CatalogItem]:
    """Вынесено отдельной функцией — чтобы разбор ответа можно было
    протестировать на примере JSON без реального похода в сеть."""
    result = []
    # v2: карточки на верхнем уровне (`cards`); поддержан и старый вложенный
    # вид (`data.cards`) на случай отката.
    for card in data.get("cards", data.get("data", {}).get("cards", [])):
        nm_id = str(card.get("nmID", ""))
        article = card.get("vendorCode", "")
        # На живом API v2 поле `title` заполнено реальным названием товара;
        # subjectName — это категория. Порядок: title -> subjectName -> артикул.
        name = card.get("title") or card.get("subjectName") or article
        for size in card.get("sizes", []):
            # external_id — идентификатор РАЗМЕР-ЦВЕТ SKU (карточка+характеристика),
            # а не карточки: пул баркодов принадлежит размер-цвету. Один размер
            # может иметь несколько баркодов (skus) — это его пул.
            sku_id = f"{nm_id}:{size.get('chrtID', '')}"
            for sku in size.get("skus", []):
                result.append(CatalogItem(external_id=sku_id, barcode=sku, article=article, name=name))
    return result

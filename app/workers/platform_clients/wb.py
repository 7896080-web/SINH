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

# Сколько идентификаторов заказов влезает в один запрос статусов
# (`/api/v3/orders/status`). Предел площадки, а не наш выбор.
STATUS_BATCH_IDS = 1000

# Сколько sku влезает в один запрос остатков (`/api/v3/stocks/{warehouseId}`).
# Тот же предел площадки, что и у статусов.
STOCKS_BATCH_SKUS = 1000


class WbClient(PlatformClient):
    name = "wb"
    # Остаток WB адресует sku, а sku у него — это баркод.
    stock_key = "barcode"

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

        # `/api/v3/orders/status` принимает не больше STATUS_BATCH_IDS
        # идентификаторов за запрос — РЕЖЕМ НА ПАЧКИ. Раньше предел был только в
        # комментарии: весь список уходил одним запросом, и как только открытых
        # заказов набралось бы больше тысячи, площадка отвергала бы его целиком.
        # Отмены перестали бы отслеживаться СРАЗУ ПО ВСЕМУ кабинету, а выглядело
        # бы это обычной ошибкой опроса — после пяти подряд предохранитель гасит
        # кабинет. Заказы WB при этом не закрываются никогда (площадка не отдаёт
        # подтверждения, см. base.get_confirmed_orders), так что список растёт со
        # скоростью продаж и предел — вопрос времени, а не гипотеза.
        cancelled = []
        int_ids = [int(x) for x in order_ids]
        for start in range(0, len(int_ids), STATUS_BATCH_IDS):
            chunk = int_ids[start:start + STATUS_BATCH_IDS]
            data = self._post("/api/v3/orders/status", {"orders": chunk})
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

    def _not_found_skus(self, response) -> set[str]:
        """Sku, про которые WB в ответе 409 сказал «не знаю такого на складе».

        Формат подтверждён на бою 20.09.2026:

            [{"data":[{"sku":"2000932279695","chrtId":0,"amount":0}],
              "code":"NotFound","message":"Not found"}]

        Разбираем ТОЛЬКО `code == "NotFound"`. Другие коды 409 значат что-то
        иное, и выкидывать по ним позиции из запроса нельзя: мы не знаем, что
        именно площадка забраковала, и молча урезать отправку значило бы решить
        за неё.
        """
        try:
            payload = response.json()
        except Exception:                      # noqa: BLE001 — тело не разобралось
            return set()
        if not isinstance(payload, list):
            payload = [payload]
        bad: set[str] = set()
        for entry in payload:
            if not isinstance(entry, dict) or entry.get("code") != "NotFound":
                continue
            for row in entry.get("data") or []:
                sku = str((row or {}).get("sku") or "")
                if sku:
                    bad.add(sku)
        return bad

    def push_stock(self, warehouse_id: str, items: list[StockPushItem]) -> dict:
        """Отправка остатков. Успех — 204 с ПУСТЫМ телом (проверено на живом
        кабинете 19.09.2026: `PUT` одним sku вернул 204, и число применилось).

        Тело ответа раньше не читалось вовсе: любой 2xx считался успехом по ВСЕМ
        позициям. Так нельзя. Если WB когда-нибудь ответит успешным кодом с
        телом, это будет означать что-то, чего мы не ждали, — и молча записать
        такой ответ в «отправлено всё» значит соврать самим себе о том, что
        лежит на площадке. Схему возможных ошибок в 2xx мы не знаем (на бою её не
        видели), поэтому не выдумываем: непустое тело при успешном коде отдаём
        наверх как ошибку с самим текстом, пусть человек посмотрит.

        ОДИН НЕИЗВЕСТНЫЙ SKU РОНЯЕТ ВСЮ ПАЧКУ, и это главное про этот метод.
        20.09 на бою: в пачке из ста позиций один баркод WB на складе не знал, и
        он ответил `409 NotFound` — ни одна из остальных девяноста девяти не
        применилась. Повтор бессмыслен: неизвестным sku он и останется, а
        значит сотня живых карточек не получила бы свой остаток НИКОГДА.

        Поэтому виновников вынимаем из запроса и шлём остальное. WB сам называет
        их в теле ответа, гадать не приходится. Выброшенные возвращаются в
        `errors` с пометкой `terminal`: повторять их незачем — пока карточки на
        складе нет, ответ будет тот же, и пять попыток только оттянут момент,
        когда человек про это узнает.
        """
        remaining = list(items)
        dropped: list[dict] = []

        # Цикл, а не одна попытка: WB перечисляет неизвестные sku в ответе, но
        # обещания назвать ВСЕ сразу он не давал. Предел по числу позиций —
        # каждый проход выкидывает хотя бы одну, иначе выходим сами.
        for _ in range(len(items) + 1):
            if not remaining:
                break
            body = {"stocks": [{"sku": i.barcode, "amount": i.quantity} for i in remaining]}

            def call(body=body):
                resp = self.session.put(f"{BASE_URL}/api/v3/stocks/{warehouse_id}",
                                        json=body, timeout=30)
                resp.raise_for_status()
                return resp

            try:
                resp = with_retry(call)
            except requests.HTTPError as e:
                detail = str(e)
                response = e.response
                text = (response.text or "").strip() if response is not None else ""
                if text:
                    detail = f"{detail}: {text[:300]}"

                bad = self._not_found_skus(response) if response is not None else set()
                bad &= {i.barcode for i in remaining}
                if not bad:
                    # Забраковано что-то другое — разбирать это самим мы не
                    # беремся, отдаём как есть по всей оставшейся пачке.
                    return {"ok": [i.barcode for i in items if i not in remaining],
                            "errors": dropped + [{"detail": detail}]}

                for sku in sorted(bad):
                    dropped.append({
                        "sku": sku, "terminal": True,
                        "detail": f"площадка не знает этот sku на складе {warehouse_id} "
                                  f"(409 NotFound) — остаток по нему не уедет, пока "
                                  f"карточки там нет",
                    })
                remaining = [i for i in remaining if i.barcode not in bad]
                continue
            except requests.RequestException as e:
                return {"ok": [i.barcode for i in items if i not in remaining],
                        "errors": dropped + [{"detail": str(e)}]}

            text = (resp.text or "").strip()
            if text:
                return {"ok": [], "errors": dropped + [
                    {"detail": f"HTTP {resp.status_code} с телом (ожидали пустое): {text[:300]}"},
                ]}
            return {"ok": [i.barcode for i in remaining], "errors": dropped}

        # Сюда попадаем, только если WB забраковал ВСЕ позиции по очереди.
        return {"ok": [], "errors": dropped}

    def get_stocks(self, warehouse_id: str, skus: list[str]) -> dict[str, int] | None:
        """Что WB держит по этим sku на этом складе.

        `POST /api/v3/stocks/{warehouseId}` — метод ЧТЕНИЯ, несмотря на глагол:
        тело запроса со списком sku, в ответе `stocks` с количеством. Проверено
        на живом кабинете 19.09.2026.

        Sku, которых WB на складе не знает, в ответе просто нет — и в словаре их
        тоже не будет. Отличать «нет в ответе» от нуля обязательно: ноль значит
        «карточка есть, остаток пуст», отсутствие — «такого sku здесь нет вовсе»,
        и это разные поводы для разбора.
        """
        if not skus:
            return {}

        out: dict[str, int] = {}
        for start in range(0, len(skus), STOCKS_BATCH_SKUS):
            chunk = skus[start:start + STOCKS_BATCH_SKUS]

            def call(chunk=chunk):
                resp = self.session.post(f"{BASE_URL}/api/v3/stocks/{warehouse_id}",
                                          json={"skus": chunk}, timeout=30)
                resp.raise_for_status()
                return resp

            try:
                data = with_retry(call).json()
            except requests.RequestException:
                # Площадка не ответила — это НЕ «остатков нет». Возвращаем None,
                # чтобы сверка честно осталась непроведённой: записать сюда
                # пустой словарь значило бы объявить все наши отправки
                # расхождением и позвать человека разбирать сетевой сбой.
                return None
            for row in data.get("stocks", []):
                sku = str(row.get("sku") or "")
                if sku:
                    out[sku] = int(row.get("amount") or 0)
        return out

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

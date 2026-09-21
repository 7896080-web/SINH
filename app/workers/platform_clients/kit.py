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

# Защитный предел на число страниц ленты заказов за один обход. Упёрлись в него
# — картина неполная, и клиент обязан сказать об этом (`last_truncated`).
MAX_ORDER_PAGES = 200

# Сколько пар товар+склад Kit принимает в одном запросе остатков (предел из
# спеки, `BulkUpdateStocksRequest.items.maxItems`).
MAX_STOCK_ITEMS = 5000

# Коды ошибки ЭЛЕМЕНТА массовой операции (`BulkOperationItemError.code`).
# Повторять такие незачем: ответ не изменится, пока не поправят каталог или
# мэппинг, — поэтому рассылка закрывает их сразу, не тратя пять попыток.
TERMINAL_ITEM_CODES = {
    "VARIANT_NOT_FOUND": "площадка не знает такой товар (variant_id {vid}) — "
                         "карточки в этом кабинете нет",
    "VARIANT_ARCHIVED": "карточка товара в архиве — остаток на неё не принимают",
    "DUPLICATE_ITEM": "одна и та же пара товар+склад ушла в запросе дважды",
    "INVALID_QUANTITY": "площадка не приняла количество",
}

# А эти два кода — про КАБИНЕТ, а не про позицию: склад в его настройках указан
# неверно или заархивирован. Выкидывать по ним позиции нельзя: они не виноваты,
# и очередь вымерла бы целиком, хотя чинится это одной правкой настройки.
ACCOUNT_ERROR_CODES = {"WAREHOUSE_NOT_FOUND", "WAREHOUSE_ARCHIVED"}

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
    # Остаток Kit адресует variant_id варианта, он же external_id строки
    # каталога. Баркод в этой роли не работает: площадка отвечает на него
    # VARIANT_NOT_FOUND и бракует ВЕСЬ запрос, а не одну позицию.
    stock_key = "external_id"

    def __init__(self, token: str, session: requests.Session | None = None,
                 variant_map_loader=None):
        self.session = session or requests.Session()
        # Авторизация Bearer — подтверждена на живом API (вызовы /v1/orders,
        # /v1/warehouses, /v1/variants возвращают 200 с этим заголовком).
        self.session.headers.update({"Authorization": f"Bearer {token}"})

        # `variant_map_loader` — функция без аргументов, отдающая уже известное
        # соответствие variant_id -> баркод из НАШЕЙ базы (снимок каталога
        # кабинета, `platform_catalog_items`, где для Kit external_id и есть
        # идентификатор варианта). Без неё клиент спрашивает баркод у площадки
        # отдельным GET на КАЖДУЮ строку КАЖДОГО заказа: на одной странице их
        # больше сотни, страниц до двухсот, и всё это заново на каждый товар —
        # Kit отвечает на такое 429, а 429 здесь оборачивается потерей заказа.
        # Загружаем лениво и один раз на экземпляр клиента.
        self._variant_map_loader = variant_map_loader
        self._variant_map: dict[str, str] | None = None

        # Сколько строк заказов пришлось выбросить, потому что баркод узнать НЕ
        # УДАЛОСЬ (429 после всех повторов, сетевая ошибка, 4xx). Это не то же
        # самое, что вариант без баркода: там ответ получен и он пустой — такой
        # товар просто не наш. Разница принципиальна: пустой ответ это факт, а
        # неудача — пробел, и расчёт, не знающий о пробеле, поставит товару
        # «актуализирован» по заказам, которых не видел.
        self.last_unresolved = 0
        # Выдачу оборвал защитный предел страниц, а не конец данных — картина
        # неполная (см. `_walk_orders`), и `recalc.collect_orders` превращает это
        # в проблему, не давая поставить товару «актуализирован».
        self.last_truncated = False
        # Варианты, по которым запрос уже провалился в пределах текущего вызова.
        # Нужны отдельно от кэша баркодов: в кэше пустая строка значит «ответ
        # получен, баркода нет» — это не потеря, и путать их нельзя.
        self._failed_variants: set[str] = set()

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

    def _walk_orders(self):
        """Сырая лента `/v1/orders` постранично.

        Конец ленты определяет `total_count` из ответа, а НЕ длина страницы.
        Короткая страница у площадки не обязана значить «данные кончились»: ровно
        на этом WB терял заказы неделями (см. `wb.ORDERS_WINDOW_DAYS`), и здесь
        стоял тот же стоп `len(orders) < 100`. 18.09 на живом кабинете он оказался
        честным — страницы 100/100/88, четвёртая пустая, `total_count` = 288
        сошёлся с собранным, — но это совпадение, а не гарантия. `total_count` Kit
        отдаёт сам и отвечает на вопрос прямо, так что спрашиваем его.

        Если `total_count` в ответе нет, идём до пустой страницы: лишний запрос
        дешевле пропущенной продажи. Упёрлись в `MAX_ORDER_PAGES` — поднимаем
        `last_truncated`: выдачу оборвали мы, а не площадка.
        """
        collected = 0
        for page in range(1, MAX_ORDER_PAGES + 1):
            data = self._get("/v1/orders", params={"page": page, "per_page": 100})
            orders = data.get("orders", [])
            if not orders:
                return
            yield from orders
            collected += len(orders)
            total = data.get("total_count")
            if isinstance(total, int) and collected >= total:
                return
        self.last_truncated = True

    def get_orders_awaiting_confirmation(self) -> list[PlatformOrder]:
        result = []
        variant_barcode_cache: dict[str, str] = {}
        self.last_unresolved = 0
        self.last_truncated = False
        self._failed_variants = set()

        for o in self._walk_orders():
            if o.get("status") != "WAIT_FOR_CONFIRMATION":
                continue
            for chunk in o.get("delivery_chunks", []):
                for item in chunk.get("items", []):
                    variant_id = item["product_variant_id"]

                    # У Kit заказ отдаёт product_variant_id, а не баркод
                    # напрямую (см. предупреждение в base.py — этот участок
                    # закрывает ту нестыковку). Резолвим через тот же
                    # объект Variant, где 'barcode' — подтверждённое поле.
                    barcode = self._barcode_for_variant(variant_id, variant_barcode_cache)
                    if not barcode:
                        continue

                    result.append(PlatformOrder(
                        order_id=f"{o['id']}:{chunk['id']}:{item['id']}",
                        barcode=barcode,
                        quantity=item.get("quantity", 1),
                        raw_status="WAIT_FOR_CONFIRMATION",
                    ))
        return result

    def get_orders_since(self, date_from):
        """FBS-заказы Kit с даты date_from. /v1/orders постранично; фильтруем
        по created_at на нашей стороне (в разобранном API нет параметра
        диапазона дат). order_date = дата заказа."""
        from datetime import datetime as _dt, timezone
        from app.timeutils import local_date_of, local_day_start_utc
        # Граница — МОМЕНТ начала местных суток, а не UTC-шное число. Дата,
        # взятая из UTC-времени, у заказа первых трёх часов дня оказывается
        # вчерашней, и такой заказ отсеивался как «раньше базовой даты»:
        # продажа не проводилась, остаток оставался завышенным на неё.
        threshold_day = local_date_of(date_from) if isinstance(date_from, _dt) else date_from
        threshold = local_day_start_utc(threshold_day)

        result = []
        variant_barcode_cache: dict[str, str] = {}
        self.last_unresolved = 0
        self.last_truncated = False
        self._failed_variants = set()
        for o in self._walk_orders():
            order_date, created_at = None, None
            raw = o.get("created_at") or o.get("created")
            if raw:
                try:
                    created_at = _dt.fromisoformat(str(raw).replace("Z", "+00:00"))
                except ValueError:
                    created_at = None
                if created_at is not None:
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    # Местная дата: уезжает в 1С датой перемещения.
                    order_date = local_date_of(created_at)
            if created_at is not None and created_at < threshold:
                continue
            for chunk in o.get("delivery_chunks", []):
                for item in chunk.get("items", []):
                    variant_id = item["product_variant_id"]
                    barcode = self._barcode_for_variant(variant_id, variant_barcode_cache)
                    if not barcode:
                        continue
                    result.append(PlatformOrder(
                        order_id=f"{o['id']}:{chunk['id']}:{item['id']}",
                        barcode=barcode, quantity=item.get("quantity", 1),
                        raw_status=str(o.get("status") or ""), order_date=order_date,
                    ))
        return result

    def _local_variant_map(self) -> dict[str, str]:
        if self._variant_map is None:
            try:
                self._variant_map = dict(self._variant_map_loader() or {}) \
                    if self._variant_map_loader else {}
            except Exception:   # своя база недоступна — просто идём в API, как раньше
                self._variant_map = {}
        return self._variant_map

    def _resolve_variant_barcode(self, variant_id: str) -> str | None:
        """Баркод варианта, или None — если у варианта его нет.

        ПОДНИМАЕТ исключение, если спросить не удалось. Раньше здесь стоял
        `except requests.HTTPError: return None`, и 429 после исчерпания попыток
        становился неотличим от «у варианта нет баркода»: заказ молча исчезал,
        наверх не уходило ничего, и расчёт считал, что кабинет честно ответил
        «продаж не было». Отличать отсутствие от неудачи обязан вызывающий."""
        variant = self._get(f"/v1/variants/{variant_id}")
        return variant.get("barcode") or None

    def _barcode_for_variant(self, variant_id: str, cache: dict[str, str]) -> str | None:
        """Баркод строки заказа: своя база → кэш вызова → площадка.

        None означает «этой строки у нас не будет». Если причина — неудачный
        запрос, счётчик `last_unresolved` растёт, и вызывающий узнает, что
        картина неполная."""
        # Считаем ПОТЕРЯННЫЕ СТРОКИ, а не различающиеся варианты: один и тот же
        # вариант встречается в разных заказах, и каждая его строка — отдельная
        # непроведённая отгрузка. Повторно спрашивать площадку при этом не идём.
        if variant_id in self._failed_variants:
            self.last_unresolved += 1
            return None
        if variant_id in cache:
            return cache[variant_id] or None

        barcode = self._local_variant_map().get(variant_id)
        if not barcode:
            try:
                barcode = self._resolve_variant_barcode(variant_id)
            except requests.RequestException:
                # Больше по этому варианту в пределах вызова не ходим: если Kit
                # уже отвечает 429, повторы только усугубят лимит.
                self._failed_variants.add(variant_id)
                self.last_unresolved += 1
                return None
        cache[variant_id] = barcode or ""
        return barcode or None

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

    def _item_errors(self, response) -> dict[str, str]:
        """{variant_id: код ошибки} из тела отказа массовой операции.

        Спека (`BulkOperationError`) обещает рядом с общим `code`/`message`
        список `errors` с разбором ПО ЭЛЕМЕНТАМ: `variant_id`, `warehouse_id`,
        `code`. Именно его мы раньше не читали — а в `last_error` он и не
        попадал, потому что рассылка режет текст и список обрезало."""
        try:
            payload = response.json()
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        out: dict[str, str] = {}
        for entry in payload.get("errors") or []:
            if not isinstance(entry, dict):
                continue
            vid = str(entry.get("variant_id") or "")
            if vid:
                out[vid] = str(entry.get("code") or "")
        return out

    def push_stock(self, warehouse_id: str, items: list[StockPushItem]) -> dict:
        """Остатки на Kit: `POST /v1/variants/stocks/bulk_update`.

        ОДНА НЕПРИНЯТАЯ ПОЗИЦИЯ РОНЯЕТ ВЕСЬ ЗАПРОС — это главное про этот метод.
        20.09 на бою по кабинету КИТ не уехало НИ ОДНОГО остатка: каждый запрос
        возвращал `400 VALIDATION_ERROR`, потому что в каждой пачке было
        несколько позиций без карточки в каталоге кабинета. 642 записи очереди
        сожгли по пять попыток и легли в `error`, а на площадке всё это время
        стояли чужие числа.

        Kit, в отличие от WB, виновных называет прямо — `errors[]` с
        `variant_id` и кодом. Поэтому вынимаем их из запроса и шлём остальное, а
        сами позиции закрываем пометкой `terminal`: пока карточки нет или она в
        архиве, ответ не изменится, и пять попыток с паузами только оттянут
        момент, когда человек узнает про неразрешённую пару товар+кабинет.

        Код про СКЛАД (`WAREHOUSE_NOT_FOUND`/`WAREHOUSE_ARCHIVED`) так не
        разбираем: позиции в нём не виноваты, виновата настройка кабинета.
        Выкинув их, мы бы похоронили всю очередь вместо того, чтобы дать
        повторам дождаться правки.
        """
        ok: list[str] = []
        errors: list[dict] = []
        for start in range(0, len(items), MAX_STOCK_ITEMS):
            part_ok, part_errors = self._push_stock_chunk(
                warehouse_id, items[start:start + MAX_STOCK_ITEMS])
            ok += part_ok
            errors += part_errors
        return {"ok": ok, "errors": errors}

    def _push_stock_chunk(self, warehouse_id: str,
                          chunk: list[StockPushItem]) -> tuple[list[str], list[dict]]:
        # Позиция без variant_id не просто не уедет — она уронит запрос целиком
        # (площадка ответит VALIDATION_ERROR на всё тело). Рассылка такие сюда
        # уже не пускает, но клиент обязан защищаться сам: падать на баркод
        # «а вдруг поймёт» здесь нельзя ни при каких обстоятельствах.
        dropped: list[dict] = [
            {"sku": it.barcode, "terminal": True,
             "detail": "нет variant_id: карточки этого товара нет в каталоге кабинета"}
            for it in chunk if not it.external_id
        ]
        remaining = [it for it in chunk if it.external_id]

        # Цикл, а не одна попытка: Kit называет виновных, но обещания назвать
        # ВСЕХ сразу не давал. Предел по числу позиций — каждый проход выносит
        # хотя бы одну, иначе выходим сами.
        for _ in range(len(chunk) + 1):
            if not remaining:
                return [], dropped

            body = {"items": [
                {"variant_id": it.external_id, "warehouse_id": warehouse_id,
                 "quantity": it.quantity}
                for it in remaining
            ]}

            def call(body=body):
                r = self.session.post(f"{BASE_URL}/v1/variants/stocks/bulk_update",
                                      json=body, timeout=30)
                r.raise_for_status()
                return r

            try:
                with_retry(call)
                return [it.barcode for it in remaining], dropped
            except requests.HTTPError as e:
                response = e.response
                detail = str(e)
                item_errors = self._item_errors(response) if response is not None else {}
                if response is not None:
                    try:
                        detail = f"{detail}: {str(response.json())[:400]}"
                    except Exception:
                        pass

                if ACCOUNT_ERROR_CODES & set(item_errors.values()):
                    return [], dropped + [{"detail": detail}]

                by_variant = {it.external_id: it for it in remaining}
                bad = {vid: code for vid, code in item_errors.items()
                       if vid in by_variant and code in TERMINAL_ITEM_CODES}
                if not bad:
                    # Либо площадка не назвала виновных, либо код незнакомый.
                    # Урезать отправку молча в таком случае значит решить за
                    # неё, что именно она забраковала.
                    return [], dropped + [{"detail": detail}]

                for vid, code in sorted(bad.items()):
                    dropped.append({
                        "sku": by_variant[vid].barcode, "terminal": True,
                        "detail": TERMINAL_ITEM_CODES[code].format(vid=vid),
                    })
                remaining = [it for it in remaining if it.external_id not in bad]
                continue
            except requests.RequestException as e:
                return [], dropped + [{"detail": str(e)}]

        # Сюда попадаем, только если Kit забраковал все позиции по очереди.
        return [], dropped

    # ------------------------------------------------ публикация карточки

    def hidden_stock_keys(self) -> set[str] | None:
        """variant_id карточек в статусе `HIDDEN`.

        На витрине Kit есть настройка «скрывать товары с нулевым остатком»:
        распроданная карточка сама уходит в `HIDDEN` — и обратно САМА НЕ
        ВОЗВРАЩАЕТСЯ. Прихода остатка ей мало, нужен явный перевод статуса,
        иначе товар есть, а купить его нельзя.

        Спрашиваем список одним проходом, а не статус по каждой позиции: у
        площадки лимит десять запросов в секунду, а скрытых карточек обычно
        куда меньше, чем отправляемых остатков.

        `status` в запросе — фильтр, но полагаться на него одного нельзя:
        неизвестные параметры Kit молча игнорирует, и если фильтр однажды
        перестанет быть известным, мы получили бы ВЕСЬ каталог и опубликовали
        всё подряд. Поэтому статус каждой строки проверяем ещё и у себя.

        `None` — спросить не удалось ИЛИ список вышел неполным; публикация в этом
        случае не делается вовсе. Неполный список опаснее отсутствующего: он
        выглядит как ответ, и карточки за его границей остаются скрытыми молча.
        """
        found: set[str] = set()
        collected = 0
        for page in range(1, MAX_ORDER_PAGES + 1):
            try:
                data = self._get("/v1/variants",
                                 params={"page": page, "per_page": 100, "status": "HIDDEN"})
            except requests.RequestException:
                return None
            rows = data.get("variants") or []
            if not rows:
                return found
            for v in rows:
                if str(v.get("status") or "") == "HIDDEN" and v.get("id"):
                    found.add(str(v["id"]))
            collected += len(rows)
            total = data.get("total_count")
            if isinstance(total, int) and collected >= total:
                return found
        # Упёрлись в предел страниц — список НЕПОЛНЫЙ, и вернуть его как полный
        # нельзя: контракт у метода ровно обратный («None — спросить не удалось,
        # публиковать вслепую нельзя»). Карточки за границей остались бы
        # скрытыми навсегда: остаток передан, площадка его приняла, у нас всё
        # зелено — а товара на витрине нет и продаж не будет.
        return None

    def publish_stock_key(self, key: str) -> bool:
        """Вернуть карточку на витрину: `status` → `PUBLISHED`.

        `PATCH /v1/variants/{id}` — это JSON Merge Patch: передаём ТОЛЬКО
        статус, остальное у карточки остаётся как есть. Передать заодно
        остатки было бы опасно — в этом методе они заменяют весь список
        целиком, и неполный список обнулил бы склады, которых в нём нет.

        Архивные карточки сюда не попадают: их площадка не отдаёт в списке
        скрытых, а патч архивной карточки она всё равно отвергает (сначала
        разархивация — это осознанное решение человека, не наше).
        """
        def call():
            time.sleep(_THROTTLE_SECONDS)
            r = self.session.patch(
                f"{BASE_URL}/v1/variants/{key}",
                json={"status": "PUBLISHED"},
                headers={"Content-Type": "application/merge-patch+json"},
                timeout=30,
            )
            r.raise_for_status()
            return r

        try:
            with_retry(call)
            return True
        except requests.RequestException:
            return False

    def get_catalog_items(self) -> list[CatalogItem]:
        """Каталог кабинета постранично.

        Конец определяет `total_count`, а НЕ длина страницы — ровно по той же
        причине, что и у ленты заказов (см. `_walk_orders`): короткая страница у
        Kit не значит «данные кончились». Стоял здесь именно такой стоп, и
        недогруженный каталог тихо стоит дорого: у части товаров нет
        `external_id`, а без variant_id рассылка закрывает позицию как «нет
        карточки в каталоге кабинета» — остаток туда не уедет никогда.

        И обязательный предел страниц. Kit молча игнорирует неизвестные
        параметры; перестань однажды действовать `page` — цикл `while True` не
        закончился бы вовсе, бесконечно копя дубли в памяти воркера.
        """
        result = []
        collected = 0
        self.last_truncated = False
        for page in range(1, MAX_ORDER_PAGES + 1):
            data = self._get("/v1/variants", params={"page": page, "per_page": 100})
            variants = data.get("variants", [])
            if not variants:
                return result
            result.extend(_parse_kit_variants(data))
            collected += len(variants)
            total = data.get("total_count")
            if isinstance(total, int) and collected >= total:
                return result
        # Выдачу оборвали мы, а не площадка: каталог неполон, и молчать об этом
        # нельзя — иначе огрызок примут за полный каталог.
        self.last_truncated = True
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

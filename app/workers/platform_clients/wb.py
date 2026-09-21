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

# Сколько истории лента вообще помнит. Спека `/api/v3/orders`: «возвращает
# информацию о сборочных заданиях, созданных НЕ БОЛЕЕ 3 МЕСЯЦЕВ НАЗАД»; для более
# старых там отдельный метод архивных заказов, которого мы не используем.
#
# Это не теория: замер 18.09 её и нащупал, только принял за особенность окна —
# `dateFrom=01.07` вернул НОЛЬ заказов, тогда как от 07.08 их было 244. То есть
# запрос от слишком старой даты не падает и не жалуется, а честно отвечает
# пустотой, и расчёт по такой дате рапортует «проведено 0, проблем нет» и ставит
# товару «актуализирован» — открывая трансляцию полного остатка по продажам,
# которых никто не видел. Ровно тот же исход, что в инциденте 18.09.
#
# Константу читает `recalc.collect_orders` и превращает слишком старую дату в
# ПРОБЛЕМУ расчёта. Здесь она объявлена потому, что это факт про ЭТУ площадку, и
# жить ему рядом с лентой; у Ozon и Kit такого подтверждённого предела у нас нет,
# и выдумывать его мы не станем — там проверки просто не будет.
ORDERS_HISTORY_DAYS = 90

# Сколько идентификаторов заказов влезает в один запрос статусов
# (`/api/v3/orders/status`). Предел площадки, а не наш выбор.
# Карточек за страницу каталога. По спеке WB у `cursor.limit` стоит
# `maximum: 100` — больше площадка всё равно не отдаст.
CATALOG_PAGE_SIZE = 100
# Страниц каталога за один вызов. Сто карточек на страницу, то есть потолок —
# двести тысяч позиций на кабинет; упёрлись в него — поднимаем `last_truncated`.
CATALOG_MAX_PAGES = 2000

STATUS_BATCH_IDS = 1000

# Сколько sku влезает в один запрос остатков (`/api/v3/stocks/{warehouseId}`).
# Тот же предел площадки, что и у статусов.
STOCKS_BATCH_SKUS = 1000


class WbClient(PlatformClient):
    name = "wb"
    # Остаток WB адресует sku, а sku у него — это баркод.
    stock_key = "barcode"
    # Насколько старую дату расчёта лента ещё способна покрыть (см. константу).
    # Спрашивает `recalc.collect_orders`; у клиентов без этого поля проверки нет.
    orders_history_days = ORDERS_HISTORY_DAYS

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
                # `str(None)` даёт непустую строку «None», и проверка `if not oid`
                # её пропускала. Такой заказ проводился и оседал в
                # `ProcessedOrder`, а дальше `get_cancelled_orders` падал на
                # `int("None")` ЕЩЁ ДО похода в сеть — отмены переставали
                # отслеживаться по ВСЕМУ кабинету (списанная единица не
                # возвращалась на ЦС), исключение уходило в задание, и через пять
                # опросов предохранитель гасил кабинет совсем. Само бы это не
                # рассосалось: запись живёт всё окно открытых заказов.
                raw_id = o.get("id")
                if raw_id is None:
                    continue
                oid = str(raw_id)
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
        # Нечисловой идентификатор отбрасываем, а не падаем на всей пачке: одна
        # испорченная запись не должна отключать отслеживание отмен по кабинету.
        int_ids = []
        for raw in order_ids:
            try:
                int_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        if not int_ids:
            return []
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
                # В ответе WB называет позицию обоими ключами сразу, а какой из
                # них настоящий — зависит от того, чем мы её адресовали. Кладём
                # оба непустых: дальше ищем совпадение по тому, чем слали.
                for key in ("sku", "chrtId"):
                    value = str((row or {}).get(key) or "")
                    if value and value != "0":
                        bad.add(value)
        return bad

    def _error_code(self, response) -> str:
        """Код ошибки из тела ответа, если он там есть."""
        try:
            payload = response.json()
        except Exception:                      # noqa: BLE001 — тело не разобралось
            return ""
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        return str(payload.get("code") or "") if isinstance(payload, dict) else ""

    @staticmethod
    def _chrt_id(item: StockPushItem) -> str:
        """chrtId позиции из строки каталога, если он там есть.

        Каталог WB складывает `external_id` как `nmID:chrtID` (см.
        `_parse_wb_cards`): карточка плюс размер. Отправке нужен именно правый
        кусок — идентификатор РАЗМЕРА.
        """
        raw = (item.external_id or "").split(":")
        tail = raw[-1].strip() if raw else ""
        return tail if tail.isdigit() and tail != "0" else ""

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

        АДРЕСУЕМ ПО chrtId, А НЕ ПО БАРКОДУ — и это главное изменение 20.09.
        В спеке WB (снимок dev.wildberries.ru от 10.09.2026) тело запроса — это
        `stocks[] {chrtId, amount}`, про `sku` там нет ни слова, зато заготовлен
        код ошибки `SKUUploadDisabled`: «Uploading stock is not allowed by 'sku'.
        Please use the 'chrtId' key». То есть загрузку по баркоду WB умеет
        отключать, и в день, когда он это сделает, остатки просто перестанут
        уходить. Отдельно в описании метода сказано: названия параметров не
        валидируются, неверное имя даёт 204 без обновления — то есть отказ может
        оказаться и вовсе беззвучным.

        Переход сделан так, чтобы не потерять то, что работает сегодня. chrtId
        берётся из каталога кабинета; позиции, для которых его нет (каталог не
        выгружен, карточка новая), уходят старым путём — баркодом. Если WB не
        принял chrtId (`409 NotFound`), позиция не закрывается, а переезжает в
        баркодную пачку: неверный chrtId в каталоге не должен останавливать
        отправку, которая до сих пор проходила.

        ОДИН НЕИЗВЕСТНЫЙ SKU РОНЯЕТ ВСЮ ПАЧКУ — второе, что нужно знать про этот
        метод. 20.09 на бою: в пачке из ста позиций один баркод WB на складе не
        знал, и он ответил `409 NotFound` — ни одна из остальных девяноста
        девяти не применилась. Повтор бессмыслен: неизвестным sku он и
        останется, а значит сотня живых карточек не получила бы свой остаток
        НИКОГДА. Поэтому виновников вынимаем из запроса и шлём остальное.
        """
        by_chrt = [i for i in items if self._chrt_id(i)]
        by_sku = [i for i in items if not self._chrt_id(i)]

        ok: list[str] = []
        errors: list[dict] = []

        if by_chrt:
            part_ok, part_errors, fallback = self._push_batch(
                warehouse_id, by_chrt, use_chrt=True)
            ok += part_ok
            errors += part_errors
            # Позиции, которых WB не знает ПО chrtId, пробуем старым ключом:
            # каталог мог протухнуть, а баркод до сих пор принимался.
            by_sku += fallback

        if by_sku:
            part_ok, part_errors, _ = self._push_batch(
                warehouse_id, by_sku, use_chrt=False)
            ok += part_ok
            errors += part_errors

        return {"ok": ok, "errors": errors}

    def _push_batch(self, warehouse_id: str, items: list[StockPushItem],
                    use_chrt: bool) -> tuple[list[str], list[dict], list[StockPushItem]]:
        """Одна пачка одним ключом. Возвращает (ушло, ошибки, вернуть баркодом).

        Третий список непустой только для chrtId-пачки: это позиции, которые WB
        по chrtId не узнал, и их стоит попробовать баркодом, прежде чем объявлять
        неразрешёнными.
        """
        def key_of(item: StockPushItem) -> str:
            return self._chrt_id(item) if use_chrt else item.barcode

        remaining = list(items)
        dropped: list[dict] = []
        fallback: list[StockPushItem] = []

        # Цикл, а не одна попытка: WB перечисляет неизвестные позиции в ответе,
        # но обещания назвать ВСЕ сразу он не давал. Предел по числу позиций —
        # каждый проход выкидывает хотя бы одну, иначе выходим сами.
        for _ in range(len(items) + 1):
            if not remaining:
                break
            if use_chrt:
                body = {"stocks": [{"chrtId": int(self._chrt_id(i)), "amount": i.quantity}
                                   for i in remaining]}
            else:
                body = {"stocks": [{"sku": i.barcode, "amount": i.quantity}
                                   for i in remaining]}

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

                # ВО ВСЕХ аварийных выходах ниже первым элементом идёт ПУСТОЙ
                # список отправленных. Раньше там стояло «то, что выбыло из
                # remaining» — и это был не успех, а ровно наоборот: выбыть из
                # remaining позиция может ТОЛЬКО одним способом, если её отклонила
                # площадка. Каждый запрос уходит целиком, частично успешных пачек
                # не бывает. В итоге отклонённый баркод возвращался и в `ok`, и в
                # `errors`, а рассылка проверяет `ok` раньше — запись закрывалась
                # как `sent` с временем отправки. Мы считали остаток доставленным,
                # в отчёт он не попадал ни как «не доехало», ни как «неизвестный
                # sku», и только сверка через полчаса спрашивала площадку и
                # получала «нет такого sku».
                if response is not None and self._error_code(response) == "SKUUploadDisabled":
                    # WB выключил загрузку по баркоду для этого кабинета. Повтор
                    # не поможет и ничего не изменит: нужен chrtId, то есть
                    # свежий каталог кабинета. Говорим это прямо, а не прячем за
                    # «не отправлено за 5 попыток».
                    return [], dropped + [
                        {"sku": i.barcode, "terminal": True,
                         "detail": "площадка больше не принимает остаток по баркоду "
                                   "(SKUUploadDisabled) — нужен chrtId, обновите "
                                   "каталог кабинета"}
                        for i in remaining
                    ], []

                bad = self._not_found_skus(response) if response is not None else set()
                bad &= {key_of(i) for i in remaining}
                if not bad:
                    # Забраковано что-то другое — разбирать это самим мы не
                    # беремся, отдаём как есть по всей оставшейся пачке.
                    return ([],
                            dropped + [{"detail": detail}], fallback)

                unknown = [i for i in remaining if key_of(i) in bad]
                if use_chrt:
                    fallback += unknown
                else:
                    for item in unknown:
                        dropped.append({
                            "sku": item.barcode, "terminal": True,
                            "detail": f"площадка не знает этот sku на складе {warehouse_id} "
                                      f"(409 NotFound) — остаток по нему не уедет, пока "
                                      f"карточки там нет",
                        })
                remaining = [i for i in remaining if key_of(i) not in bad]
                continue
            except requests.RequestException as e:
                return ([],
                        dropped + [{"detail": str(e)}], fallback)

            text = (resp.text or "").strip()
            if text:
                return ([], dropped + [
                    {"detail": f"HTTP {resp.status_code} с телом (ожидали пустое): {text[:300]}"},
                ], fallback)
            return [i.barcode for i in remaining], dropped, fallback

        # Сюда попадаем, только если WB забраковал ВСЕ позиции по очереди.
        return [], dropped, fallback

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
        # Сто, а не тысяча. По спеке WB (`swagger/02-items.yaml`, снимок
        # dev.wildberries.ru) у `cursor.limit` стоит `maximum: 100` — площадка
        # режет запрос до сотни сама, и именно поэтому ниже появился комментарий
        # «WB отдаёт меньше лимита за страницу». Следствие было в другом: предел
        # в 200 страниц покрывал не двести тысяч карточек, как задумано, а
        # двадцать тысяч, и признака обрыва клиент не поднимал вовсе. У карточек
        # за границей нет строки каталога, значит нет chrtId — остаток уходит
        # баркодом, и в день, когда WB включит `SKUUploadDisabled`, эти карточки
        # замолчат.
        limit = CATALOG_PAGE_SIZE
        cursor = {"limit": limit}
        prev_key = None
        self.last_truncated = False
        for _ in range(CATALOG_MAX_PAGES):
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
        else:
            # Цикл дошёл до предела, ни разу не встретив конца ленты: каталог
            # неполон. Молчать нельзя — огрызок примут за полный каталог.
            self.last_truncated = True
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

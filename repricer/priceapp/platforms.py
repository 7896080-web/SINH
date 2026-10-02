"""Клиенты площадок: каталог, проверка ключей, отправка цен.

Код КОПИЯ из sync_admin (`app/workers/platform_clients/`), а не импорт: общий
импорт связал бы накаты программ (`tests/test_isolation.py`). Взяты части,
проверенные там на живых кабинетах: выгрузка каталога (WB content-api v2,
Ozon /v3/product/list + /v3/product/info/list, Kit /v1/variants с total_count).

**Отправка цен вживую НЕ проверялась** — написана по документации:
  * WB — `POST discounts-prices-api.wildberries.ru/api/v2/upload/task`, цена на
    КАРТОЧКУ (nmID), не на размер: при разных ценах размеров уходит наибольшая.
    Токену нужна категория «Цены и скидки». Скидку карточки не трогаем —
    покупатель платит цену минус скидку, и это видно в кабинете WB.
  * Ozon — `POST /v1/product/import/prices` по offer_id (артикулу продавца);
    old_price и min_price не передаём.
  * Kit — метод цен в спеке не сверен: честный отказ, ничего не отправляется.
  * Lamoda — `POST /v2/nomenclatures-prices` по parentSku (карточке), в копейках,
    `force: false` (из акции товар не убираем). Сверено с OAS-файлом.

**Чтение текущих цен вживую НЕ проверялось** — тоже по документации:
  * WB — `GET discounts-prices-api.wildberries.ru/api/v2/list/goods/filter`
    (limit до 1000, offset): цена и скидка по КАРТОЧКЕ (nmID). `price` — до
    скидки, то же поле, что мы отправляем; `discountedPrice` — что платит
    покупатель.
  * Ozon — `POST /v5/product/info/prices` (cursor, limit до 1000) по offer_id:
    `price.price` — цена продажи.
  * Lamoda — `GET /v2/nomenclatures-sell-values`: `price` и `salePrice` (пока
    скидка действует) по parentSku, страна RU.
  * Kit — не читаем (`get_prices` бросает PlatformError).
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from priceapp.http_retry import with_retry

PLATFORMS = {"wb": "Wildberries", "ozon": "Ozon", "kit": "Яндекс KIT", "lamoda": "Lamoda"}

# Где умеем читать текущие цены (`get_prices`). У остальных метод честно
# отказывает; суточное обновление и кнопка их пропускают, называя причину.
READS_PRICES = {"wb", "ozon", "lamoda"}

# Какие ключи у кабинета: (имя поля, подпись).
CREDENTIAL_FIELDS = {
    "wb": [("token", "Токен (категории «Контент» и «Цены и скидки»)")],
    "ozon": [("client_id", "Client-Id"), ("api_key", "Api-Key")],
    "kit": [("token", "Токен")],
    "lamoda": [("client_id", "Client ID"), ("client_secret", "Client Secret"),
               ("seller_id", "Seller ID (идентификатор продавца)")],
}


@dataclass
class CatalogRow:
    external_id: str
    barcode: str
    article: str
    name: str
    size: str = ""


@dataclass
class PriceItem:
    barcode: str
    price: int
    external_id: str = ""
    article: str = ""


@dataclass
class CurrentPrice:
    price: int              # то же поле, что мы отправляем
    sale_price: int         # что платит покупатель (после скидки продавца)
    status: str = ""        # что площадка говорит о цене (Lamoda: OK/PROCESSING/ERROR/QUARANTINE)


class PlatformError(RuntimeError):
    pass


def _int_price(v) -> int | None:
    try:
        n = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return int(round(n)) if n > 0 else None


# --- Wildberries -----------------------------------------------------------------

WB_CONTENT = "https://content-api.wildberries.ru"
WB_PRICES = "https://discounts-prices-api.wildberries.ru"
WB_PAGE = 100            # по спеке WB у cursor.limit maximum: 100
WB_MAX_PAGES = 2000
WB_PRICE_PAGE = 1000      # limit у /api/v2/list/goods/filter — до 1000
WB_PRICE_MAX_PAGES = 500


class WbClient:
    platform = "wb"

    def __init__(self, token: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": token})
        self.last_truncated = False

    def _post(self, url: str, body: dict):
        def call():
            r = self.session.post(url, json=body, timeout=30)
            r.raise_for_status()
            return r.json() if r.content else {}
        return with_retry(call)

    def test_connection(self) -> tuple[bool, str]:
        """/ping обоих доменов: у каждого своя категория токена."""
        problems = []
        for host, what in ((WB_CONTENT, "Контент"), (WB_PRICES, "Цены и скидки")):
            try:
                r = self.session.get(f"{host}/ping", timeout=15)
                r.raise_for_status()
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                problems.append(f"«{what}»: {code}" + (" — нет этой категории у токена" if code == 401 else ""))
            except requests.RequestException as e:
                problems.append(f"«{what}»: нет связи ({e})")
        if problems:
            return False, "; ".join(problems)
        return True, "Токен действителен для «Контент» и «Цены и скидки»."

    def get_catalog(self) -> list[CatalogRow]:
        result, cursor, prev = [], {"limit": WB_PAGE}, None
        self.last_truncated = False
        for _ in range(WB_MAX_PAGES):
            data = self._post(f"{WB_CONTENT}/content/v2/get/cards/list",
                              {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
            cards = data.get("cards", data.get("data", {}).get("cards", []))
            result.extend(parse_wb_cards(data))
            c = data.get("cursor", {})
            key = (c.get("updatedAt"), c.get("nmID"))
            if not cards or not key[0] or not key[1] or key == prev:
                break
            prev = key
            cursor = {"limit": WB_PAGE, "updatedAt": key[0], "nmID": key[1]}
        else:
            self.last_truncated = True
        return result

    def price_key(self, item) -> str:
        """Чем адресуется цена строки каталога: у WB — nmID (цена на карточку)."""
        return (item.external_id or "").split(":")[0]

    def get_prices(self) -> dict[str, CurrentPrice]:
        out: dict[str, CurrentPrice] = {}
        self.last_truncated = False
        for page in range(WB_PRICE_MAX_PAGES):
            def call(offset=page * WB_PRICE_PAGE):
                r = self.session.get(f"{WB_PRICES}/api/v2/list/goods/filter",
                                     params={"limit": WB_PRICE_PAGE, "offset": offset}, timeout=30)
                r.raise_for_status()
                return r.json()
            data = with_retry(call)
            goods = (data.get("data") or {}).get("listGoods") or []
            if not goods:
                return out
            out.update(parse_wb_prices(data))
        self.last_truncated = True
        return out

    def push_prices(self, items: list[PriceItem]) -> dict:
        by_nm: dict[int, list[PriceItem]] = {}
        errors = []
        for it in items:
            nm = (it.external_id or "").split(":")[0]
            if not nm.isdigit():
                errors.append({"detail": "нет nmID — обновите каталог кабинета", "items": [it.barcode]})
                continue
            by_nm.setdefault(int(nm), []).append(it)
        nm_price = {nm: max(i.price for i in g) for nm, g in by_nm.items()}
        ok, sent = [], {}
        nms = list(nm_price)
        for start in range(0, len(nms), 1000):
            chunk = nms[start:start + 1000]
            body = {"data": [{"nmID": nm, "price": nm_price[nm]} for nm in chunk]}
            barcodes = [i.barcode for nm in chunk for i in by_nm[nm]]
            try:
                data = self._post(f"{WB_PRICES}/api/v2/upload/task", body)
            except requests.RequestException as e:
                errors.append({"detail": str(e), "items": barcodes})
                continue
            if isinstance(data, dict) and data.get("error"):
                errors.append({"detail": data.get("errorText") or "WB вернул error=true", "items": barcodes})
                continue
            for nm in chunk:
                for i in by_nm[nm]:
                    ok.append(i.barcode)
                    sent[i.barcode] = nm_price[nm]
        return {"ok": ok, "errors": errors, "sent_prices": sent}


def parse_wb_prices(data: dict) -> dict[str, CurrentPrice]:
    """nmID -> цена. Размеры с разной ценой (editableSizePrice) — наибольшая:
    отправляем мы тоже наибольшую по карточке."""
    out = {}
    for g in (data.get("data") or {}).get("listGoods") or []:
        pairs = [(_int_price(sz.get("price")), _int_price(sz.get("discountedPrice")))
                 for sz in g.get("sizes") or []]
        pairs = [(p, s or p) for p, s in pairs if p]
        if pairs and str(g.get("nmID", "")).isdigit():
            out[str(g["nmID"])] = CurrentPrice(*max(pairs))
    return out


def parse_wb_cards(data: dict) -> list[CatalogRow]:
    result = []
    for card in data.get("cards", data.get("data", {}).get("cards", [])):
        nm_id = str(card.get("nmID", ""))
        article = card.get("vendorCode", "")
        name = card.get("title") or card.get("subjectName") or article
        for size in card.get("sizes", []):
            sku_id = f"{nm_id}:{size.get('chrtID', '')}"
            size_name = str(size.get("techSize") or size.get("wbSize") or "").strip()
            for sku in size.get("skus", []):
                result.append(CatalogRow(sku_id, sku, article, name, size_name))
    return result


# --- Ozon ------------------------------------------------------------------------

OZON = "https://api-seller.ozon.ru"
OZON_PAGE = 1000
OZON_MAX_PAGES = 200


class OzonClient:
    platform = "ozon"

    def __init__(self, client_id: str, api_key: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"Client-Id": client_id, "Api-Key": api_key,
                                     "Content-Type": "application/json"})
        self.last_truncated = False

    def _post(self, path: str, body: dict):
        def call():
            r = self.session.post(f"{OZON}{path}", json=body, timeout=30)
            r.raise_for_status()
            return r.json()
        return with_retry(call)

    def test_connection(self) -> tuple[bool, str]:
        try:
            self._post("/v3/product/list", {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1})
            return True, "Соединение установлено, ключи действительны."
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            if code in (401, 403):
                return False, f"{code} — Client-Id/Api-Key недействительны."
            return False, f"Ошибка {code} при обращении к Ozon."
        except requests.RequestException as e:
            return False, f"Не удалось связаться с Ozon: {e}"

    def get_catalog(self) -> list[CatalogRow]:
        result, last_id = [], ""
        self.last_truncated = False
        for _ in range(OZON_MAX_PAGES):
            data = self._post("/v3/product/list", {"filter": {"visibility": "ALL"},
                                                   "last_id": last_id, "limit": OZON_PAGE})
            items = data.get("result", {}).get("items", [])
            if not items:
                break
            ids = [i["product_id"] for i in items if i.get("product_id")]
            if ids:
                result.extend(parse_ozon_info(self._post("/v3/product/info/list", {"product_id": ids})))
            last_id = data.get("result", {}).get("last_id", "")
            if not last_id:
                break
        else:
            self.last_truncated = True
        return result

    def price_key(self, item) -> str:
        """У Ozon цена — на offer_id (артикул продавца)."""
        return item.article or ""

    def get_prices(self) -> dict[str, CurrentPrice]:
        out: dict[str, CurrentPrice] = {}
        cursor = ""
        self.last_truncated = False
        for _ in range(OZON_MAX_PAGES):
            data = self._post("/v5/product/info/prices",
                              {"cursor": cursor, "filter": {"visibility": "ALL"}, "limit": OZON_PAGE})
            items = data.get("items") or []
            out.update(parse_ozon_prices(data))
            cursor = data.get("cursor") or ""
            if not items or not cursor:
                return out
        self.last_truncated = True
        return out

    def push_prices(self, items: list[PriceItem]) -> dict:
        ok, errors, sent = [], [], {}
        # Два SKU 1С на одном offer_id — уходит большая цена: меньшая могла бы
        # оказаться ниже пола у дорогого.
        by_offer: dict[str, list[PriceItem]] = {}
        for it in items:
            by_offer.setdefault(it.article or it.barcode, []).append(it)
        offers = list(by_offer)
        for start in range(0, len(offers), 1000):
            chunk = offers[start:start + 1000]
            price_of = {o: max(i.price for i in by_offer[o]) for o in chunk}
            body = {"prices": [{"offer_id": o, "price": str(price_of[o]), "currency_code": "RUB"}
                               for o in chunk]}
            try:
                resp = self._post("/v1/product/import/prices", body)
            except requests.RequestException as e:
                errors.append({"detail": str(e), "items": [i.barcode for o in chunk for i in by_offer[o]]})
                continue
            for res in resp.get("result", []):
                group = by_offer.get(res.get("offer_id"))
                if not group:
                    continue
                if res.get("updated"):
                    for i in group:
                        ok.append(i.barcode)
                        sent[i.barcode] = price_of[res.get("offer_id")]
                else:
                    errors.append({"detail": res.get("errors"), "items": [i.barcode for i in group]})
        return {"ok": ok, "errors": errors, "sent_prices": sent}


def parse_ozon_prices(data: dict) -> dict[str, CurrentPrice]:
    out = {}
    for it in data.get("items") or []:
        p = _int_price((it.get("price") or {}).get("price"))
        if p and it.get("offer_id"):
            out[str(it["offer_id"])] = CurrentPrice(p, p)
    return out


def parse_ozon_info(data: dict) -> list[CatalogRow]:
    result = []
    for item in data.get("items", data.get("result", {}).get("items", [])):
        barcodes = item.get("barcodes") or ([item["barcode"]] if item.get("barcode") else [])
        for b in [b for b in barcodes if b]:
            result.append(CatalogRow(str(item.get("id", "")), b, item.get("offer_id", ""),
                                     item.get("name", "")))
    return result


# --- Яндекс KIT ------------------------------------------------------------------

KIT = "https://api.kit.yandex.net"
KIT_MAX_PAGES = 200


class KitClient:
    platform = "kit"

    def __init__(self, token: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.last_truncated = False

    def _get(self, path: str, params=None):
        def call():
            r = self.session.get(f"{KIT}{path}", params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        return with_retry(call)

    def test_connection(self) -> tuple[bool, str]:
        try:
            self._get("/v1/warehouses", params={"status": "ACTIVE"})
            return True, "Соединение установлено, токен действителен."
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            return False, "401 — токен недействителен." if code == 401 else f"Ошибка {code} при обращении к Kit."
        except requests.RequestException as e:
            return False, f"Не удалось связаться с Kit: {e}"

    def get_catalog(self) -> list[CatalogRow]:
        result, collected = [], 0
        self.last_truncated = False
        for page in range(1, KIT_MAX_PAGES + 1):
            data = self._get("/v1/variants", params={"page": page, "per_page": 100})
            variants = data.get("variants", [])
            if not variants:
                return result
            result.extend(parse_kit_variants(data))
            collected += len(variants)
            total = data.get("total_count")
            if isinstance(total, int) and collected >= total:
                return result
        self.last_truncated = True
        return result

    def price_key(self, item) -> str:
        return item.external_id or ""

    def get_prices(self) -> dict[str, CurrentPrice]:
        raise PlatformError("Kit: чтение цен ещё не сверено со спекой API — текущие цены не загружены")

    def push_prices(self, items: list[PriceItem]) -> dict:
        """Метод цен Kit не сверен со спекой — цена не отправляется."""
        return {"ok": [], "sent_prices": {},
                "errors": [{"detail": "Kit: метод обновления цен ещё не сверен со спекой API — "
                                      "цена не отправлена", "items": [i.barcode for i in items]}]}


def parse_kit_variants(data: dict) -> list[CatalogRow]:
    result = []
    for v in data.get("variants", []):
        if v.get("barcode"):
            result.append(CatalogRow(str(v.get("id", "")), v["barcode"], v.get("sku", ""),
                                     v.get("name", "")))
    return result


# --- Lamoda ------------------------------------------------------------------------
#
# Lamoda Seller Partner API v2 — сверено с OAS-файлом спецификации
# (academy.lamoda.ru/upload/iblock/c17/5jpdwa8k7gqtz7v0hwwt56669um0kncn.yaml,
# снимок 02.10.2026). Вживую НЕ проверялось. Главное из спеки:
#   * токен — `POST /v2/auth-token` {grant_type: client_credentials, client_id,
#     client_secret} -> 201 {access_token, expires_in}; дальше `Authorization: Bearer`;
#   * `sellerId` ОБЯЗАТЕЛЕН в каждом запросе — третий ключ кабинета;
#   * суммы — в КОПЕЙКАХ (`amount` целое, `currency` RUB);
#   * каталог — `GET /v2/nomenclatures` (country обязателен, limit до 25, конец —
#     `meta.totalPages`): строка = размерный `sku` со своим `barcode`;
#   * текущие цены — `GET /v2/nomenclatures-sell-values` (без sku — весь каталог);
#   * цена ставится на КАРТОЧКУ (`parentSku`), а не на размер: как у WB, при разных
#     ценах размеров уходит наибольшая — меньшая могла бы оказаться ниже пола;
#   * `POST /v2/nomenclatures-prices` с `force: false` ВСЕГДА. Если цена нарушает
#     условия акции, `force: true` УБРАЛ БЫ товар из акции — это коммерческое
#     решение, его принимает человек в кабинете Lamoda, а не программа. Такая
#     позиция возвращается ошибкой со словами, что делать.

LAMODA = "https://public-api-seller.lamoda.ru/api"
LAMODA_COUNTRY = "RU"
LAMODA_PAGE = 25            # NomenclaturesLimitQuery: maximum 25
LAMODA_MAX_PAGES = 8000
LAMODA_PUSH_CHUNK = 100     # предел пачки спека не называет — берём осторожно


def lamoda_parent(external_id: str) -> str:
    """`external_id` строки каталога Lamoda — `parentSku:sku`."""
    return (external_id or "").split(":")[0]


class LamodaClient:
    platform = "lamoda"

    def __init__(self, client_id: str, client_secret: str, seller_id: str,
                 session: requests.Session | None = None):
        self.client_id, self.client_secret, self.seller_id = client_id, client_secret, str(seller_id)
        self.session = session or requests.Session()
        self.last_truncated = False
        self._token = None

    def get_token(self) -> str:
        r = self.session.post(f"{LAMODA}/v2/auth-token", timeout=30, json={
            "grant_type": "client_credentials", "client_id": self.client_id,
            "client_secret": self.client_secret})
        r.raise_for_status()
        token = (r.json() or {}).get("access_token")
        if not token:
            raise PlatformError("Lamoda: в ответе на запрос токена нет access_token")
        return token

    def _auth(self) -> dict:
        if self._token is None:
            self._token = self.get_token()
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict):
        def call():
            r = self.session.get(f"{LAMODA}{path}", params={"sellerId": self.seller_id, **params},
                                 headers=self._auth(), timeout=30)
            r.raise_for_status()
            return r.json()
        return with_retry(call)

    def _post(self, path: str, body: dict):
        def call():
            r = self.session.post(f"{LAMODA}{path}", json={"sellerId": self.seller_id, **body},
                                  headers=self._auth(), timeout=30)
            r.raise_for_status()
            return r.json() if r.content else {}
        return with_retry(call)

    def _pages(self, path: str, params: dict, key: str):
        """Все страницы списка. Конец — `meta.totalPages`, а не короткая страница."""
        self.last_truncated = False
        for page in range(1, LAMODA_MAX_PAGES + 1):
            data = self._get(path, {**params, "page": page, "limit": LAMODA_PAGE})
            yield data.get(key) or []
            total = (data.get("meta") or {}).get("totalPages")
            if not isinstance(total, int) or page >= total:
                return
        self.last_truncated = True

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.get_token()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            body = (e.response.text[:200] if e.response is not None else "")
            if code in (400, 401, 403):
                return False, f"Lamoda: {code} — Client ID / Client Secret не приняты. {body}".strip()
            return False, f"Lamoda: ошибка {code} при получении токена. {body}".strip()
        except requests.RequestException as e:
            return False, f"Lamoda: не удалось связаться ({e})"
        except PlatformError as e:
            return False, str(e)
        try:
            self._get("/v2/nomenclatures-sell-values", {"page": 1, "limit": 1})
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            return False, (f"Lamoda: токен получен, но запрос по Seller ID {self.seller_id} отклонён ({code}) — "
                           "проверьте Seller ID.")
        except requests.RequestException as e:
            return False, f"Lamoda: токен получен, запрос цен не прошёл ({e})"
        return True, "Токен получен, Seller ID принят — ключи действительны."

    def get_catalog(self) -> list[CatalogRow]:
        result = []
        for chunk in self._pages("/v2/nomenclatures", {"country": LAMODA_COUNTRY}, "nomenclatures"):
            result.extend(parse_lamoda_nomenclatures({"nomenclatures": chunk}))
        return result

    def price_key(self, item) -> str:
        return lamoda_parent(item.external_id)

    def get_prices(self) -> dict[str, CurrentPrice]:
        out: dict[str, CurrentPrice] = {}
        for chunk in self._pages("/v2/nomenclatures-sell-values", {}, "nomenclatures"):
            for parent, cur in parse_lamoda_sell_values({"nomenclatures": chunk}).items():
                if parent not in out or cur.price > out[parent].price:
                    out[parent] = cur
        return out

    def get_min_prices(self) -> dict[str, int]:
        """parentSku -> минимальная цена Lamoda, ₽. Два прохода: правила
        (`/v2/minimal-prices`) и категории товаров (`/v2/nomenclatures`)."""
        minimal = [m for chunk in self._pages("/v2/minimal-prices", {}, "minimalPrices") for m in chunk]
        truncated = self.last_truncated
        noms = [n for chunk in self._pages("/v2/nomenclatures", {"country": LAMODA_COUNTRY}, "nomenclatures")
                for n in chunk]
        self.last_truncated = truncated or self.last_truncated
        return parse_lamoda_min_prices(minimal, noms)

    def push_prices(self, items: list[PriceItem]) -> dict:
        by_parent: dict[str, list[PriceItem]] = {}
        errors = []
        for it in items:
            parent = lamoda_parent(it.external_id)
            if not parent:
                errors.append({"detail": "нет parentSku — обновите каталог кабинета", "items": [it.barcode]})
                continue
            by_parent.setdefault(parent, []).append(it)
        price_of = {p: max(i.price for i in g) for p, g in by_parent.items()}
        ok, sent = [], {}
        parents = list(price_of)
        for start in range(0, len(parents), LAMODA_PUSH_CHUNK):
            chunk = parents[start:start + LAMODA_PUSH_CHUNK]
            body = {"country": LAMODA_COUNTRY, "force": False,
                    "prices": [{"parentSku": p, "price": {"amount": price_of[p] * 100, "currency": "RUB"}}
                               for p in chunk]}
            try:
                resp = self._post("/v2/nomenclatures-prices", body)
            except requests.RequestException as e:
                errors.append({"detail": str(e), "items": [i.barcode for p in chunk for i in by_parent[p]]})
                continue
            failed, unattributed = lamoda_push_failures(resp, chunk)
            if unattributed:
                # Ошибку без parentSku не к кому привязать — считаем неуспешной всю
                # пачку: повтор той же цены безвреден, а «отправлено» неправдой — нет.
                failed.update({p: unattributed for p in chunk if p not in failed})
            for p in chunk:
                if p in failed:
                    errors.append({"detail": failed[p], "items": [i.barcode for i in by_parent[p]]})
                else:
                    for i in by_parent[p]:
                        ok.append(i.barcode)
                        sent[i.barcode] = price_of[p]
        return {"ok": ok, "errors": errors, "sent_prices": sent}


def _lamoda_rub(price: dict | None) -> int | None:
    if not isinstance(price, dict) or price.get("currency") not in (None, "RUB"):
        return None
    amount = price.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        return None
    return int(round(amount / 100))


def parse_lamoda_nomenclatures(data: dict) -> list[CatalogRow]:
    result = []
    for n in data.get("nomenclatures") or []:
        if not n.get("barcode") or not n.get("parentSku"):
            continue
        result.append(CatalogRow(f"{n['parentSku']}:{n.get('sku') or ''}", n["barcode"],
                                 n.get("externalParentSku") or n.get("externalSku") or "",
                                 n.get("name") or "", n.get("externalSize") or ""))
    return result


def parse_lamoda_sell_values(data: dict, now=None) -> dict[str, CurrentPrice]:
    """parentSku -> цена (RU). Цена продажи — `salePrice`, только пока скидка
    действует (`saleStart`..`saleEnd`); без дат — действует."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)

    def when(v):
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    out = {}
    for n in data.get("nomenclatures") or []:
        for sv in n.get("sellValues") or []:
            if sv.get("country") != LAMODA_COUNTRY:
                continue
            price = _lamoda_rub(sv.get("price"))
            if not price or not n.get("parentSku"):
                continue
            sale = _lamoda_rub(sv.get("salePrice"))
            start, end = when(sv.get("saleStart")), when(sv.get("saleEnd"))
            if sale and ((start and now < start) or (end and now > end)):
                sale = None
            cur = CurrentPrice(price, sale or price, str(sv.get("priceUpdateStatus") or ""))
            prev = out.get(n["parentSku"])
            if prev is None or cur.price > prev.price:
                status = _worse_status(prev.status if prev else "", cur.status)
                out[n["parentSku"]] = CurrentPrice(cur.price, cur.sale_price, status)
            else:
                prev.status = _worse_status(prev.status, cur.status)
    return out


# Худший из статусов размеров карточки: карантин у одного размера — карантин карточки.
_STATUS_RANK = {"": 0, "OK": 1, "PROCESSING": 2, "ERROR": 3, "QUARANTINE": 4}


def _worse_status(a: str, b: str) -> str:
    return a if _STATUS_RANK.get(a, 0) >= _STATUS_RANK.get(b, 0) else b


def parse_lamoda_min_prices(minimal: list[dict], nomenclatures: list[dict]) -> dict[str, int]:
    """parentSku -> минимальная цена Lamoda, ₽ (RU).

    Правила заданы ПО КАТЕГОРИИ (`categoryName`/`subcategoryName`, иногда с
    `brand`), а не по товару. Сопоставляем с уровнями 1 и 2 категорий товара на
    английском (`categoryLevels`, language EN) — в примерах спеки и те и другие
    записаны кодами вроде FOOTWEAR/ACCESSORIES. Это ДОГАДКА по схеме, поэтому
    минимальная цена в программе только предупреждает и ничего не блокирует.
    Подходит несколько правил — берём наибольшую: предупредить лишний раз
    дешевле, чем промолчать."""
    rules = []
    for m in minimal:
        rub = next((_lamoda_rub(p.get("price")) for p in m.get("prices") or []
                    if p.get("country") == LAMODA_COUNTRY), None)
        if rub:
            rules.append((str(m.get("categoryName") or "").upper(), str(m.get("subcategoryName") or "").upper(),
                          (m.get("brand") or "").strip().lower(), rub))
    out: dict[str, int] = {}
    for n in nomenclatures:
        parent = n.get("parentSku")
        if not parent:
            continue
        levels = {lv.get("level"): str(lv.get("name") or "").upper()
                  for lv in n.get("categoryLevels") or [] if lv.get("language") == "EN"}
        brand = (n.get("brand") or "").strip().lower()
        hits = [rub for cat, sub, rb, rub in rules
                if cat == levels.get(1) and sub == levels.get(2) and (not rb or rb == brand)]
        if hits:
            out[parent] = max(hits + [out.get(parent, 0)])
    return out


def lamoda_push_failures(resp: dict, chunk: list[str]) -> tuple[dict[str, str], str]:
    """({parentSku: причина}, причина без адресата). Ошибка приходит с `sku`
    (может быть пустым), отказ по акции — с `parentSku` в `fraudValidationResults`."""
    failed, unattributed = {}, ""
    known = set(chunk)
    for e in resp.get("errors") or []:
        sku = str(e.get("sku") or "")
        text = f"{e.get('code', '')}: {'; '.join(e.get('messages') or [])}".strip(": ")
        parent = sku if sku in known else next((p for p in known if sku and sku.startswith(p)), "")
        if parent:
            failed[parent] = f"Lamoda отклонила цену — {text}"
        else:
            unattributed = f"Lamoda: ошибка без указания товара — {text}"
    for fr in resp.get("fraudValidationResults") or []:
        p = fr.get("parentSku")
        if p in known:
            how = ("изменить цену можно только после окончания акции" if fr.get("status") == "RESTRICTION"
                   else "чтобы применить, уберите товар из акции в кабинете Lamoda")
            names = ", ".join(str(x.get("name")) for x in fr.get("promotions") or [] if x.get("name"))
            failed[p] = (f"Lamoda: цена нарушает условия акции{' «' + names + '»' if names else ''} "
                         f"({fr.get('invalidValue')}) — {how}")
    if not failed and not unattributed and resp.get("errorCount"):
        unattributed = f"Lamoda: ошибок {resp.get('errorCount')}, но без подробностей"
    return failed, unattributed


def build_client(platform: str, creds: dict):
    missing = [f for f, _ in CREDENTIAL_FIELDS[platform] if not creds.get(f)]
    if missing:
        raise PlatformError(f"не заданы ключи: {', '.join(missing)}")
    if platform == "wb":
        return WbClient(creds["token"])
    if platform == "ozon":
        return OzonClient(creds["client_id"], creds["api_key"])
    if platform == "kit":
        return KitClient(creds["token"])
    if platform == "lamoda":
        return LamodaClient(creds["client_id"], creds["client_secret"], creds["seller_id"])
    raise PlatformError(platform)

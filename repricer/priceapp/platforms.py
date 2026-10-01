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
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from priceapp.http_retry import with_retry

PLATFORMS = {"wb": "Wildberries", "ozon": "Ozon", "kit": "Яндекс KIT"}

# Какие ключи у кабинета: (имя поля, подпись).
CREDENTIAL_FIELDS = {
    "wb": [("token", "Токен (категории «Контент» и «Цены и скидки»)")],
    "ozon": [("client_id", "Client-Id"), ("api_key", "Api-Key")],
    "kit": [("token", "Токен")],
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


class PlatformError(RuntimeError):
    pass


# --- Wildberries -----------------------------------------------------------------

WB_CONTENT = "https://content-api.wildberries.ru"
WB_PRICES = "https://discounts-prices-api.wildberries.ru"
WB_PAGE = 100            # по спеке WB у cursor.limit maximum: 100
WB_MAX_PAGES = 2000


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
    raise PlatformError(platform)

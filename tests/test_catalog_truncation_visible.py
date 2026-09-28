"""Обрезанная выгрузка каталога не должна выглядеть успешной.

Клиенты площадок поднимают `last_truncated`, когда выдачу оборвал наш защитный
предел страниц, а не конец данных, — но при загрузке каталога этот признак до сих
пор никто не спрашивал. Снимок при этом выглядит свежим: у попавших карточек
обновлён `fetched_at`, счётчик показывает «загружено N».

Цена молчания отложенная и потому особенно незаметная. По снимку каталога
считаются ключи отправки: у WB это chrtId (нет его — остаток уходит баркодом, а
WB умеет приём баркода отключить), у Kit — variant_id (нет — позиция не уедет
вовсе и закроется терминально). То есть неполный каталог оборачивается не
ошибкой, а тихо не уехавшим остатком.
"""
from app.models import Platform
from app.workers.catalog_sync import load_platform_catalog
from app.workers.platform_clients.base import CatalogItem
from app.workers.platform_clients.kit import KitClient
from app.workers.platform_clients.ozon import OzonClient
from app.workers.platform_clients.wb import WbClient
from tests.factories import make_account


class Client:
    def __init__(self, truncated):
        self.last_truncated = truncated

    def get_catalog_items(self):
        return [CatalogItem(barcode="b1", external_id="100:1", article="A", name="Товар")]


def test_a_complete_load_is_not_flagged(db):
    account = make_account(db, Platform.wb)

    stats = load_platform_catalog(db, Client(False), account)

    assert stats["truncated"] is False


def test_a_truncated_load_is_flagged(db):
    account = make_account(db, Platform.wb)

    stats = load_platform_catalog(db, Client(True), account)

    assert stats["truncated"] is True


def test_the_page_says_it_was_cut(logged_in_client, web_db, monkeypatch):
    """Зелёное «загружено N карточек» на обрезанной выгрузке — худший вид
    молчания: человек нажал кнопку ровно затем, чтобы каталог стал полным, и
    уходит в уверенности, что стал."""
    from app.models import PlatformAccount

    account = PlatformAccount(platform="wb", name="Кабинет", warehouse_id="wh")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)

    monkeypatch.setattr("app.routers.mapping.build_client",
                        lambda db, aid: Client(True))

    # Текст берём из ответа САМОГО POST: он идёт с редиректом на страницу, и
    # флеш снимается именно ею. Отдельный GET следом приходит уже пустым.
    page = logged_in_client.post(f"/mapping/load-catalog/{account.id}").text

    assert "ОБОРВАНА" in page


def test_a_complete_load_stays_green(logged_in_client, web_db, monkeypatch):
    """Обычный путь не задет: предупреждение на каждой загрузке обесценило бы
    его так же верно, как и молчание."""
    from app.models import PlatformAccount

    account = PlatformAccount(platform="wb", name="Кабинет", warehouse_id="wh")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)

    monkeypatch.setattr("app.routers.mapping.build_client",
                        lambda db, aid: Client(False))

    page = logged_in_client.post(f"/mapping/load-catalog/{account.id}").text

    assert "ОБОРВАНА" not in page


# ------------------------------- и признак обязан поднимать КАЖДЫЙ клиент

# Само поднятие `last_truncated` — обязанность клиента, и обязанность общая.
#
# У Ozon этой ветки не было вовсе: `get_catalog_items` крутил `range(50)` без
# `else`, то есть упёршись в предел, отдавал огрызок как полный каталог. Всё,
# что проверено выше, при этом не срабатывало: `stats["truncated"]` оставался
# ложным, предупреждения не было ни в логе, ни на странице.
#
# Цена у Ozon вдобавок выше, чем у соседей. Остаток туда адресуется АРТИКУЛОМ из
# каталога: позиция, не попавшая в огрызок, ключа отправки не получает, рассылка
# закрывает её как «нет карточки», а отчёт относит к `unknown_sku` — «продавать
# нечего, оверселла не будет». Для не выгруженной карточки это неправда: на
# площадке она есть и продаётся.


def test_ozon_says_the_catalogue_was_cut():
    """Лента, которая не кончается: `last_id` сдвигается всегда."""
    n = [0]

    def fake_post(path, body=None, **kw):
        if "product/list" in path:
            n[0] += 1
            return {"result": {"items": [{"product_id": n[0]}], "last_id": f"c{n[0]}"}}
        return {"items": [{"id": n[0], "barcodes": [f"b{n[0]}"], "offer_id": "A"}]}

    c = OzonClient(client_id="c", api_key="k")
    c._post = fake_post

    items = c.get_catalog_items()

    assert c.last_truncated is True
    assert items, "огрызок всё равно возвращаем — он лучше, чем ничего"


def test_ozon_does_not_cry_wolf_on_a_complete_catalogue():
    def fake_post(path, body=None, **kw):
        if "product/list" in path:
            return {"result": {"items": [{"product_id": 1}], "last_id": ""}}
        return {"items": [{"id": 1, "barcodes": ["b1"], "offer_id": "A"}]}

    c = OzonClient(client_id="c", api_key="k")
    c._post = fake_post

    c.get_catalog_items()

    assert c.last_truncated is False


def test_wb_says_the_catalogue_was_cut():
    n = [0]

    def fake(path, body=None, **kw):
        n[0] += 1
        return {"cards": [{"nmID": n[0], "sizes": [{"chrtID": n[0], "skus": [f"b{n[0]}"]}]}],
                "cursor": {"updatedAt": f"2026-09-{n[0] % 28 + 1:02d}", "nmID": n[0]}}

    c = WbClient(token="t", warehouse_id="wh")
    c._post_content = fake

    c.get_catalog_items()

    assert c.last_truncated is True


class _Headers:
    """Сессия-пустышка: клиенту Kit нужен только `headers` при сборке."""
    headers: dict = {}


def test_kit_says_the_catalogue_was_cut():
    n = [0]

    def fake(path, params=None):
        n[0] += 1
        return {"variants": [{"id": n[0], "barcodes": [f"b{n[0]}"]}], "total_count": 10 ** 9}

    c = KitClient(token="t", session=_Headers())
    c._get = fake

    c.get_catalog_items()

    assert c.last_truncated is True

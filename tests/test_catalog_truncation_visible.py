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

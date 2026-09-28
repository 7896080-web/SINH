from app.models import Barcode, Product, PlatformCatalogItem, MappingConflict, Platform
from app.workers.catalog_sync import load_platform_catalog
from app.workers.platform_clients.base import CatalogItem
from app.workers.platform_clients.wb import _parse_wb_cards
from app.workers.platform_clients.ozon import _parse_ozon_product_info
from app.workers.platform_clients.kit import _parse_kit_variants
from tests.factories import make_account


class FakeCatalogClient:
    def __init__(self, items):
        self._items = items

    def get_catalog_items(self):
        return self._items


def test_load_platform_catalog_saves_snapshot(db):
    account = make_account(db, platform=Platform.wb)
    client = FakeCatalogClient([
        CatalogItem(external_id="e1", barcode="111", article="A1", name="Товар 1"),
    ])
    stats = load_platform_catalog(db, client, account)

    assert stats["fetched"] == 1
    row = db.query(PlatformCatalogItem).filter(PlatformCatalogItem.account_id == account.id).first()
    assert row.barcode == "111"
    assert row.article == "A1"
    assert row.name == "Товар 1"


def test_load_platform_catalog_marks_already_mapped(db):
    account = make_account(db, platform=Platform.wb)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=1))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()

    client = FakeCatalogClient([CatalogItem(external_id="e1", barcode="111", article="A1", name="Товар")])
    stats = load_platform_catalog(db, client, account)

    assert stats["already_mapped"] == 1
    assert stats["new_conflicts"] == 0
    assert db.query(MappingConflict).count() == 0


def test_load_platform_catalog_registers_conflict_for_unknown_barcode(db):
    account = make_account(db, platform=Platform.ozon)
    client = FakeCatalogClient([CatalogItem(external_id="e1", barcode="999", article="A9", name="Неизвестный")])
    stats = load_platform_catalog(db, client, account)

    assert stats["new_conflicts"] == 1
    conflict = db.query(MappingConflict).filter(MappingConflict.barcode == "999").first()
    assert conflict is not None
    assert conflict.account_id == account.id
    # НОЛЬ, а не единица: `attempts` считает, сколько заказов по этому баркоду
    # не удалось разнести, и здесь их не было ни одного — строку завела выгрузка
    # каталога, чтобы конфликт стало видно ДО первого заказа. Единица была
    # прямой неправдой: отчёт складывает `attempts`, печатает сумму как «заказов
    # по ним N» и обещает «остаток завышен ровно на эти продажи». Счётчик
    # поднимет `resolve_barcode`, когда заказ действительно придёт.
    assert conflict.attempts == 0


def test_load_platform_catalog_updates_existing_conflict_not_duplicates(db):
    account = make_account(db, platform=Platform.ozon)
    client = FakeCatalogClient([CatalogItem(external_id="e1", barcode="999", article="A9", name="Товар")])
    load_platform_catalog(db, client, account)
    stats2 = load_platform_catalog(db, client, account)

    assert stats2["known_conflicts"] == 1
    assert db.query(MappingConflict).filter(MappingConflict.barcode == "999").count() == 1


def test_load_platform_catalog_skips_items_without_barcode(db):
    account = make_account(db, platform=Platform.kit)
    client = FakeCatalogClient([CatalogItem(external_id="e1", barcode="", article="A9", name="Без баркода")])
    stats = load_platform_catalog(db, client, account)

    assert stats["no_barcode"] == 1
    assert db.query(PlatformCatalogItem).count() == 0


def test_load_platform_catalog_upserts_on_second_call(db):
    account = make_account(db, platform=Platform.wb)
    client1 = FakeCatalogClient([CatalogItem(external_id="e1", barcode="111", article="A1", name="Старое имя")])
    load_platform_catalog(db, client1, account)

    client2 = FakeCatalogClient([CatalogItem(external_id="e1", barcode="111", article="A1", name="Новое имя")])
    load_platform_catalog(db, client2, account)

    assert db.query(PlatformCatalogItem).filter(PlatformCatalogItem.account_id == account.id).count() == 1
    row = db.query(PlatformCatalogItem).first()
    assert row.name == "Новое имя"


def test_load_platform_catalog_same_barcode_different_wb_cabinets_independent(db):
    """Разные кабинеты WB — разные снимки каталога, конфликт в одном
    кабинете не считается решённым конфликтом в другом."""
    wb1 = make_account(db, name="ИП Яворская")
    wb2 = make_account(db, name="ИП Ребрик")

    client = FakeCatalogClient([CatalogItem(external_id="e1", barcode="555", article="A5", name="Товар")])
    load_platform_catalog(db, client, wb1)
    load_platform_catalog(db, client, wb2)

    assert db.query(MappingConflict).filter(MappingConflict.barcode == "555").count() == 2
    assert db.query(PlatformCatalogItem).filter(PlatformCatalogItem.barcode == "555").count() == 2


def test_load_platform_catalog_multiple_barcodes_per_external_id(db):
    """WB-случай: одна карточка (общий external_id/nmID) даёт несколько
    баркодов (размеров). Каждый баркод — отдельная строка каталога; загрузка
    не падает на уникальном ограничении (баг был при ключе по external_id)."""
    account = make_account(db, name="ИП Караман")
    client = FakeCatalogClient([
        CatalogItem(external_id="nm-1", barcode="111", article="ART-1", name="Куртка"),
        CatalogItem(external_id="nm-1", barcode="222", article="ART-1", name="Куртка"),
        CatalogItem(external_id="nm-1", barcode="333", article="ART-1", name="Куртка"),
    ])
    stats = load_platform_catalog(db, client, account)

    assert stats["fetched"] == 3
    rows = db.query(PlatformCatalogItem).filter(PlatformCatalogItem.account_id == account.id).all()
    assert {r.barcode for r in rows} == {"111", "222", "333"}
    assert all(r.external_id == "nm-1" for r in rows)


def test_load_platform_catalog_reload_dedupes_by_barcode(db):
    """Повторная загрузка того же кабинета не плодит дубли — апсерт по баркоду."""
    account = make_account(db, name="ИП Караман")
    client = FakeCatalogClient([
        CatalogItem(external_id="nm-1", barcode="111", article="ART-1", name="Куртка"),
        CatalogItem(external_id="nm-1", barcode="222", article="ART-1", name="Куртка"),
    ])
    load_platform_catalog(db, client, account)
    load_platform_catalog(db, client, account)

    assert db.query(PlatformCatalogItem).filter(PlatformCatalogItem.account_id == account.id).count() == 2


# --- Парсинг ответов конкретных площадок (без сети, на примерах из документации) ---

def test_parse_wb_cards():
    # external_id = размер-цвет SKU = nmID:chrtID (пул принадлежит размер-цвету,
    # не карточке). Два баркода ОДНОГО размера (chrtID) — его пул.
    data = {"cards": [
        {
            "nmID": 12345, "vendorCode": "ART-1", "title": "Кроссовки", "subjectName": "Обувь",
            "sizes": [
                {"chrtID": 777, "techSize": "42", "skus": ["1111111111111", "2222222222222"]},
                {"chrtID": 888, "techSize": "43", "skus": ["3333333333333"]},
            ],
        },
    ]}
    items = _parse_wb_cards(data)
    assert len(items) == 3
    # первый размер — два баркода в одном пуле (общий external_id)
    assert items[0].external_id == "12345:777"
    assert items[1].external_id == "12345:777"
    # другой размер — ДРУГОЙ external_id (не тот же пул)
    assert items[2].external_id == "12345:888"
    assert items[0].name == "Кроссовки"  # title, а не subjectName


def test_parse_wb_cards_legacy_nested_shape():
    # Обратная совместимость со старым вложенным видом data.cards.
    data = {"data": {"cards": [
        {"nmID": 9, "vendorCode": "A", "title": "X",
         "sizes": [{"chrtID": 555, "skus": ["999"]}]},
    ]}}
    items = _parse_wb_cards(data)
    assert len(items) == 1
    assert items[0].barcode == "999"
    assert items[0].external_id == "9:555"


def test_parse_ozon_product_info():
    # /v3/product/info/list: баркоды списком `barcodes`. Все баркоды одного
    # product_id — его пул размер-цвета (одна строка на баркод, общий external_id).
    data = {"items": [
        {"id": 123, "name": "Товар", "offer_id": "OFR-1", "barcodes": ["4600000000001", "4600000000009"]},
        {"id": 124, "name": "Без баркода", "offer_id": "OFR-2", "barcodes": []},
        {"id": 125, "name": "Legacy", "offer_id": "OFR-3", "barcode": "4600000000002"},
    ]}
    items = _parse_ozon_product_info(data)
    assert len(items) == 3  # два баркода товара 123 + один legacy
    pool_123 = [i for i in items if i.external_id == "123"]
    assert {i.barcode for i in pool_123} == {"4600000000001", "4600000000009"}
    assert all(i.article == "OFR-1" for i in pool_123)
    # устаревший singular `barcode` тоже поддержан
    assert any(i.barcode == "4600000000002" for i in items)


def test_parse_kit_variants():
    # external_id = id варианта (размер-цвет SKU); разные варианты — разные пулы.
    data = {"variants": [
        {"id": "v-1", "sku": "SKU-1", "barcode": "1234567890123", "name": "Куртка"},
        {"id": "v-2", "sku": "SKU-2", "barcode": "1234567890124", "name": "Куртка"},
        {"id": "v-3", "sku": "SKU-3", "name": "Без баркода"},
    ]}
    items = _parse_kit_variants(data)
    assert len(items) == 2  # без баркода пропущен
    assert items[0].barcode == "1234567890123"
    assert items[0].external_id == "v-1"
    assert items[1].external_id == "v-2"


def test_wb_get_catalog_items_paginates_until_cursor_stops(monkeypatch):
    """Пагинация v2: листаем, пока приходят карточки и курсор сдвигается — даже
    если страница вернула меньше лимита (иначе каталог WB не догружался и часть
    товаров не давала предложений)."""
    from app.workers.platform_clients.wb import WbClient
    client = WbClient(token="t", warehouse_id="w")

    pages = [
        {"cards": [{"nmID": 1, "vendorCode": "A", "title": "T1",
                    "sizes": [{"chrtID": 11, "skus": ["bc1"]}]}],
         "cursor": {"updatedAt": "u1", "nmID": 1, "total": 1}},   # меньше лимита, но НЕ конец
        {"cards": [{"nmID": 2, "vendorCode": "B", "title": "T2",
                    "sizes": [{"chrtID": 22, "skus": ["bc2"]}]}],
         "cursor": {"updatedAt": "u2", "nmID": 2, "total": 1}},
        {"cards": [], "cursor": {"updatedAt": "u2", "nmID": 2, "total": 0}},  # пусто — конец
    ]
    calls = {"i": 0}

    def fake_post_content(path, body):
        i = calls["i"]; calls["i"] += 1
        return pages[min(i, len(pages) - 1)]

    monkeypatch.setattr(client, "_post_content", fake_post_content)

    items = client.get_catalog_items()
    assert {it.barcode for it in items} == {"bc1", "bc2"}  # обе страницы выгружены
    assert calls["i"] == 3                                  # дошли до пустой страницы


def test_wb_get_catalog_items_stops_if_cursor_not_advancing(monkeypatch):
    """Защита от зацикливания: если курсор не сдвигается — стоп."""
    from app.workers.platform_clients.wb import WbClient
    client = WbClient(token="t", warehouse_id="w")
    same = {"cards": [{"nmID": 1, "vendorCode": "A", "title": "T",
                       "sizes": [{"chrtID": 11, "skus": ["bc1"]}]}],
            "cursor": {"updatedAt": "u", "nmID": 1, "total": 1}}
    calls = {"i": 0}

    def fake(path, body):
        calls["i"] += 1
        return same  # курсор всегда (u,1) — не сдвигается

    monkeypatch.setattr(client, "_post_content", fake)
    items = client.get_catalog_items()
    assert calls["i"] == 2                       # вторая страница с тем же курсором → стоп
    assert {it.barcode for it in items} == {"bc1"}


def test_load_platform_catalog_pool_match_adds_alternate_barcode(db):
    """Сопоставление по пулу размер-цвета: баркод не в 1С, но сосед по external_id
    уже сопоставлен → регистрируем альтернативный баркод того же uid, без конфликта."""
    account = make_account(db, platform=Platform.wb)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=1))
    db.add(Barcode(barcode="bcA", uid_1c="u1"))  # один баркод размер-цвета уже в 1С
    db.commit()

    client = FakeCatalogClient([
        CatalogItem(external_id="X", barcode="bcA", article="A1", name="N"),  # в 1С
        CatalogItem(external_id="X", barcode="bcB", article="A1", name="N"),  # новый, тот же размер-цвет
    ])
    stats = load_platform_catalog(db, client, account)

    b = db.query(Barcode).filter(Barcode.barcode == "bcB").first()
    assert b is not None and b.uid_1c == "u1"                 # альтернативный баркод того же uid
    assert db.query(MappingConflict).filter(MappingConflict.barcode == "bcB").count() == 0
    assert stats["pool_matched"] == 1
    assert stats["new_conflicts"] == 0


def test_load_platform_catalog_pool_ambiguous_not_merged(db):
    """Пул не объединяется с другими SKU: если соседи по external_id ведут к РАЗНЫМ
    uid — не мержим, оставляем конфликт на ручной разбор."""
    account = make_account(db, platform=Platform.wb)
    db.add(Barcode(barcode="bc1", uid_1c="u1"))
    db.add(Barcode(barcode="bc2", uid_1c="u2"))  # разные uid в одном external_id
    db.commit()

    client = FakeCatalogClient([
        CatalogItem(external_id="X", barcode="bc1", article="A", name="N"),
        CatalogItem(external_id="X", barcode="bc2", article="A", name="N"),
        CatalogItem(external_id="X", barcode="bc3", article="A", name="N"),  # новый
    ])
    stats = load_platform_catalog(db, client, account)

    assert db.query(Barcode).filter(Barcode.barcode == "bc3").first() is None  # НЕ смаппился
    assert db.query(MappingConflict).filter(MappingConflict.barcode == "bc3").count() == 1
    assert stats["pool_matched"] == 0

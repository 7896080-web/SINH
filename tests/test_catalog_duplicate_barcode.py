"""Повтор баркода в выгрузке площадки не должен ронять загрузку каталога.

Автопривязка заводит незнакомый баркод, если соседи по карточке ведут к одному
товару 1С. Проверка «нет ли уже такого» идёт запросом, а сессия живёт с
`autoflush=False`: первый `db.add` до базы ещё не дошёл, и второй такой же
баркод в той же выгрузке уходил вторым INSERT. На коммите —
`UNIQUE constraint failed: barcodes.barcode`, и падала ВСЯ загрузка каталога.

Падала бы она и каждый следующий раз, пока площадка отдаёт ту же выгрузку, а
каталог тем временем молча устаревает — по нему считаются chrtId для WB и
variant_id для Kit, то есть ключи, КОТОРЫМИ УХОДИТ ОСТАТОК.
"""
from app.models import Barcode, Platform, PlatformCatalogItem, Product
from app.workers.catalog_sync import load_platform_catalog
from app.workers.platform_clients.base import CatalogItem
from tests.factories import make_account


class FakeClient:
    def __init__(self, items):
        self._items = items

    def get_catalog_items(self):
        return list(self._items)


def _known(db):
    account = make_account(db, Platform.wb)
    db.add(Product(uid_1c="u1", article="A", name="Товар", stock_on_hand=1))
    db.add(Barcode(barcode="b-known", uid_1c="u1"))
    db.commit()
    return account


def _item(barcode, external_id="100:200"):
    return CatalogItem(barcode=barcode, external_id=external_id, article="A", name="Товар")


def test_a_duplicate_does_not_break_the_load(db):
    account = _known(db)

    stats = load_platform_catalog(db, FakeClient([
        _item("b-known"), _item("b-new"), _item("b-new")]), account)

    assert stats["pool_matched"] == 1, "баркод заводится один раз"
    assert db.query(Barcode).count() == 2


def test_the_rest_of_the_catalogue_still_loads(db):
    """Главное последствие было не в самой строке, а в том, что с ней пропадал
    весь каталог кабинета."""
    account = _known(db)

    load_platform_catalog(db, FakeClient([
        _item("b-known"), _item("b-dup"), _item("b-dup"), _item("b-tail")]), account)

    saved = {row.barcode for row in db.query(PlatformCatalogItem).all()}
    assert saved == {"b-known", "b-dup", "b-tail"}


def test_a_barcode_already_in_the_base_is_untouched(db):
    """Контроль: обычный путь не задет — известный баркод по-прежнему считается
    сопоставленным, а не заводится заново."""
    account = _known(db)

    stats = load_platform_catalog(db, FakeClient([_item("b-known")]), account)

    assert stats["already_mapped"] == 1
    assert db.query(Barcode).count() == 1

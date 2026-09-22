"""Каталог поднимает пары, закрытые из-за его же отсутствия.

Поймано 22.09 на живом кабинете КИТ: карточку завели, трансляцию включили,
расчёт поставил пару в очередь — а каталог кабинета выгружается раз в сутки и
про новую карточку ещё не знал. `variant_id` нет → позиция закрыта ТЕРМИНАЛЬНО.
Через четыре минуты пришёл каталог, ключ появился — и не произошло НИЧЕГО:
запись терминальна, следующая отправка случится, когда изменится остаток, то
есть у зимней куртки через месяцы.

Опасность правки ровно одна и обратная: поднять то, что каталогом не лечится.
Про это здесь тестов больше, чем про сам подъём.
"""
from datetime import timedelta

from app.models import (Barcode, DispatchQueueItem, DispatchStatus, Platform,
                        PlatformAccount, PlatformCatalogItem, Product, SyncSetting)
from app.timeutils import now_utc
from app.workers.catalog_sync import revive_after_catalog
from tests.factories import make_account


class KitClient:
    """Kit адресует остаток variant_id — он же `external_id` каталога."""
    stock_key = "external_id"
    last_truncated = False

    def get_catalog_items(self):
        return []


class WbClient:
    """WB адресует баркодом: ключ есть всегда, если есть баркод."""
    stock_key = "barcode"
    last_truncated = False

    def get_catalog_items(self):
        return []


def _pair(db, *, platform=Platform.kit, uid="u1", barcode="BC-1",
          sent_sku=None, card_missing=True, status=DispatchStatus.error,
          with_catalog=True, external_id="v-1"):
    account = PlatformAccount(platform=platform, name="КИТ", warehouse_id="wh")
    db.add(account)
    db.commit()

    db.add(Product(uid_1c=uid, article="A-1", stock_on_hand=7,
                   broadcast_enabled=True, recalc_done_at=now_utc(),
                   recalc_account_ids=str(account.id)))
    db.add(Barcode(barcode=barcode, uid_1c=uid, source_platform="1c"))
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))
    if with_catalog:
        db.add(PlatformCatalogItem(account_id=account.id, barcode=barcode,
                                   external_id=external_id, article="A-1",
                                   name="Куртка", fetched_at=now_utc()))
    db.add(DispatchQueueItem(
        uid_1c=uid, account_id=account.id, quantity=7, reason="recalc_covered",
        status=status, card_missing=card_missing, sent_sku=sent_sku,
        last_error="нет карточки в каталоге кабинета — остаток отправить не по чему",
        created_at=now_utc() - timedelta(minutes=10)))
    db.commit()
    return account


def _pending(db, account):
    return db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account.id,
        DispatchQueueItem.status == DispatchStatus.pending).all()


# --------------------------------------------------------------------------
# Поднимаем то, что лечится каталогом
# --------------------------------------------------------------------------

def test_a_pair_that_waited_for_the_key_is_revived(db):
    """Ключ появился — пара обязана поехать.

    Иначе остаток не уедет никогда: запись терминальна, а следующего события по
    товару не будет, пока не изменится остаток.
    """
    account = _pair(db)

    assert revive_after_catalog(db, KitClient(), account) == 1

    rows = _pending(db, account)
    assert len(rows) == 1
    assert rows[0].reason == "catalog_loaded"


def test_the_revival_happens_inside_the_catalog_load(db):
    """Подъём — часть загрузки каталога, а не отдельная кнопка: человека,
    который вспомнит нажать её в нужный момент, не существует."""
    from app.workers.catalog_sync import load_platform_catalog

    account = _pair(db)

    stats = load_platform_catalog(db, KitClient(), account)

    assert stats["revived"] == 1


# --------------------------------------------------------------------------
# И НЕ поднимаем то, что каталогом не лечится
# --------------------------------------------------------------------------

def test_a_refusal_by_the_platform_is_not_revived(db):
    """`sent_sku` заполнен — значит позиция УЕХАЛА, а площадка ответила «такого
    sku на складе нет» (у WB это 409 NotFound).

    Каталог тут ни при чём: ключ был и есть, а карточки на складе площадки нет.
    Подними мы такую пару — сожгли бы запрос, закрыли её снова, и находка
    «Площадка не знает наш sku» замигала бы на ровном месте. На живом кабинете
    таких под сотню.
    """
    account = _pair(db, sent_sku="BC-1")

    assert revive_after_catalog(db, KitClient(), account) == 0
    assert _pending(db, account) == []


def test_a_pair_still_without_a_key_is_not_revived(db):
    """Карточки на площадке нет вовсе — ключ как не появился, так и нет.

    Иначе каждая суточная выгрузка гоняла бы по кругу сотню мёртвых пар.
    """
    account = _pair(db, with_catalog=False)

    assert revive_after_catalog(db, KitClient(), account) == 0
    assert _pending(db, account) == []


def test_an_empty_key_in_the_catalogue_is_not_a_key(db):
    """Строка каталога есть, а `variant_id` в ней пуст — отправлять по-прежнему
    нечем."""
    account = _pair(db, external_id="")

    assert revive_after_catalog(db, KitClient(), account) == 0


def test_an_ordinary_dispatch_failure_is_not_revived(db):
    """Обрыв связи каталогом не лечится: у него свои повторы."""
    account = _pair(db, card_missing=False)

    assert revive_after_catalog(db, KitClient(), account) == 0


def test_a_test_row_is_never_revived(db):
    """Граница `is_test`: симуляция не должна попасть в боевую отправку."""
    account = _pair(db)
    db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account.id).update({"is_test": True})
    db.commit()

    assert revive_after_catalog(db, KitClient(), account) == 0


# --------------------------------------------------------------------------
# Гейты те же
# --------------------------------------------------------------------------

def test_the_gates_still_hold(db):
    """Трансляция выключена — пара не едет, сколько бы ключей ни появилось.

    Обойти гейт здесь значило бы отправить ноль на живую карточку: ровно то, от
    чего он и стоит.
    """
    account = _pair(db)
    db.query(Product).filter(Product.uid_1c == "u1").update(
        {"broadcast_enabled": False})
    db.commit()

    assert revive_after_catalog(db, KitClient(), account) == 0
    assert _pending(db, account) == []


def test_a_cabinet_outside_the_recalc_is_not_revived(db):
    """Кабинет, которого расчёт не касался, не поедет: на него ушёл бы остаток,
    не сверенный с его продажами."""
    account = _pair(db)
    db.query(Product).filter(Product.uid_1c == "u1").update(
        {"recalc_account_ids": ""})
    db.commit()

    assert revive_after_catalog(db, KitClient(), account) == 0


# --------------------------------------------------------------------------
# Правило выбора ключа — одно на рассылку и на каталог
# --------------------------------------------------------------------------

def test_wb_uses_the_barcode_as_its_key(db):
    """У WB ключ — баркод, и он есть даже без строки каталога.

    Проверка не про подъём, а про то, что правило выбора ключа у каталога то
    же, что у рассылки: разойдись они, каталог начал бы поднимать пары, по
    которым отправлять по-прежнему нечем.
    """
    from app.workers.dispatch import push_identifier

    account = _pair(db, platform=Platform.wb, with_catalog=False)

    assert push_identifier(db, "u1", account.id, "barcode") == "BC-1"
    assert push_identifier(db, "u1", account.id, "external_id") == ""
    # А раз ключ у WB есть всегда — такую пару каталог и поднимет.
    assert revive_after_catalog(db, WbClient(), account) == 1

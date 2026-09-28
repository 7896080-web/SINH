"""Находки аудита 22.09, блок «данные и схема».

Четыре разных дефекта, но у трёх общая природа: система забывает то, что
помнить обязана, и вспоминает не то.
"""
import io
from datetime import timedelta

from openpyxl import Workbook

from app.models import (Barcode, DispatchQueueItem, DispatchStatus, MappingConflict,
                        Platform, PlatformAccount, PlatformCatalogItem, Product,
                        SyncSetting)
from app.report import collect_findings
from app.timeutils import now_utc
from tests.factories import make_account


def _keys(findings):
    return {f.key for f in findings}


# ---------------------------------------------------------------------------
# 1. Мёртвый отказ воскресает, когда чистка удалит перекрывшую его отправку
# ---------------------------------------------------------------------------

def _pair_with_error(db, *, sent_row: bool, last_nonzero_ago=None):
    """Пара, по которой был отказ, а потом остаток доехал."""
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5,
                   broadcast_enabled=True))
    setting = SyncSetting(uid_1c="u1", account_id=account.id, enabled=True)
    if last_nonzero_ago is not None:
        setting.last_nonzero_sent_at = now_utc() - last_nonzero_ago
    db.add(setting)
    db.add(DispatchQueueItem(
        uid_1c="u1", account_id=account.id, quantity=5, reason="order",
        status=DispatchStatus.error, last_error="не отправлено за 5 попыток: 400",
        created_at=now_utc() - timedelta(days=40)))
    if sent_row:
        db.add(DispatchQueueItem(
            uid_1c="u1", account_id=account.id, quantity=5, sent_quantity=5,
            sent_sku="111", reason="order", status=DispatchStatus.sent,
            created_at=now_utc() - timedelta(days=39),
            sent_at=now_utc() - timedelta(days=39)))
    db.commit()
    return account


def test_a_dead_error_stays_dead_after_the_cleanup_removed_its_cover(db):
    """Чистка удаляет успешные отправки через тридцать суток, а отказы не
    удаляет никогда — намеренно, `error` описывает незаконченное дело. Но отсев
    мёртвых отказов построен на «последней записи пары», и как только
    перекрывшая отправка исчезает, последней СНОВА становится старый отказ.

    Он воскресает КРИТИЧНОЙ находкой «площадка продаёт то, чего нет» — про
    число, доехавшее месяц назад. Разобрать её нельзя ничем: статуса запись не
    сменит, удалена не будет, отчёт останется красным навсегда. На бою заготовлены
    сотни таких записей от дефекта Kit 14–20.09.

    Спасает `last_nonzero_sent_at`: она живёт на ПАРЕ и чистку переживает.
    """
    _pair_with_error(db, sent_row=False, last_nonzero_ago=timedelta(days=39))

    assert "dispatch_errors" not in _keys(collect_findings(db))


def test_an_error_newer_than_the_last_send_is_still_reported(db):
    """Обратная сторона: отправка, бывшая РАНЬШЕ отказа, про него ничего не
    говорит. Заглушить его ею значило бы потерять настоящее расхождение —
    остаток списан, число не уехало, площадка продаёт то, чего нет."""
    _pair_with_error(db, sent_row=False, last_nonzero_ago=timedelta(days=41))

    assert "dispatch_errors" in _keys(collect_findings(db))


def test_a_pair_that_never_got_a_number_is_still_reported(db):
    """И третий случай: на пару вообще ничего не отправляли. Тогда отказ —
    единственное, что о ней известно, и молчать о нём нельзя."""
    _pair_with_error(db, sent_row=False, last_nonzero_ago=None)

    assert "dispatch_errors" in _keys(collect_findings(db))


# ---------------------------------------------------------------------------
# 2. Автопривязка по пулу открывала оверселл
# ---------------------------------------------------------------------------

class _Catalog:
    last_truncated = False

    def __init__(self, items):
        self._items = items

    def get_catalog_items(self):
        return self._items


def test_a_pool_guess_drops_the_recalc_mark(db):
    """Догадка привязывает новый баркод к товару — значит набор баркодов
    изменился, и прошлый расчёт к нему не относится: продажи по этому баркоду он
    заведомо не видел.

    Без снятия отметки это тихий оверселл. Товар числится актуализированным,
    догнать продажи нечем (`catch_up_product` по нему не зовут, живой опрос
    старый заказ уже не принесёт), ворота открыты, ступень 2 молчит — и наружу
    уходит остаток, завышенный ровно на эти продажи. Заодно этой же загрузкой
    удаляется единственный след, конфликт сопоставления.
    """
    from app.workers.catalog_sync import load_platform_catalog
    from app.workers.platform_clients.base import CatalogItem

    account = make_account(db, Platform.wb)
    product = Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5,
                      recalc_done_at=now_utc(), recalc_account_ids=str(account.id))
    db.add(product)
    db.add(Barcode(barcode="111", uid_1c="u1"))          # сосед по карточке
    db.add(PlatformCatalogItem(account_id=account.id, external_id="100:1",
                               barcode="111", article="A", name="К"))
    db.commit()

    stats = load_platform_catalog(db, _Catalog([
        CatalogItem(barcode="111", external_id="100:1", article="A", name="К"),
        CatalogItem(barcode="222", external_id="100:1", article="A", name="К"),
    ]), account)

    db.refresh(product)
    assert stats["pool_matched"] == 1
    assert stats["recalc_dropped"] == 1
    assert product.recalc_done_at is None
    assert product.recalc_account_ids == "", "NULL включил бы ступень 2 не там"


def test_a_product_without_a_recalc_is_left_alone(db):
    """У товара, где расчёта не было НИКОГДА, не трогаем ничего. Выставить ему
    пустую строку значит включить ступень 2 лестницы, которая для таких товаров
    обязана молчать, — на отмеченный кабинет уехал бы ноль."""
    from app.workers.catalog_sync import load_platform_catalog
    from app.workers.platform_clients.base import CatalogItem

    account = make_account(db, Platform.wb)
    product = Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5)
    db.add(product)
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(PlatformCatalogItem(account_id=account.id, external_id="100:1",
                               barcode="111", article="A", name="К"))
    db.commit()

    load_platform_catalog(db, _Catalog([
        CatalogItem(barcode="111", external_id="100:1", article="A", name="К"),
        CatalogItem(barcode="222", external_id="100:1", article="A", name="К"),
    ]), account)

    db.refresh(product)
    assert product.recalc_account_ids is None


def test_a_new_barcode_from_the_mapping_import_drops_the_mark_too(logged_in_client, web_db):
    """Тот же пробел был в ручном импорте «Мэппинга»: переподвязка отметку
    снимала, а заведение НОВОГО баркода — нет, хотя для остатка разница та же."""
    account = PlatformAccount(platform=Platform.wb, name="WB", warehouse_id="wh")
    web_db.add(account)
    product = Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5,
                      recalc_done_at=now_utc(), recalc_account_ids="1")
    web_db.add(product)
    web_db.commit()

    wb = Workbook()
    ws = wb.active
    ws.append(["Баркод", "ID_1С"])
    ws.append(["999", "u1"])
    buf = io.BytesIO()
    wb.save(buf)
    logged_in_client.post(
        "/mapping/import",
        files={"file": ("f.xlsx", buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=True)

    web_db.expire_all()
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first().recalc_done_at is None


# ---------------------------------------------------------------------------
# 3. Повтор ID_1С в файле «Товары и остатки»
# ---------------------------------------------------------------------------

def test_a_repeated_id_does_not_kill_the_whole_import(logged_in_client, web_db):
    """Сессия живёт с `autoflush=False`: второй строке файла с тем же ID_1С
    запрос не покажет первый `db.add`, и на ту же пару добавлялся второй объект
    под `uq_product_account`. Коммит на весь импорт один, обработчика исключений
    нет — оператор получал 500 и откат ВСЕГО файла: ни даты, ни факта, ни брони,
    ни отметок кабинетов, по всем строкам.

    А повтор ID_1С — обычное дело: склеили две выгрузки, скопировали строку,
    чтобы поправить опечатку.
    """
    account = PlatformAccount(platform=Platform.wb, name="WB", warehouse_id="wh")
    web_db.add(account)
    web_db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5,
                       reserve=0))
    web_db.commit()
    label = f"{account.name} ({account.platform.value.upper()})"

    wb = Workbook()
    ws = wb.active
    ws.append(["ID_1С", "Резерв", f"{label} — Синхронизировать"])
    ws.append(["u1", 1, "Да"])
    ws.append(["u1", 2, "Да"])           # тот же товар второй раз
    buf = io.BytesIO()
    wb.save(buf)

    r = logged_in_client.post(
        "/products/import",
        files={"file": ("f.xlsx", buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=True)

    assert r.status_code == 200
    web_db.expire_all()
    rows = web_db.query(SyncSetting).filter(SyncSetting.uid_1c == "u1").all()
    assert len(rows) == 1, "на пару товар+кабинет завелось больше одной настройки"
    assert web_db.query(Product).filter(Product.uid_1c == "u1").first().reserve == 2

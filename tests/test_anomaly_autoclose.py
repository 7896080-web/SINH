"""Аномалия, причина которой исчезла, уходит из списка сама.

Аномалия — это заказ на товар, с которым мы работать не могли: трансляция на
кабинет не включена, или у товара нет баркода. Причину устраняют не только
кнопкой на этой странице: товар включают на «Товарах», массовой правкой,
импортом из Excel, баркод приезжает справочником из 1С. Во всех этих случаях
строка оставалась в списке навсегда.

20.09 на бою их накопилось 93, почти все — по каталогу, который ещё ведёт
вторая система. Список, не пустеющий от работы, перестают открывать: по нему
не видно, сделано что-то или нет.
"""

from app.anomalies import close_fixed_anomalies
from app.models import (AnomalyReason, AnomalyStatus, Barcode, Platform,
                        Product, SyncAnomaly, SyncSetting)
from tests.factories import make_account


def _anomaly(db, account, uid="u1", reason=AnomalyReason.order_on_disabled,
             is_test=False):
    db.add(SyncAnomaly(uid_1c=uid, account_id=account.id, reason=reason,
                       order_id="o-1", status=AnomalyStatus.new, is_test=is_test))


def _product(db, uid="u1"):
    db.add(Product(uid_1c=uid, article="A1", name="Товар", stock_on_hand=5))


# --------------------------------------------------------- гасим что лечится

def test_an_anomaly_closes_when_the_pair_is_enabled(db):
    account = make_account(db)
    _product(db)
    _anomaly(db, account)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    assert close_fixed_anomalies(db) == 1
    assert db.query(SyncAnomaly).one().status == AnomalyStatus.resolved


def test_a_missing_barcode_anomaly_closes_when_the_barcode_appears(db):
    """Баркод приезжает справочником из 1С, а не через эту страницу — и до
    правки строка оставалась висеть, хотя разносить заказ уже есть на что."""
    account = make_account(db)
    _product(db)
    _anomaly(db, account, reason=AnomalyReason.missing_barcode)
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()

    assert close_fixed_anomalies(db) == 1


# ------------------------------------------- и не гасим то, что не вылечено

def test_an_anomaly_stays_while_the_pair_is_off(db):
    """Закрытая аномалия исчезает из работы. Закрыть её раньше времени — значит
    потерять заказ, который никто не разнёс."""
    account = make_account(db)
    _product(db)
    _anomaly(db, account)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=False))
    db.commit()

    assert close_fixed_anomalies(db) == 0
    assert db.query(SyncAnomaly).one().status == AnomalyStatus.new


def test_enabling_another_cabinet_does_not_close_the_anomaly(db):
    """Аномалия — про пару товар+кабинет. Включение соседнего кабинета про этот
    не говорит ничего."""
    wb = make_account(db, Platform.wb, name="ИП ЯВОРСКАЯ")
    kit = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")
    _product(db)
    _anomaly(db, wb)
    db.add(SyncSetting(uid_1c="u1", account_id=kit.id, enabled=True))
    db.commit()

    assert close_fixed_anomalies(db) == 0


def test_a_missing_barcode_anomaly_stays_without_a_barcode(db):
    account = make_account(db)
    _product(db)
    _anomaly(db, account, reason=AnomalyReason.missing_barcode)
    db.commit()

    assert close_fixed_anomalies(db) == 0


def test_a_barcode_of_another_product_changes_nothing(db):
    account = make_account(db)
    _product(db)
    _product(db, uid="u2")
    _anomaly(db, account, reason=AnomalyReason.missing_barcode)
    db.add(Barcode(barcode="111", uid_1c="u2"))
    db.commit()

    assert close_fixed_anomalies(db) == 0


def test_already_resolved_rows_are_left_alone(db):
    account = make_account(db)
    _product(db)
    db.add(SyncAnomaly(uid_1c="u1", account_id=account.id,
                       reason=AnomalyReason.order_on_disabled,
                       status=AnomalyStatus.resolved))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    assert close_fixed_anomalies(db) == 0


def test_test_anomalies_are_never_touched(db):
    """Симуляция со страницы «Тестирование» живёт отдельно — та же граница
    `is_test`, что и у остальных побочных записей."""
    account = make_account(db)
    _product(db)
    _anomaly(db, account, is_test=True)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    assert close_fixed_anomalies(db) == 0
    assert db.query(SyncAnomaly).one().status == AnomalyStatus.new


def test_an_empty_list_is_not_an_error(db):
    assert close_fixed_anomalies(db) == 0


# ----------------------------------------------------------------- страница

def test_the_page_drops_a_fixed_anomaly(logged_in_client, web_db):
    from app.models import PlatformAccount

    account = PlatformAccount(platform=Platform.wb, name="ИП ЯВОРСКАЯ", warehouse_id="wh-1")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    web_db.add(Product(uid_1c="u1", article="УЖЕВКЛЮЧЁН", name="Товар", stock_on_hand=5))
    web_db.add(SyncAnomaly(uid_1c="u1", account_id=account.id,
                           reason=AnomalyReason.order_on_disabled,
                           status=AnomalyStatus.new))
    web_db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    web_db.commit()

    page = logged_in_client.get("/anomalies")

    assert "УЖЕВКЛЮЧЁН" not in page.text
    assert web_db.query(SyncAnomaly).one().status == AnomalyStatus.resolved


def test_the_page_refreshes_itself(logged_in_client):
    """Аномалии появляются при опросе заказов и исчезают по мере включения
    товаров. Без опроса оператор разбирал бы список на момент открытия."""
    page = logged_in_client.get("/anomalies")

    assert 'hx-trigger="every' in page.text
    assert "/anomalies/rows" in page.text


def test_the_refresh_keeps_the_filters(logged_in_client):
    """Обновление, сбрасывающее отбор, хуже отсутствия обновления: оператор
    сузил список кабинетом, а через две минуты видит весь."""
    page = logged_in_client.get("/anomalies")

    block = page.text.split('id="anomalies-table"', 1)[1][:300]
    assert "hx-include" in block

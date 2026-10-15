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


# ------------------------------------------- и не упираемся в предел параметров

def test_the_queries_do_not_carry_a_parameter_per_anomaly(db):
    """Число параметров в запросе не зависит от числа аномалий — и это главное.

    Прежний вид грузил ВСЕ открытые аномалии целыми объектами и строил `IN` по
    всем их товарам. Строки в статусе `new` не чистит никто намеренно
    (незаконченное дело), а «заказ на невключённый товар» пока идёт переход
    приходит десятками в сутки и сам не закрывается — то есть на 32 766 разных
    товарах SQLite ответил бы «too many SQL variables», и страница аномалий
    перестала бы открываться ВОВСЕ, а она единственное место, где их разбирают.

    Проверяем не скорость и не память, а именно это: столько же параметров на
    двадцати аномалиях, сколько на шестидесяти. Магического числа тут нет
    намеренно — с ним тест пришлось бы править от каждой правки условия, а
    зависимость от объёма он бы не поймал.
    """
    from sqlalchemy import event

    account = make_account(db)
    widest: list[int] = []

    @event.listens_for(db.get_bind(), "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        widest.append(len(parameters or ()))

    def measure(anomalies: int) -> int:
        """Сколько параметров в самом широком запросе автозакрытия."""
        widest.clear()
        close_fixed_anomalies(db)
        return max(widest)

    # Аномалии НЕвылеченные: ни одна не закроется, значит оба замера про одно и
    # то же состояние, и разница может прийти только от объёма.
    for i in range(20):
        _product(db, uid=f"u{i}")
        _anomaly(db, account, uid=f"u{i}", reason=AnomalyReason.missing_barcode)
    db.commit()
    on_twenty = measure(20)

    for i in range(20, 60):
        _product(db, uid=f"u{i}")
        _anomaly(db, account, uid=f"u{i}", reason=AnomalyReason.missing_barcode)
    db.commit()
    on_sixty = measure(60)

    assert db.query(SyncAnomaly).filter(
        SyncAnomaly.status == AnomalyStatus.new).count() == 60, (
        "замеры шли по разным состояниям — тест проверяет не то, что думает")
    assert on_sixty == on_twenty, (
        f"параметров стало больше от числа аномалий ({on_twenty} -> {on_sixty}): "
        "запрос растёт вместе с таблицей и однажды упрётся в предел SQLite")


def test_nothing_to_close_means_no_writing_at_all(db):
    """Закрывать нечего — значит и писать нечего. Проверено на самом SQLite.

    Пустой `UPDATE` — это всё равно письмо: он открывает транзакцию записи и
    берёт блокировку ещё до того, как выяснится, что подходящих строк нет
    (второй писатель в этот момент получает `database is locked`). А страница
    коммитит только когда что-то закрылось, то есть блокировка жила бы до конца
    запроса — на каждой загрузке и каждом автообновлении раз в две минуты, пока
    в ту же базу пишут рассылка и приём заказов.
    """
    from sqlalchemy import event

    account = make_account(db)
    _product(db)
    _anomaly(db, account)                      # есть, но НЕ вылечена
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=False))
    db.commit()

    written = []

    @event.listens_for(db.get_bind(), "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            written.append(statement.split()[0].upper())

    assert close_fixed_anomalies(db) == 0
    assert written == [], (
        f"на пустой работе ушло письмо в базу ({written}) — блокировка записи "
        "будет висеть до конца запроса страницы")


def test_something_to_close_still_gets_written(db):
    """Обратная сторона: проба не должна глушить саму работу.

    Без этой проверки правка выше однажды превратилась бы в «ничего не
    закрываем», и список перестал бы пустеть от работы — молча."""
    from sqlalchemy import event

    account = make_account(db)
    _product(db)
    _anomaly(db, account)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    written = []

    @event.listens_for(db.get_bind(), "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE"):
            written.append(statement)

    assert close_fixed_anomalies(db) == 1
    assert len(written) == 1, "закрывать было что, а записи не произошло"


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

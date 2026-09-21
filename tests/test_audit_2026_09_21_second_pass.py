"""Находки второго аудита 21.09 — каждая закрыта тестом.

Общее у них одно: все эти дефекты ничем не проявлялись в день, когда случались.
Массовая кнопка не отзывала остаток — площадка молча продолжала продавать. Запись
очереди, пережившая снятие галочки, обнуляла чужую карточку — и это выглядело как
успешная отправка. Суточная чистка стирала память об отправке — и отзыв переставал
работать через месяц после последней продажи.
"""
import pytest

from app.models import (Barcode, DispatchQueueItem, DispatchStatus, PlatformAccount,
                        Product, ReconciliationLog, SyncSetting)
from app.timeutils import now_utc


# --------------------------------------------------------------------------
# Ступень 2 лестницы: снятие отметки «актуализирован» обязано ЗАКРЫВАТЬ ворота
# --------------------------------------------------------------------------

def _product_with_two_cabinets(db):
    a = PlatformAccount(platform="wb", name="WB", warehouse_id="w1", is_active=True)
    b = PlatformAccount(platform="ozon", name="OZ", warehouse_id="w2", is_active=True)
    db.add_all([a, b]); db.commit(); db.refresh(a); db.refresh(b)
    p = Product(uid_1c="u1", article="A", name="Т", stock_on_hand=50,
                broadcast_enabled=True, recalc_done_at=now_utc(),
                recalc_account_ids=str(a.id))
    db.add(p)
    db.add(Barcode(barcode="bc1", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=a.id, enabled=True))
    db.add(SyncSetting(uid_1c="u1", account_id=b.id, enabled=True))
    db.commit()
    return p, a, b


def test_clearing_the_recalc_mark_does_not_open_the_gate(db):
    """Переподвязка баркода снимает отметку — и это должно ЗАКРЫТЬ трансляцию.

    Условие было написано как «расчёт БЫЛ и кабинет не покрыт», поэтому снятие
    отметки его отключало: кабинет, минуту назад закрытый как непокрытый, начинал
    получать полный остаток — причём посчитанный по СТАРОМУ набору баркодов, то
    есть завышенный. Комментарий в `mapping.py` обещал ровно обратное.
    """
    from app.transmit import quantity_for_account, enqueue_full_resend

    p, a, b = _product_with_two_cabinets(db)
    assert quantity_for_account(db, "u1", a.id, 50) == 50      # покрыт расчётом
    assert quantity_for_account(db, "u1", b.id, 50) == 0       # не покрыт

    # Ровно то, что делает переподвязка баркода.
    p.recalc_done_at = None
    p.recalc_account_ids = ""
    db.commit()

    assert quantity_for_account(db, "u1", a.id, 50) == 0, "остаток поехал без пересчёта"
    assert quantity_for_account(db, "u1", b.id, 50) == 0
    assert enqueue_full_resend(db, "u1", a.id) is False
    assert enqueue_full_resend(db, "u1", b.id) is False


def test_a_product_that_never_had_a_recalc_is_not_gated_by_step_two(db):
    """А товар, которого расчёт НИКОГДА не касался, ступень 2 не трогает.

    Различие принципиальное: закрой мы и такие товары, у всех, кому трансляцию
    включили до появления расчёта, на площадки уехал бы ноль.
    """
    from app.transmit import quantity_for_account

    a = PlatformAccount(platform="wb", name="WB", warehouse_id="w1", is_active=True)
    db.add(a); db.commit(); db.refresh(a)
    db.add(Product(uid_1c="old", article="A", name="Т", stock_on_hand=7,
                   broadcast_enabled=True))          # recalc_* оба NULL
    db.add(SyncSetting(uid_1c="old", account_id=a.id, enabled=True))
    db.commit()

    assert quantity_for_account(db, "old", a.id, 7) == 7


# --------------------------------------------------------------------------
# Память об отправке переживает чистку очереди
# --------------------------------------------------------------------------

def test_the_memory_of_a_transmission_survives_retention(db):
    """Чистка очереди не должна стирать право на отзыв остатка.

    `ever_transmitted` искала доказательство в `dispatch_queue`, а суточная чистка
    удаляет терминальные записи старше тридцати суток. У медленного размера
    остаток не меняется месяцами: строка исчезала, и снятие галочки переставало
    отзывать остаток — площадка продолжала продавать по нашему числу, а заказы по
    снятой паре живой опрос уже пропускает. Оверселл.
    """
    from app.transmit import ever_transmitted
    from app.retention import apply_retention
    from datetime import timedelta

    a = PlatformAccount(platform="wb", name="WB", warehouse_id="w1", is_active=True)
    db.add(a); db.commit(); db.refresh(a)
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=5))
    old = now_utc() - timedelta(days=40)
    db.add(SyncSetting(uid_1c="u1", account_id=a.id, enabled=True,
                       last_nonzero_sent_at=old))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=a.id, quantity=3, sent_quantity=3,
                             reason="manual_enable", status=DispatchStatus.sent,
                             sent_at=old, created_at=old))
    db.commit()

    assert ever_transmitted(db, "u1", a.id) is True
    apply_retention(db)
    assert db.query(DispatchQueueItem).count() == 0, "чистка не отработала — тест ни о чём"
    assert ever_transmitted(db, "u1", a.id) is True, "система забыла, что писала на карточку"


# --------------------------------------------------------------------------
# Рассылка не отправляет холостой ноль
# --------------------------------------------------------------------------

class _FakeClient:
    stock_key = "barcode"

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def push_stock(self, warehouse_id, items):
        if self.fail:
            raise RuntimeError("площадка недоступна")
        self.sent += [(i.barcode, i.quantity) for i in items]
        return {"ok": [i.barcode for i in items], "errors": []}

    def get_stocks(self, warehouse_id, keys):
        return None


def test_dispatch_never_zeroes_a_card_it_never_wrote_to(db):
    """Запись, пережившая снятие галочки, обнуляла живую чужую карточку.

    Инцидент 18.09 закрыли на ВХОДЕ в очередь, а запись, попавшая туда до того как
    оператор передумал, доживала до отправки — и лестница отдавала по ней ноль.
    """
    from app.workers.dispatch import run_dispatch_cycle

    a = PlatformAccount(platform="wb", name="WB", warehouse_id="w1", is_active=True,
                        dispatch_enabled=True)
    db.add(a); db.commit(); db.refresh(a)
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=50,
                   broadcast_enabled=False))          # трансляция выключена
    db.add(Barcode(barcode="bc1", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=a.id, enabled=True))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=a.id, quantity=50,
                             reason="manual_enable", status=DispatchStatus.pending))
    db.commit()

    client = _FakeClient()
    run_dispatch_cycle(db, {a.id: client}, [a])
    assert client.sent == [], f"на площадку ушло {client.sent}"

    item = db.query(DispatchQueueItem).first()
    assert item.status == DispatchStatus.sent      # запись закрыта, а не висит
    assert item.sent_at is None                    # но отправкой не считается
    assert "отзывать нечего" in (item.last_error or "")


def test_dispatch_does_send_zero_when_there_is_something_to_withdraw(db):
    """А осознанный отзыв уходить обязан: иначе площадка продаёт то, чего нет."""
    from app.workers.dispatch import run_dispatch_cycle

    a = PlatformAccount(platform="wb", name="WB", warehouse_id="w1", is_active=True,
                        dispatch_enabled=True)
    db.add(a); db.commit(); db.refresh(a)
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=50,
                   broadcast_enabled=False))
    db.add(Barcode(barcode="bc1", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=a.id, enabled=True,
                       last_nonzero_sent_at=now_utc()))     # туда уже писали
    db.add(DispatchQueueItem(uid_1c="u1", account_id=a.id, quantity=0,
                             reason="broadcast_off", status=DispatchStatus.pending))
    db.commit()

    client = _FakeClient()
    run_dispatch_cycle(db, {a.id: client}, [a])
    assert client.sent == [("bc1", 0)]


def test_a_successful_send_is_remembered_on_the_pair(db):
    """Успешная непустая отправка ставит отметку на паре, а не только в очередь."""
    from app.workers.dispatch import run_dispatch_cycle

    a = PlatformAccount(platform="wb", name="WB", warehouse_id="w1", is_active=True,
                        dispatch_enabled=True)
    db.add(a); db.commit(); db.refresh(a)
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=9,
                   broadcast_enabled=True))
    db.add(Barcode(barcode="bc1", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=a.id, enabled=True))
    db.add(DispatchQueueItem(uid_1c="u1", account_id=a.id, quantity=9,
                             reason="manual_enable", status=DispatchStatus.pending))
    db.commit()

    run_dispatch_cycle(db, {a.id: _FakeClient()}, [a])
    setting = db.query(SyncSetting).first()
    assert setting.last_nonzero_sent_at is not None


def test_one_broken_cabinet_does_not_starve_the_others(db):
    """Сетевой сбой одного кабинета останавливал рассылку на все следующие.

    `with_retry` перевыбрасывает `RequestException`, а клиенты ловят только
    `HTTPError`: исключение вылетало из всего цикла, и кабинеты, стоящие в списке
    после сбойного, в этом проходе не обрабатывались вовсе. Список идёт по id —
    страдали всегда одни и те же.
    """
    from app.workers.dispatch import run_dispatch_cycle

    bad = PlatformAccount(platform="wb", name="Сломанный", warehouse_id="w1",
                          is_active=True, dispatch_enabled=True)
    good = PlatformAccount(platform="ozon", name="Исправный", warehouse_id="w2",
                           is_active=True, dispatch_enabled=True)
    db.add_all([bad, good]); db.commit(); db.refresh(bad); db.refresh(good)
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=5,
                   broadcast_enabled=True))
    db.add(Barcode(barcode="bc1", uid_1c="u1"))
    for acc in (bad, good):
        db.add(SyncSetting(uid_1c="u1", account_id=acc.id, enabled=True))
        db.add(DispatchQueueItem(uid_1c="u1", account_id=acc.id, quantity=5,
                                 reason="manual_enable", status=DispatchStatus.pending))
    db.commit()

    ok_client = _FakeClient()
    stats = run_dispatch_cycle(db, {bad.id: _FakeClient(fail=True), good.id: ok_client},
                               [bad, good])
    assert ok_client.sent == [("bc1", 5)], "исправный кабинет остался без остатка"
    assert "failed" in stats["Сломанный"]


# --------------------------------------------------------------------------
# Ворота трансляции и отключённый кабинет
# --------------------------------------------------------------------------

def test_a_disabled_cabinet_does_not_lock_the_broadcast_gate_forever(db):
    """Кабинет, погашенный предохранителем, запирал строку навсегда.

    Расчёт опрашивает только активные кабинеты и в покрытые такой не кладёт, а
    ворота сравнивали покрытие с ПОЛНЫМ набором отмеченных — `issubset` ложно
    всегда. Повторный расчёт ничего не менял, а включить трансляцию было нельзя
    ни по этому товару, ни по остальным, отмеченным для того же кабинета.
    """
    from app.broadcast_gate import blocks_broadcast_on

    live = PlatformAccount(platform="wb", name="Живой", warehouse_id="w1", is_active=True)
    dead = PlatformAccount(platform="kit", name="Погашенный", warehouse_id="w2",
                           is_active=False)
    db.add_all([live, dead]); db.commit(); db.refresh(live); db.refresh(dead)
    from datetime import date
    p = Product(uid_1c="u1", article="A", name="Т", stock_on_hand=5,
                offset_base_date=date(2026, 9, 1), offset_base_stock=5, fact_at_date=5,
                recalc_done_at=now_utc(), recalc_account_ids=str(live.id))
    db.add(p)
    db.add(SyncSetting(uid_1c="u1", account_id=live.id, enabled=True))
    db.add(SyncSetting(uid_1c="u1", account_id=dead.id, enabled=True))
    db.commit()
    db.refresh(p)

    assert blocks_broadcast_on(p) is None, (
        "погашенный кабинет запер ворота: " + str(blocks_broadcast_on(p)))


# --------------------------------------------------------------------------
# Сверка: совпадение — не расхождение
# --------------------------------------------------------------------------

def test_a_matching_row_is_not_left_unresolved(db):
    """Строка сверки с нулевой дельтой не должна копиться как «неразрешённая».

    Их около сорока тысяч в сутки, и кнопка «Закрыть старые расхождения» поднимала
    их все разом одной транзакцией: на 250 тысячах — 10,9 с эксклюзивной
    блокировки записи и 857 МБ памяти.
    """
    from app.workers.reconciliation import run_reconciliation

    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=5))
    db.add(Barcode(barcode="bc1", uid_1c="u1"))
    db.commit()

    run_reconciliation(db, {"bc1": 5}, missing_means_zero=False, snapshot_at=now_utc())
    log = db.query(ReconciliationLog).filter(ReconciliationLog.uid_1c == "u1").first()
    assert log is not None and log.delta == 0
    assert log.resolved is True


def test_closing_old_rows_commits_in_chunks(logged_in_client, web_db):
    """Кнопка закрывает порциями, а не одной транзакцией на всю выборку."""
    from datetime import timedelta
    from app.routers.diagnostics import CLOSE_RECONCILIATION_CHUNK

    old = now_utc() - timedelta(days=3)
    n = CLOSE_RECONCILIATION_CHUNK + 7       # заведомо больше одной порции
    web_db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=1))
    for _ in range(n):
        web_db.add(ReconciliationLog(uid_1c="u1", python_stock=1, in_flight=0,
                                     expected_1c=1, actual_1c=2, delta=1,
                                     classification="auto_plus", checked_at=old,
                                     resolved=False))
    web_db.commit()

    r = logged_in_client.post("/diagnostics/close-old-reconciliation", follow_redirects=False)
    assert r.status_code == 303
    web_db.expire_all()
    left = web_db.query(ReconciliationLog).filter(
        ReconciliationLog.resolved.is_(False)).count()
    assert left == 0, f"осталось незакрытых: {left}"


# --------------------------------------------------------------------------
# Массовые пути каталога
# --------------------------------------------------------------------------

def _seed_web(web_db, *, transmitted=True):
    a = PlatformAccount(platform="wb", name="WB", warehouse_id="w1", is_active=True)
    web_db.add(a); web_db.commit(); web_db.refresh(a)
    from datetime import date
    web_db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=50,
                       broadcast_enabled=True, reserve=0,
                       offset_base_date=date(2026, 9, 1), offset_base_stock=50,
                       fact_at_date=50, recalc_done_at=now_utc(),
                       recalc_account_ids=str(a.id)))
    web_db.add(Barcode(barcode="bc1", uid_1c="u1"))
    web_db.add(SyncSetting(uid_1c="u1", account_id=a.id, enabled=True,
                           last_nonzero_sent_at=now_utc() if transmitted else None))
    web_db.commit()
    return a


def test_bulk_broadcast_off_withdraws_the_stock(logged_in_client, web_db):
    """Массовое выключение трансляции обязано отзывать остаток.

    Галочка в строке отзыв делала, массовая кнопка — нет, и на площадке
    оставалось последнее отправленное число. Заказы по этой паре живой опрос
    продолжает принимать (он смотрит на `SyncSetting.enabled`, которого выключение
    трансляции не трогает): остаток падает у нас, у площадки — нет. Оверселл, и
    сразу по всему отбору.
    """
    a = _seed_web(web_db)
    logged_in_client.post("/products/bulk", data={"action": "broadcast_off", "uids": "u1"})

    web_db.expire_all()
    withdrawals = web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.reason == "broadcast_off").all()
    assert len(withdrawals) == 1 and withdrawals[0].quantity == 0
    assert web_db.query(Product).first().broadcast_enabled is False


def test_bulk_reserve_recomputes_the_offset(logged_in_client, web_db):
    """Массовая правка брони обязана пересчитать порог.

    Одиночная правка его пересчитывает, `set_fact` тоже, а `set_reserve` в
    массовой ветке — нет. Новая бронь оставалась словами: в пороге сидела старая,
    и наружу уходило больше, чем оператор оставил в продаже.
    """
    _seed_web(web_db)
    before = web_db.query(Product).first().broadcast_offset

    logged_in_client.post("/products/bulk",
                          data={"action": "set_reserve", "int_value": "10", "uids": "u1"})
    web_db.expire_all()
    p = web_db.query(Product).first()
    assert p.reserve == 10
    assert p.broadcast_offset != before, "порог не пересчитан — бронь не подействовала"


def test_test_page_cleanup_respects_the_broadcast_gates(logged_in_client, web_db):
    """«Очистить» на «Тестировании» ставила БОЕВЫЕ записи мимо обоих гейтов.

    Страница нужна, чтобы прогнать товар ДО включения трансляции — то есть кнопку
    нажимают ровно в том состоянии, в котором лестница даёт ноль. Этот ноль уезжал
    на живую карточку, по которой идут чужие продажи.
    """
    a = _seed_web(web_db, transmitted=False)
    p = web_db.query(Product).first()
    p.broadcast_enabled = False
    web_db.commit()

    logged_in_client.post("/testing/simulate-order",
                          data={"uid_1c": "u1", "account_id": a.id, "quantity": 1})
    web_db.query(DispatchQueueItem).delete()
    web_db.commit()

    logged_in_client.post("/testing/cleanup", data={"uid_1c": "u1", "account_id": a.id})
    web_db.expire_all()
    real = web_db.query(DispatchQueueItem).filter(
        DispatchQueueItem.is_test.is_(False)).all()
    assert real == [], f"мимо гейтов поставлено {len(real)} боевых записей"

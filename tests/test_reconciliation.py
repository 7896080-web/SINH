import pytest

from app.models import ReconciliationClassification as C
from app.workers.reconciliation import classify_delta, run_reconciliation
from app.models import Product, Barcode, SyncSetting, FtpTask, FtpTaskStatus
from tests.factories import make_account


@pytest.mark.parametrize("delta,python_stock,expected", [
    (0, 10, C.normal),
    (2, 10, C.auto_plus),       # +2 из 10 = 20% — в пределах порога
    (-2, 10, C.auto_minus),
    (6, 10, C.needs_review),     # абсолютное значение > 5
    (-6, 10, C.needs_review),
    (2, 3, C.needs_review),      # 2 из 3 = 67% — превышает порог по доле
    (1, 3, C.needs_review),      # 1 из 3 = 33% > 30% — тоже превышает порог по доле
])
def test_classify_delta_table(delta, python_stock, expected):
    assert classify_delta(delta, python_stock) == expected


def test_classify_delta_zero_stock_treated_as_needs_review_unless_zero_delta():
    # при python_stock == 0 любое ненулевое отклонение — 100% превышение доли
    assert classify_delta(0, 0) == C.normal
    assert classify_delta(1, 0) == C.needs_review


def test_run_reconciliation_auto_plus_updates_stock_and_enqueues_dispatch(db):
    # Трансляция ВКЛЮЧЕНА: сверка ставит в очередь только то, что действительно
    # передаётся. Раньше товар здесь создавался с выключенной трансляцией и всё
    # равно попадал в очередь — на площадку по нему уходил ноль.
    p = Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10,
                broadcast_enabled=True)
    db.add(p)
    db.add(Barcode(barcode="111", uid_1c="u1"))
    account = make_account(db)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    stats = run_reconciliation(db, {"111": 12})  # в 1С на 2 больше, чем у Python

    assert stats["auto_plus"] == 1
    db.refresh(p)
    assert p.stock_on_hand == 12

    from app.models import DispatchQueueItem
    queued = db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u1").all()
    assert len(queued) == 1
    assert queued[0].quantity == 12
    assert queued[0].reason == "reconciliation"


def test_run_reconciliation_large_delta_now_applied(db):
    """По решению оператора применяем ЛЮБОЕ складское движение, любого размера:
    даже крупная дельта (в журнале остаётся needs_review) теперь обновляет остаток."""
    p = Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10)
    db.add(p)
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()

    stats = run_reconciliation(db, {"111": 100})  # крупный приход

    assert stats["needs_review"] == 1  # классификация для журнала сохранена
    db.refresh(p)
    assert p.stock_on_hand == 100  # но остаток применён


def test_run_reconciliation_moves_manual_override_with_warehouse(db):
    """Ручная цифра transmit_override «дышит» вместе со складом: складская дельта
    (приход +, расход −) двигает и остаток, и override на ту же величину."""
    p = Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10, transmit_override=3)
    db.add(p)
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()

    run_reconciliation(db, {"111": 15})   # приход: delta +5
    db.refresh(p)
    assert p.stock_on_hand == 15
    assert p.transmit_override == 8       # 3 + 5

    run_reconciliation(db, {"111": 12})   # расход: delta −3
    db.refresh(p)
    assert p.stock_on_hand == 12
    assert p.transmit_override == 5       # 8 − 3


def test_run_reconciliation_accounts_for_in_flight_tasks(db):
    """Товар отправлен на списание через FTP, ещё нет ответа от 1С —
    сверка не должна считать это расхождением."""
    p = Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=8)  # уже списали 2 у себя
    db.add(p)
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(FtpTask(command="CREATE_MOVEMENT", barcode="111", quantity=2,
                    order_id="o1", account_id=make_account(db).id, status=FtpTaskStatus.sent))
    db.commit()

    # 1С физически ещё показывает 10 (задание не обработано) — это ОЖИДАЕМО,
    # не расхождение: expected_1c = 8 (python) + 2 (в пути) = 10
    stats = run_reconciliation(db, {"111": 10})

    assert stats["normal"] == 1


# --------------------------------------------- сверка уважает гейты трансляции

def test_reconciliation_does_not_queue_a_product_with_broadcast_off(db):
    """18.09 на бою по такому товару ушли нули на Озон и Kit. Гейты тогда
    добавили в приём заказа и в `enqueue_full_resend`, а путь сверки остался
    мимо них и обнулял живые карточки каждый час."""
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10,
                   broadcast_enabled=False))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    account = make_account(db)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    run_reconciliation(db, {"111": 12})

    from app.models import DispatchQueueItem
    assert db.query(DispatchQueueItem).count() == 0


def test_reconciliation_does_not_queue_an_account_the_recalc_did_not_cover(db):
    """Ступень 2 лестницы: по непокрытому кабинету уйдёт ноль, а он обнулит
    карточку, на которую мы ещё ничего не отправляли."""
    from app.timeutils import now_utc
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10,
                   broadcast_enabled=True, recalc_done_at=now_utc(),
                   recalc_account_ids=""))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    account = make_account(db)
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    run_reconciliation(db, {"111": 12})

    from app.models import DispatchQueueItem
    assert db.query(DispatchQueueItem).count() == 0


def test_reconciliation_queues_a_covered_account(db):
    """Обратная сторона: покрытый кабинет получить обновление обязан."""
    from app.timeutils import now_utc
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=10,
                   broadcast_enabled=True, recalc_done_at=now_utc(),
                   recalc_account_ids=str(account.id)))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()

    run_reconciliation(db, {"111": 12})

    from app.models import DispatchQueueItem
    rows = db.query(DispatchQueueItem).all()
    assert len(rows) == 1 and rows[0].quantity == 12 and rows[0].reason == "reconciliation"

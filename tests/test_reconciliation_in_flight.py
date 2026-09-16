"""Находка 4 аудита: сверка не вычитала «в пути» и возвращала на склад единицы,
которые площадка уже продала.

Правило учёта, от которого всё считается: заказ с площадки уходит в перемещение
ЦС → склад площадки СРАЗУ, подтверждения с нашей стороны не ждём. Остаток у нас
списывается в момент приёма заказа, а документ в 1С появляется только когда 1С
обработает задание и пришлёт результат. Пока задание не закрыто, 1С показывает
единицы, которых у нас уже нет, — это и есть «в пути». Возврат на ЦС бывает
только по НАШЕЙ отмене (CANCEL_MOVEMENT) и работает симметрично.
"""
from datetime import timedelta

from app.models import (Product, Barcode, FtpTask, FtpTaskStatus, SyncSetting,
                        DispatchQueueItem, ReconciliationLog)
from app.models import ReconciliationClassification as C
from app.timeutils import now_utc
from app.workers.reconciliation import run_reconciliation, _in_flight_adjustment
from tests.factories import make_account


def _product(db, stock: int, uid: str = "u1", barcode: str = "111", **kw) -> Product:
    p = Product(uid_1c=uid, article="A1", name="Товар", stock_on_hand=stock, **kw)
    db.add(p)
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.commit()
    return p


def _task(db, command: str, quantity: int, status=FtpTaskStatus.sent,
          completed_at=None, barcode: str = "111", order: str = "o1", is_test: bool = False):
    task = FtpTask(command=command, barcode=barcode, quantity=quantity, order_id=order,
                   account_id=make_account(db).id, status=status,
                   completed_at=completed_at, is_test=is_test)
    db.add(task)
    db.commit()
    return task


# --------------------------------------------------- сам дефект (находка 4)

def test_open_task_units_are_not_returned_to_stock(db):
    """Сценарий из аудита дословно: было 10, заказ на 2 (задание не закрыто),
    в магазине продали 1 мимо площадок, 1С отдала 9.

    Правильный остаток — 7: девять по учёту 1С минус две единицы, которые 1С ещё
    не списала. Раньше присваивалось сырое 9, и эти две единицы уходили на
    площадки второй раз."""
    p = _product(db, stock=8)          # 10 − 2 списано при приёме заказа
    _task(db, "CREATE_MOVEMENT", 2)    # задание в 1С ещё не проведено

    stats = run_reconciliation(db, {"111": 9})

    db.refresh(p)
    assert p.stock_on_hand == 7
    assert stats["auto_minus"] == 1


def test_open_task_units_are_not_resent_to_platforms(db):
    """Та же ситуация со стороны площадки: в очередь должен уйти исправленный
    остаток, а не сырое число из 1С."""
    _product(db, stock=8)
    account = make_account(db, name="Кабинет получателя")
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    _task(db, "CREATE_MOVEMENT", 2)

    run_reconciliation(db, {"111": 9})

    queued = db.query(DispatchQueueItem).filter(DispatchQueueItem.uid_1c == "u1").all()
    assert [q.quantity for q in queued] == [7]


def test_error_equals_sum_of_open_tasks(db):
    """Величина прежней ошибки равна сумме незакрытых заданий — проверяем на трёх
    заданиях сразу, чтобы поведение не зависело от их количества."""
    _product(db, stock=5)              # было 12, ушло 7 по трём заказам
    _task(db, "CREATE_MOVEMENT", 3, order="o1")
    _task(db, "CREATE_MOVEMENT", 2, order="o2")
    _task(db, "CREATE_MOVEMENT", 2, order="o3")

    run_reconciliation(db, {"111": 11})   # 12 в 1С минус 1 проданная в магазине

    assert db.query(Product).filter(Product.uid_1c == "u1").first().stock_on_hand == 4


def test_stock_moves_exactly_by_warehouse_delta(db):
    """Тождество, на котором держится вся сверка: остаток двигается ровно на
    складскую дельту, сколько бы ни было «в пути». Для transmit_override это уже
    было так — теперь и для самого остатка."""
    p = _product(db, stock=8, transmit_override=6)
    _task(db, "CREATE_MOVEMENT", 2)

    run_reconciliation(db, {"111": 9})    # дельта склада = −1

    db.refresh(p)
    assert p.stock_on_hand == 7           # 8 − 1
    assert p.transmit_override == 5       # 6 − 1


def test_cancellation_in_flight_is_symmetric(db):
    """Наша отмена вернула товар на ЦС у нас, 1С ещё нет: «в пути» отрицательный,
    и вычитание должно прибавить единицы обратно."""
    p = _product(db, stock=10)               # 8 + 2 возвращённых по нашей отмене
    _task(db, "CANCEL_MOVEMENT", 2)

    run_reconciliation(db, {"111": 7})       # 8 в 1С минус 1 проданная в магазине

    db.refresh(p)
    assert p.stock_on_hand == 9              # 10 − 1


def test_zero_delta_still_changes_nothing(db):
    """Когда расхождения нет, сверка не должна трогать ни остаток, ни очередь —
    поправка на «в пути» не должна это сломать."""
    p = _product(db, stock=8)
    _task(db, "CREATE_MOVEMENT", 2)

    stats = run_reconciliation(db, {"111": 10})

    db.refresh(p)
    assert stats["normal"] == 1
    assert p.stock_on_hand == 8
    assert db.query(DispatchQueueItem).count() == 0


def test_oversold_stock_stays_negative(db):
    """Магазин продал глубже, чем мы успели отгрузить. Честный остаток —
    отрицательный (на площадки при этом уходит 0); прятать это нулём нельзя,
    иначе пересортица не видна ни в журнале, ни оператору."""
    p = _product(db, stock=0)
    _task(db, "CREATE_MOVEMENT", 3)

    run_reconciliation(db, {"111": 1})       # в 1С осталась 1 из трёх «в пути»

    db.refresh(p)
    assert p.stock_on_hand == -2


# ------------------------------------- «в пути» считается НА МОМЕНТ СНИМКА

def test_task_closed_after_snapshot_still_counts_as_in_flight(db):
    """Штатное расписание оставляет ~5 минут между выгрузкой и сверкой. Задание,
    закрытое в этом промежутке, в снимок ещё не попало: считать его проведённым
    значило бы принять уже отгруженные единицы за приход на склад."""
    p = _product(db, stock=8)
    snapshot_at = now_utc() - timedelta(minutes=5)
    _task(db, "CREATE_MOVEMENT", 2, status=FtpTaskStatus.done,
          completed_at=snapshot_at + timedelta(minutes=1))

    stats = run_reconciliation(db, {"111": 10}, snapshot_at=snapshot_at)

    db.refresh(p)
    assert stats["normal"] == 1      # расхождения нет: 8 + 2 «в пути» = 10
    assert p.stock_on_hand == 8


def test_task_closed_before_snapshot_is_not_in_flight(db):
    """Обратный случай: документ в 1С уже проведён к моменту выгрузки, значит
    снимок его учитывает и вычитать эти единицы второй раз нельзя."""
    p = _product(db, stock=8)
    snapshot_at = now_utc() - timedelta(minutes=5)
    _task(db, "CREATE_MOVEMENT", 2, status=FtpTaskStatus.done,
          completed_at=snapshot_at - timedelta(minutes=10))

    stats = run_reconciliation(db, {"111": 8}, snapshot_at=snapshot_at)

    db.refresh(p)
    assert stats["normal"] == 1
    assert p.stock_on_hand == 8


def test_without_snapshot_time_behaviour_is_the_old_one(db):
    """Время снимка может быть неизвестно (разовый скрипт, файловая система не
    отдала mtime) — тогда считаем «в пути» только по открытым сейчас заданиям."""
    completed = now_utc() - timedelta(minutes=1)
    _product(db, stock=8)
    _task(db, "CREATE_MOVEMENT", 2, status=FtpTaskStatus.done, completed_at=completed)

    assert _in_flight_adjustment(db, "u1") == 0
    assert _in_flight_adjustment(db, "u1", completed - timedelta(minutes=5)) == 2


def test_test_tasks_never_affect_reconciliation(db):
    """Граница is_test: симулированный заказ со страницы «Тестирование» не должен
    попадать в «в пути» ни как открытое задание, ни как закрытое после снимка."""
    snapshot_at = now_utc() - timedelta(minutes=5)
    _product(db, stock=8)
    _task(db, "CREATE_MOVEMENT", 2, status=FtpTaskStatus.done,
          completed_at=snapshot_at + timedelta(minutes=1), is_test=True)
    _task(db, "CREATE_MOVEMENT", 5, order="o2", is_test=True)

    assert _in_flight_adjustment(db, "u1", snapshot_at) == 0


def test_reconciliation_log_keeps_the_arithmetic(db):
    """В журнале сверки должно остаться всё, чтобы задним числом восстановить
    расчёт: остаток до, «в пути», ожидание, факт 1С и дельта."""
    _product(db, stock=8)
    _task(db, "CREATE_MOVEMENT", 2)

    run_reconciliation(db, {"111": 9})

    log = db.query(ReconciliationLog).filter(ReconciliationLog.uid_1c == "u1").first()
    assert (log.python_stock, log.in_flight, log.expected_1c, log.actual_1c, log.delta) == (8, 2, 10, 9, -1)
    assert log.classification == C.auto_minus
    assert log.resolved is True

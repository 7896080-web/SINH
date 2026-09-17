"""Находка 5 аудита: ошибка отправки на площадку была терминальной.

Запись со статусом `error` не возвращалась в `pending` нигде в проекте: API
ответил ошибкой один раз — и следующий цикл при уже живом API не отправлял
ничего. Остаток списан у нас, площадка о нём не узнала до следующего события по
товару, то есть продолжала продавать то, чего нет.

Теперь сбой не терминален: запись остаётся `pending` и повторяется с нарастающей
паузой, а `error` ставится только когда попытки исчерпаны.
"""
from datetime import timedelta

from app.models import (Product, Barcode, SyncSetting, DispatchQueueItem, DispatchStatus)
from app.timeutils import now_utc
from app.workers.dispatch import run_dispatch_cycle, MAX_ATTEMPTS, _retry_delay
from tests.factories import make_account


class FlakyClient:
    """Площадка, которая отвечает ошибкой заданное число первых раз."""

    def __init__(self, fail_times: int = 1):
        self.fail_times = fail_times
        self.calls = []

    def push_stock(self, warehouse_id, items):
        self.calls.append([(i.barcode, i.quantity) for i in items])
        if self.fail_times > 0:
            self.fail_times -= 1
            return {"ok": [], "errors": [{"barcode": i.barcode, "error": "503 от площадки"} for i in items]}
        return {"ok": [i.barcode for i in items], "errors": []}


def _seed(db, stock: int = 7, uid: str = "u1", barcode: str = "111"):
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(uid_1c=uid, article="A1", name="Товар", stock_on_hand=stock, broadcast_enabled=True))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))
    db.commit()
    return account


def _queue(db, account, quantity: int, uid: str = "u1", reason: str = "order") -> DispatchQueueItem:
    item = DispatchQueueItem(uid_1c=uid, account_id=account.id, quantity=quantity, reason=reason)
    db.add(item)
    db.commit()
    return item


def _due(db, item: DispatchQueueItem):
    """Промотать паузу: цикл ходит раз в 30-60 секунд, а тест ждать не должен."""
    item.next_attempt_at = now_utc() - timedelta(seconds=1)
    db.commit()


# ------------------------------------------------------- сам дефект

def test_failed_item_stays_pending_and_is_retried(db):
    """Сценарий из аудита: API ответил ошибкой, следующий цикл при живом API
    обязан дослать. Раньше запись навсегда оставалась в `error`."""
    account = _seed(db)
    item = _queue(db, account, 7)
    client = FlakyClient(fail_times=1)

    run_dispatch_cycle(db, {account.id: client}, [account])
    db.refresh(item)
    assert item.status == DispatchStatus.pending      # не error
    assert item.attempts == 1

    _due(db, item)
    run_dispatch_cycle(db, {account.id: client}, [account])

    db.refresh(item)
    assert item.status == DispatchStatus.sent
    assert item.sent_at is not None
    assert item.next_attempt_at is None
    assert len(client.calls) == 2
    assert client.calls[1] == [("111", 7)]


def test_error_only_after_attempts_are_exhausted(db):
    """Площадка отвечает ошибкой всегда: запись обязана в конце концов стать
    ошибкой, иначе мы будем долбить её бесконечно и прятать проблему."""
    account = _seed(db)
    item = _queue(db, account, 7)
    client = FlakyClient(fail_times=99)

    for _ in range(MAX_ATTEMPTS):
        _due(db, item)
        run_dispatch_cycle(db, {account.id: client}, [account])
        db.refresh(item)

    assert item.status == DispatchStatus.error
    assert item.attempts == MAX_ATTEMPTS
    assert "не отправлено за" in item.last_error
    assert len(client.calls) == MAX_ATTEMPTS


def test_terminal_error_is_not_retried_again(db):
    """Исчерпав попытки, запись выходит из работы — иначе цикл вечно возит
    заведомо неотправимое значение."""
    account = _seed(db)
    item = _queue(db, account, 7)
    client = FlakyClient(fail_times=99)

    for _ in range(MAX_ATTEMPTS + 3):
        _due(db, item)
        run_dispatch_cycle(db, {account.id: client}, [account])
        db.refresh(item)

    assert len(client.calls) == MAX_ATTEMPTS


def test_pause_between_attempts_is_respected(db):
    """Пауза не декоративная: до её конца площадку не трогаем совсем."""
    account = _seed(db)
    item = _queue(db, account, 7)
    client = FlakyClient(fail_times=99)

    run_dispatch_cycle(db, {account.id: client}, [account])
    db.refresh(item)
    assert item.next_attempt_at > now_utc()

    run_dispatch_cycle(db, {account.id: client}, [account])   # сразу же, пауза не вышла

    assert len(client.calls) == 1
    db.refresh(item)
    assert item.attempts == 1


def test_pause_grows_with_each_failure(db):
    assert _retry_delay(1) < _retry_delay(2) < _retry_delay(3) < _retry_delay(4)
    assert _retry_delay(99) == _retry_delay(4)   # дальше не растёт


def test_last_error_shows_attempt_number(db):
    account = _seed(db)
    item = _queue(db, account, 7)
    client = FlakyClient(fail_times=99)

    run_dispatch_cycle(db, {account.id: client}, [account])

    db.refresh(item)
    assert item.last_error.startswith(f"попытка 1 из {MAX_ATTEMPTS}")
    assert "503 от площадки" in item.last_error


# ----------------------------------- повтор не воскрешает устаревшее значение

def test_newer_value_supersedes_an_item_waiting_for_retry(db):
    """Главный риск повторов: отложенная после сбоя запись не должна пережить
    более новое изменение остатка и позже отправить на площадку старое число."""
    account = _seed(db, stock=3)
    stale = _queue(db, account, 7)
    client = FlakyClient(fail_times=1)

    run_dispatch_cycle(db, {account.id: client}, [account])   # 7 не ушло, ждёт повтора
    db.refresh(stale)
    assert stale.status == DispatchStatus.pending

    fresh = _queue(db, account, 3, reason="order")            # пришёл новый заказ
    run_dispatch_cycle(db, {account.id: client}, [account])

    db.refresh(stale)
    db.refresh(fresh)
    assert stale.status == DispatchStatus.sent
    assert stale.last_error == "поглощено более новым изменением в этом цикле"
    assert fresh.status == DispatchStatus.sent
    assert client.calls[-1] == [("111", 3)]                   # ушло новое значение, не 7


def test_waiting_item_blocks_older_values_from_going_out(db):
    """Обратный порядок: самая свежая запись на паузе, более старая — готова.
    Отправлять старое значение нельзя, ждём повтора свежего."""
    account = _seed(db, stock=3)
    old = _queue(db, account, 7)
    client = FlakyClient(fail_times=1)
    run_dispatch_cycle(db, {account.id: client}, [account])   # 7 сорвалось и ждёт

    db.refresh(old)
    older_value_not_sent = old.status == DispatchStatus.pending
    assert older_value_not_sent

    run_dispatch_cycle(db, {account.id: client}, [account])   # пауза не вышла

    assert len(client.calls) == 1


# ------------------------------------------ постоянные сбои остаются терминальными

def test_missing_barcode_is_still_terminal(db):
    """Отсутствие баркода повтором не лечится — это не сбой связи, а нечего
    отправлять. Такая запись обязана сразу становиться ошибкой."""
    account = make_account(db, warehouse_id="wh-1")
    db.add(Product(uid_1c="u1", article="A1", name="Без баркода", stock_on_hand=5, broadcast_enabled=True))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    item = _queue(db, account, 5)

    run_dispatch_cycle(db, {account.id: FlakyClient(fail_times=0)}, [account])

    db.refresh(item)
    assert item.status == DispatchStatus.error
    assert item.last_error == "нет баркода для отправки"


def test_successful_send_is_unchanged(db):
    """Штатный путь не должен пострадать: с первой попытки — отправлено, без
    паузы и без счётчика повторов."""
    account = _seed(db)
    item = _queue(db, account, 7)

    stats = run_dispatch_cycle(db, {account.id: FlakyClient(fail_times=0)}, [account])

    db.refresh(item)
    assert item.status == DispatchStatus.sent
    assert item.attempts == 1
    assert item.next_attempt_at is None
    assert stats[account.name]["retry"] == 0


def test_stats_report_retries(db):
    account = _seed(db)
    _queue(db, account, 7)

    stats = run_dispatch_cycle(db, {account.id: FlakyClient(fail_times=99)}, [account])

    assert stats[account.name]["retry"] == 1
    assert stats[account.name]["sent"] == 0

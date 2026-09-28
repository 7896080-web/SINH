"""Перепроведение зависших перемещений — и всё, чего оно делать НЕ должно.

19.09 два файла заданий из пятидесяти трёх 1С забрала и не ответила по ним ни
одной строкой. Двенадцать перемещений навсегда остались «в пути»: остаток по
одиннадцати товарам занижен, а закрыть задание нечем — `timeout` значит
«неизвестно, создан документ или нет», и слепой повтор завёл бы ВТОРОЙ документ.

Механизм опирается на идемпотентность 1С по номеру заказа: документ с этим
номером уже есть — обработка возвращает OK и второго не создаёт. Поэтому повтор
безопасен и одновременно служит проверкой. Пока такой идемпотентности нет,
механизм обязан молчать — это и проверяется первым.
"""
from datetime import timedelta

import pytest

from app.models import FtpTask, FtpTaskStatus, Platform, StockDateSnapshot, StockDateStatus
from app.timeutils import now_utc
from app.workers import ftp_channel as ch
from tests.factories import make_account


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(ch.MOVEMENT_REPOST_ENV, "1")


def _task(db, account, *, status=FtpTaskStatus.timeout, command="CREATE_MOVEMENT",
          age_minutes=60, reposts=0, is_test=False, order_id="5638678769"):
    t = FtpTask(command=command, barcode="2000932307169", warehouse_from="ЦС Склад",
                warehouse_to="Wildberries_Склад_FBO", quantity=1, order_id=order_id,
                account_id=account.id, status=status, repost_count=reposts,
                is_test=is_test, sent_at=now_utc() - timedelta(minutes=age_minutes))
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# --------------------------------------------- выключено, пока 1С не идемпотентна

def test_disabled_by_default_nothing_is_reposted(db):
    """Самый важный тест здесь. Пока обработка 1С не проверяет номер заказа,
    повтор завёл бы второй документ перемещения на боевой базе."""
    acc = make_account(db, Platform.wb)
    t = _task(db, acc)

    stats = ch.repost_stuck_movements(db)

    db.refresh(t)
    assert t.status is FtpTaskStatus.timeout
    assert t.repost_count == 0
    assert stats["reposted"] == 0
    assert stats["skipped_disabled"] == 1     # но проблема посчитана, а не забыта


def test_the_switch_is_read_at_call_time(db, monkeypatch):
    acc = make_account(db, Platform.wb)
    _task(db, acc)

    monkeypatch.setenv(ch.MOVEMENT_REPOST_ENV, "1")
    assert ch.repost_enabled() is True
    monkeypatch.setenv(ch.MOVEMENT_REPOST_ENV, "0")
    assert ch.repost_enabled() is False


# --------------------------------------------- что повторяем, а что нет

def test_a_stuck_movement_goes_back_to_the_queue(db, on):
    acc = make_account(db, Platform.wb)
    t = _task(db, acc)

    stats = ch.repost_stuck_movements(db)

    db.refresh(t)
    assert t.status is FtpTaskStatus.pending
    assert t.repost_count == 1
    assert t.batch_filename is None and t.sent_at is None
    assert stats["reposted"] == 1


def test_a_fresh_timeout_waits(db, on):
    """Ответ 1С штатно приходит за 4–7 минут, а опоздавший закрывает задание сам.
    Повторять сразу — значит гонять то, что вот-вот закроется."""
    acc = make_account(db, Platform.wb)
    t = _task(db, acc, age_minutes=5)

    ch.repost_stuck_movements(db)

    db.refresh(t)
    assert t.status is FtpTaskStatus.timeout


def test_a_refusal_is_never_reposted(db, on):
    """`failed` — внятный отказ 1С: документа заведомо нет, повтор ничего не
    проверяет. Это разбирает человек."""
    acc = make_account(db, Platform.wb)
    t = _task(db, acc, status=FtpTaskStatus.failed)

    ch.repost_stuck_movements(db)

    db.refresh(t)
    assert t.status is FtpTaskStatus.failed


def test_a_cancellation_is_never_reposted(db, on):
    """Идемпотентность обещана по созданию; про отмену такой договорённости нет."""
    acc = make_account(db, Platform.wb)
    t = _task(db, acc, command="CANCEL_MOVEMENT")

    ch.repost_stuck_movements(db)

    db.refresh(t)
    assert t.status is FtpTaskStatus.timeout


def test_a_test_task_is_never_reposted(db, on):
    """Граница is_test: симуляция не должна создать документ в реальной 1С."""
    acc = make_account(db, Platform.wb)
    t = _task(db, acc, is_test=True)

    ch.repost_stuck_movements(db)

    db.refresh(t)
    assert t.status is FtpTaskStatus.timeout


def test_repeats_are_bounded(db, on):
    """Исчерпав предел, задание остаётся timeout и ждёт человека, а не молотит."""
    acc = make_account(db, Platform.wb)
    t = _task(db, acc, reposts=ch.MAX_REPOSTS)

    stats = ch.repost_stuck_movements(db)

    db.refresh(t)
    assert t.status is FtpTaskStatus.timeout
    assert stats["reposted"] == 0
    assert stats["exhausted"] == 1


def test_one_cycle_takes_at_most_a_small_batch(db, on):
    acc = make_account(db, Platform.wb)
    for i in range(10):
        _task(db, acc, order_id=f"order-{i}")

    stats = ch.repost_stuck_movements(db)

    assert stats["reposted"] == ch.REPOST_BATCH_LINES


# --------------------------------------------- повтор едет один, ни с чем не смешиваясь

def _pending(db, account, **kw):
    return _task(db, account, status=FtpTaskStatus.pending, **kw)


def test_a_repost_never_shares_a_file_with_fresh_tasks(db):
    """Ради этого всё и затевалось: 1С роняет файл ЦЕЛИКОМ, и строка, на которой
    она спотыкается, утащила бы за собой свежие перемещения."""
    acc = make_account(db, Platform.wb)
    _pending(db, acc, reposts=1, order_id="repost-1")
    _pending(db, acc, reposts=0, order_id="fresh-1")
    _pending(db, acc, reposts=0, order_id="fresh-2")

    _, content = ch.build_task_batch(db)

    assert "repost-1" in content
    assert "fresh-1" not in content and "fresh-2" not in content


def test_fresh_tasks_never_carry_a_repost(db):
    acc = make_account(db, Platform.wb)
    _pending(db, acc, reposts=0, order_id="fresh-1")

    _, content = ch.build_task_batch(db)

    assert "fresh-1" in content


def test_the_hourly_export_request_is_not_swallowed_by_a_repost(db):
    """Повтор не должен проглотить часовой запрос выгрузки: иначе сверка встала бы
    на час, и мы починили бы одно, сломав другое."""
    acc = make_account(db, Platform.wb)
    _pending(db, acc, reposts=1, order_id="repost-1")

    _, content = ch.build_task_batch(db, request_stock_export=True)

    assert "EXPORT_STOCK_ON_HAND" in content
    assert "repost-1" not in content       # повтор уедет следующим минутным файлом


def test_a_date_request_does_not_ride_with_a_repost(db):
    acc = make_account(db, Platform.wb)
    _pending(db, acc, reposts=1, order_id="repost-1")
    db.add(StockDateSnapshot(snapshot_date=now_utc().date(), status=StockDateStatus.pending))
    db.commit()

    _, content = ch.build_task_batch(db)

    assert "repost-1" in content
    assert ch.STOCK_ON_DATE_COMMAND not in content

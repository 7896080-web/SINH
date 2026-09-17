"""Находки 8, 9, 10 аудита — канал обмена с 1С.

 8. Ответ `ERROR` закрывал задание как успешное: документа в 1С нет, а система
    считала заказ проведённым.
 9. Ответ сопоставлялся только по номеру заказа — среди всех кабинетов и команд,
    и бралось самое свежее задание. Ответ мог закрыть чужое.
10. Имя файла задания было с точностью до секунды: два батча в одну секунду
    получали одно имя, второй затирал первый через replace — а строки затёртого
    уже были помечены отправленными и в 1С не попадали никогда.
"""
import pytest

from app.models import (Product, Barcode, FtpTask, FtpTaskStatus, Platform, SyncSetting)
from app.timeutils import now_utc
from app.workers import ftp_channel
from app.workers.ftp_channel import (LocalExchange, apply_result_batch, build_task_batch,
                                     parse_result_line, _unique_task_filename)
from app.workers.reconciliation import run_reconciliation, _in_flight_adjustment
from tests.factories import make_account


def _exchange(tmp_path) -> LocalExchange:
    ex = LocalExchange(tmp_path / "t", tmp_path / "r", tmp_path / "a")
    ex._ensure_dirs()
    return ex


def _task(db, account, order: str = "o1", command: str = "CREATE_MOVEMENT",
          quantity: int = 2, status=FtpTaskStatus.sent, sent_at=None,
          barcode: str = "111") -> FtpTask:
    task = FtpTask(command=command, barcode=barcode, quantity=quantity, order_id=order,
                   account_id=account.id, status=status, sent_at=sent_at or now_utc())
    db.add(task)
    db.commit()
    return task


# ------------------------------------------------ 8. ERROR — это не успех

def test_error_answer_does_not_close_task_as_done(db):
    """Сценарий из аудита: `e1|ERROR|Не найден склад «…»`. Документа в 1С нет,
    поэтому и задание не должно выглядеть выполненным."""
    account = make_account(db)
    task = _task(db, account, order="e1")

    stats = apply_result_batch(db, "e1|ERROR|Не найден склад «ЦС Склад»")

    db.refresh(task)
    assert task.status == FtpTaskStatus.failed
    assert task.result_status == "ERROR"
    assert "Не найден склад" in task.result_detail
    assert task.completed_at is not None
    assert stats == {"ok": 0, "error": 1, "unmatched": 0}


def test_ok_answer_still_closes_task_as_done(db):
    account = make_account(db)
    task = _task(db, account, order="o1")

    stats = apply_result_batch(db, "o1|OK|ЦБ000000186")

    db.refresh(task)
    assert task.status == FtpTaskStatus.done
    assert task.result_detail == "ЦБ000000186"
    assert stats["ok"] == 1


def test_failed_task_still_counts_as_in_flight(db):
    """Главное следствие: 1С документа не создала, значит у себя она товар не
    списала. Если такое задание перестанет считаться «в пути», сверка вернёт уже
    проданные единицы на склад и отправит их на площадки второй раз."""
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=8, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()
    _task(db, account, order="e1")
    apply_result_batch(db, "e1|ERROR|склад не найден")

    assert _in_flight_adjustment(db, "u1") == 2

    run_reconciliation(db, {"111": 10})           # 1С всё ещё показывает 10

    product = db.query(Product).filter(Product.uid_1c == "u1").first()
    assert product.stock_on_hand == 8             # не 10: две единицы уже проданы


def test_timed_out_task_also_counts_as_in_flight(db):
    """Ответа нет вообще: создан документ или нет — неизвестно. Считаем «в пути»
    по той же причине: ошибиться можно только в сторону недоотправки."""
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=8, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()
    _task(db, account, order="t1", status=FtpTaskStatus.timeout)

    assert _in_flight_adjustment(db, "u1") == 2


def test_done_task_is_not_in_flight(db):
    """Обратная граница: документ создан — единицы 1С уже списала, повторно
    вычитать их нельзя."""
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=8, broadcast_enabled=True))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()
    _task(db, account, order="o1")
    apply_result_batch(db, "o1|OK|ЦБ000000186")

    assert _in_flight_adjustment(db, "u1") == 0


def test_failed_task_blocks_backfill_reset(db):
    """Сброс истории при неразобранном отказе 1С запрещён — иначе отказ потеряется
    вместе с заказом."""
    from app.routers.testing import _open_1c_tasks

    account = make_account(db)
    _task(db, account, order="e1", status=FtpTaskStatus.failed)

    class _Order:
        order_id = "e1"
        account_id = account.id

    assert _open_1c_tasks(db, [_Order()]) == 1


# --------------------------------- 9. сопоставление ответа с заданием

def test_answer_closes_the_oldest_open_task_not_the_newest(db):
    """Два кабинета с одинаковым номером заказа. 1С отвечает в том же порядке, в
    каком получила строки, поэтому единственный ответ принадлежит самому раннему
    незакрытому заданию. Раньше бралось самое свежее — закрывалось чужое."""
    a1 = make_account(db, name="Кабинет 1")
    a2 = make_account(db, platform=Platform.ozon, name="Кабинет 2")
    from datetime import timedelta
    first = _task(db, a1, order="12345", sent_at=now_utc() - timedelta(minutes=5))
    second = _task(db, a2, order="12345", sent_at=now_utc())

    apply_result_batch(db, "12345|OK|ЦБ-1")

    db.refresh(first)
    db.refresh(second)
    assert first.status == FtpTaskStatus.done
    assert second.status == FtpTaskStatus.sent      # чужое задание не тронуто


def test_two_answers_close_two_different_tasks(db):
    """Пришли оба ответа — закрыться должны оба задания, а не одно дважды."""
    a1 = make_account(db, name="Кабинет 1")
    a2 = make_account(db, platform=Platform.ozon, name="Кабинет 2")
    from datetime import timedelta
    first = _task(db, a1, order="12345", sent_at=now_utc() - timedelta(minutes=5))
    second = _task(db, a2, order="12345", sent_at=now_utc())

    stats = apply_result_batch(db, "12345|OK|ЦБ-1\n12345|OK|ЦБ-2")

    db.refresh(first)
    db.refresh(second)
    assert (first.status, first.result_detail) == (FtpTaskStatus.done, "ЦБ-1")
    assert (second.status, second.result_detail) == (FtpTaskStatus.done, "ЦБ-2")
    assert stats["ok"] == 2


def test_command_in_answer_makes_matching_exact(db):
    """Если обработка 1С приписывает к ответу имя команды, сопоставление
    перестаёт быть догадкой: по одному номеру заказа у нас бывает и создание, и
    отмена, и перепутать их нельзя."""
    account = make_account(db)
    from datetime import timedelta
    create = _task(db, account, order="o1", command="CREATE_MOVEMENT",
                   sent_at=now_utc() - timedelta(minutes=5))
    cancel = _task(db, account, order="o1", command="CANCEL_MOVEMENT", sent_at=now_utc())

    apply_result_batch(db, "o1|OK|ЦБ-отмена|CANCEL_MOVEMENT")

    db.refresh(create)
    db.refresh(cancel)
    assert cancel.status == FtpTaskStatus.done
    assert create.status == FtpTaskStatus.sent      # создание всё ещё ждёт своего ответа


def test_unmatched_answer_is_counted_not_applied(db):
    account = make_account(db)
    _task(db, account, order="o1")

    stats = apply_result_batch(db, "чужой-заказ|OK|ЦБ-9")

    assert stats["unmatched"] == 1
    assert db.query(FtpTask).first().status == FtpTaskStatus.sent


@pytest.mark.parametrize("line,expected", [
    ("o1|OK|ЦБ-1", ("o1", "OK", "ЦБ-1", "")),
    ("o1|ERROR|склад не найден", ("o1", "ERROR", "склад не найден", "")),
    ("o1|OK|ЦБ-1|CREATE_MOVEMENT", ("o1", "OK", "ЦБ-1", "CREATE_MOVEMENT")),
    # подробность с разделителем не должна быть принята за команду
    ("o1|ERROR|склад «А|Б» не найден", ("o1", "ERROR", "склад «А|Б» не найден", "")),
    ("мусор", None),
])
def test_parse_result_line(line, expected):
    assert parse_result_line(line) == expected


# ------------------------------------------- 10. имя файла задания

def test_two_batches_in_the_same_second_get_different_names(db, tmp_path, monkeypatch):
    """Сценарий из аудита: минутная отправка и суточный запрос справочника
    попали в одну секунду. Раньше оба файла назывались одинаково."""
    ex = _exchange(tmp_path)
    account = make_account(db)
    frozen = now_utc()
    monkeypatch.setattr(ftp_channel, "now_utc", lambda: frozen)   # часы стоят

    _task(db, account, order="o1", status=FtpTaskStatus.pending)
    name1, body1 = build_task_batch(db, exchange=ex)
    ex.upload_task_file(name1, body1)

    _task(db, account, order="o2", status=FtpTaskStatus.pending)
    name2, body2 = build_task_batch(db, exchange=ex)
    ex.upload_task_file(name2, body2)

    assert name1 != name2
    assert sorted(p.name for p in ex.dir_tasks.glob("task_*.txt")) == sorted([name1, name2])


def test_upload_refuses_to_overwrite_existing_task_file(tmp_path):
    """Затирание — не штатный ход: строки внутри уже помечены отправленными, и
    молча потерять их нельзя."""
    ex = _exchange(tmp_path)
    ex.upload_task_file("task_20260917120000000000.txt", "CREATE_MOVEMENT|111|ЦС Склад|WB|1|o1|wb|")

    with pytest.raises(FileExistsError):
        ex.upload_task_file("task_20260917120000000000.txt", "другое содержимое")

    assert (ex.dir_tasks / "task_20260917120000000000.txt").read_text(encoding="utf-8").startswith("CREATE")


def test_archived_name_is_not_reused(tmp_path, monkeypatch):
    """Имя, уже ушедшее в архив, брать нельзя: 1С забирает задания из tasks и
    переносит их в тот же архив, так что столкновение имён там реально."""
    ex = _exchange(tmp_path)
    frozen = now_utc()
    monkeypatch.setattr(ftp_channel, "now_utc", lambda: frozen)
    taken = _unique_task_filename(ex)
    (ex.dir_archive / taken).write_text("уже обработано", encoding="utf-8")

    assert _unique_task_filename(ex) != taken


def test_batch_marks_lines_sent_only_under_the_chosen_name(db, tmp_path, monkeypatch):
    """Имя выбирается ДО пометки строк отправленными: иначе занятое имя означало
    бы, что батч помечен отправленным и при этом потерян."""
    ex = _exchange(tmp_path)
    account = make_account(db)
    frozen = now_utc()
    monkeypatch.setattr(ftp_channel, "now_utc", lambda: frozen)
    (ex.dir_tasks / _unique_task_filename(ex)).write_text("занято", encoding="utf-8")

    _task(db, account, order="o1", status=FtpTaskStatus.pending)
    name, body = build_task_batch(db, exchange=ex)
    ex.upload_task_file(name, body)          # не должно бросить

    task = db.query(FtpTask).filter(FtpTask.order_id == "o1").first()
    assert task.status == FtpTaskStatus.sent
    assert task.batch_filename == name
    assert (ex.dir_tasks / name).exists()

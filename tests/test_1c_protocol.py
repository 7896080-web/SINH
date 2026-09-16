"""Контракт FTP-протокола с обработкой 1С (1c/README.md): приложение должно
принимать ровно те форматы файлов, которые пишет .epf, и отдавать задания в
том виде, который .epf разбирает."""
from app.workers.ftp_channel import parse_stock_export_file, apply_result_batch, build_task_batch
from app.models import FtpTask, FtpTaskStatus
from tests.factories import make_account


def test_stock_export_format_including_negative_quantity():
    # Формат: uid|артикул|наименование|количество|баркод1,баркод2
    content = (
        "u-1|ART-1|Куртка|5|2000000000001,8600000000001\n"
        "u-2|ART-2|Брюки|-3|2000000000002\n"          # отрицательный остаток (пересортица)
        "u-3|ART-3|Без баркода|7|\n"                    # без баркода — пропускается
    )
    parsed = parse_stock_export_file(content)
    assert parsed["2000000000001"] == 5
    assert parsed["8600000000001"] == 5   # оба баркода SKU получают его остаток
    assert parsed["2000000000002"] == -3  # отрицательное значение сохраняется как есть
    assert all(bc for bc in parsed)       # пустых баркодов нет


def test_result_file_closes_task(db):
    account = make_account(db)
    db.add(FtpTask(command="CREATE_MOVEMENT", barcode="111", order_id="o-1",
                   account_id=account.id, status=FtpTaskStatus.sent))
    db.commit()

    stats = apply_result_batch(db, "o-1|OK|Перемещение 000123")
    assert stats["ok"] == 1
    task = db.query(FtpTask).filter(FtpTask.order_id == "o-1").first()
    assert task.status == FtpTaskStatus.done
    assert task.result_status == "OK"
    assert "000123" in task.result_detail


def test_result_file_error_marks_task_error(db):
    account = make_account(db)
    db.add(FtpTask(command="CANCEL_MOVEMENT", order_id="o-2",
                   account_id=account.id, status=FtpTaskStatus.sent))
    db.commit()
    stats = apply_result_batch(db, "o-2|ERROR|Не найден склад")
    assert stats["error"] == 1
    assert db.query(FtpTask).filter(FtpTask.order_id == "o-2").first().result_status == "ERROR"


def test_task_file_export_stock_line(db):
    # EXPORT_STOCK_ON_HAND уходит в файл задания при запросе выгрузки остатков.
    filename, content = build_task_batch(db, request_stock_export=True)
    assert "EXPORT_STOCK_ON_HAND" in content

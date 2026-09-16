from datetime import datetime, timedelta
from app.timeutils import now_utc

from app.models import FtpTask, FtpTaskStatus, Platform
from app.workers.ftp_channel import (
    build_task_batch, apply_result_batch, detect_timed_out_tasks, parse_stock_export_file,
)
from tests.factories import make_account


def test_build_task_batch_formats_create_movement(db):
    account = make_account(db, platform=Platform.wb)
    db.add(FtpTask(command="CREATE_MOVEMENT", barcode="111", warehouse_from="ЦС",
                    warehouse_to="WB.Ожидает", quantity=3, order_id="o1", account_id=account.id))
    db.commit()

    result = build_task_batch(db)
    assert result is not None
    filename, content = result

    assert filename.startswith("task_")
    assert content == "CREATE_MOVEMENT|111|ЦС|WB.Ожидает|3|o1|wb|"  # 8-е поле — дата (пусто = текущая)

    task = db.query(FtpTask).first()
    assert task.status == FtpTaskStatus.sent
    assert task.batch_filename == filename


def test_build_task_batch_formats_cancel_movement(db):
    account = make_account(db, platform=Platform.ozon)
    db.add(FtpTask(command="CANCEL_MOVEMENT", order_id="o1", account_id=account.id))
    db.commit()

    _, content = build_task_batch(db)
    assert content == "CANCEL_MOVEMENT|o1|ozon"


def test_build_task_batch_none_when_nothing_pending(db):
    assert build_task_batch(db) is None


def test_build_task_batch_with_export_request_and_no_tasks(db):
    filename, content = build_task_batch(db, request_stock_export=True)
    assert content == "EXPORT_STOCK_ON_HAND"


def test_apply_result_batch_matches_and_closes_task(db):
    account = make_account(db)
    db.add(FtpTask(command="CREATE_MOVEMENT", barcode="111", order_id="o1",
                    account_id=account.id, status=FtpTaskStatus.sent, sent_at=now_utc()))
    db.commit()

    stats = apply_result_batch(db, "o1|OK|ПЕРМ-000123")

    assert stats == {"ok": 1, "error": 0, "unmatched": 0}
    task = db.query(FtpTask).first()
    assert task.status == FtpTaskStatus.done
    assert task.result_status == "OK"
    assert task.result_detail == "ПЕРМ-000123"


def test_apply_result_batch_handles_error_result(db):
    account = make_account(db)
    db.add(FtpTask(command="CREATE_MOVEMENT", barcode="111", order_id="o1",
                    account_id=account.id, status=FtpTaskStatus.sent, sent_at=now_utc()))
    db.commit()

    stats = apply_result_batch(db, "o1|ERROR|ШтрихКодНеНайден")

    assert stats["error"] == 1
    task = db.query(FtpTask).first()
    assert task.result_status == "ERROR"


def test_apply_result_batch_unmatched_line_does_not_crash(db):
    stats = apply_result_batch(db, "unknown_order|OK|ПЕРМ-000999")
    assert stats["unmatched"] == 1


def test_detect_timed_out_tasks(db):
    account = make_account(db)
    old = FtpTask(command="CREATE_MOVEMENT", barcode="111", order_id="o1", account_id=account.id,
                   status=FtpTaskStatus.sent, sent_at=now_utc() - timedelta(minutes=30))
    fresh = FtpTask(command="CREATE_MOVEMENT", barcode="222", order_id="o2", account_id=account.id,
                     status=FtpTaskStatus.sent, sent_at=now_utc() - timedelta(minutes=2))
    db.add_all([old, fresh])
    db.commit()

    timed_out = detect_timed_out_tasks(db)

    assert len(timed_out) == 1
    assert timed_out[0].order_id == "o1"
    db.refresh(old)
    db.refresh(fresh)
    assert old.status == FtpTaskStatus.timeout
    assert fresh.status == FtpTaskStatus.sent


def test_parse_stock_export_file_flattens_multiple_barcodes():
    content = "u1|ART-1|Товар 1|5|111,112\nu2|ART-2|Товар 2|0|999"
    result = parse_stock_export_file(content)

    assert result == {"111": 5, "112": 5, "999": 0}


# --- Локальный канал (папка вместо FTP) ---

def test_local_exchange_upload_task_file_is_atomic(tmp_path):
    from app.workers.ftp_channel import LocalExchange
    ex = LocalExchange(tmp_path / "tasks", tmp_path / "results", tmp_path / "archive")

    ex.upload_task_file("task_1.txt", "CREATE_MOVEMENT|111|ЦС Склад|Wildberries_Впути|1|o1|wb")

    final = tmp_path / "tasks" / "task_1.txt"
    assert final.read_text(encoding="utf-8").startswith("CREATE_MOVEMENT")
    # временный .part не остаётся
    assert not (tmp_path / "tasks" / "task_1.txt.part").exists()


def test_local_exchange_lists_ignoring_part_and_archives(tmp_path):
    from app.workers.ftp_channel import LocalExchange
    ex = LocalExchange(tmp_path / "tasks", tmp_path / "results", tmp_path / "archive")
    ex._ensure_dirs()
    (tmp_path / "results" / "result_1.txt").write_text("o1|OK|ПЕРМ-1", encoding="utf-8")
    (tmp_path / "results" / "result_2.txt.part").write_text("недописанный", encoding="utf-8")
    (tmp_path / "results" / "stock_1.txt").write_text("u1|A|N|5|111", encoding="utf-8")

    assert ex.list_result_files() == ["result_1.txt"]   # .part и stock_ не берём
    assert ex.list_stock_files() == ["stock_1.txt"]

    content = ex.download_and_archive_result("result_1.txt")
    assert content == "o1|OK|ПЕРМ-1"
    assert not (tmp_path / "results" / "result_1.txt").exists()
    assert (tmp_path / "archive" / "result_1.txt").exists()


def test_fetch_stock_export_files_combines_and_archives(tmp_path):
    from app.workers.ftp_channel import LocalExchange, fetch_stock_export_files
    ex = LocalExchange(tmp_path / "tasks", tmp_path / "results", tmp_path / "archive")
    ex._ensure_dirs()
    (tmp_path / "results" / "stock_1.txt").write_text("u1|A|N|5|111,112", encoding="utf-8")

    combined = fetch_stock_export_files(ex)

    assert combined == {"111": 5, "112": 5}
    assert (tmp_path / "archive" / "stock_1.txt").exists()


def test_local_exchange_strips_bom_written_by_1c(tmp_path):
    """1С пишет result_/stock_/barcodes_ через ЗаписьТекста(UTF8) — с BOM. Первая строка
    должна разбираться так же, как остальные (раньше BOM прилипал к order_id первой
    строки, и первое задание каждого файла оставалось 'sent' навсегда)."""
    from app.workers.ftp_channel import LocalExchange
    ex = LocalExchange(tmp_path / "tasks", tmp_path / "results", tmp_path / "archive")
    ex._ensure_dirs()
    (tmp_path / "results" / "result_1.txt").write_bytes("﻿o1|OK|ЦБ1\no2|OK|ЦБ2".encode("utf-8"))
    content = ex.download_and_archive_result("result_1.txt")
    assert content.splitlines()[0] == "o1|OK|ЦБ1"


def test_apply_result_batch_first_line_with_bom_closes_task(db):
    """Сквозная проверка: файл с BOM, прочитанный через LocalExchange, закрывает
    задание из ПЕРВОЙ строки."""
    from app.workers.ftp_channel import LocalExchange, apply_result_batch
    import tempfile, pathlib
    account = make_account(db, Platform.wb)
    task = FtpTask(command="CREATE_MOVEMENT", barcode="111", quantity=1, order_id="first",
                   account_id=account.id, status=FtpTaskStatus.sent, sent_at=now_utc())
    db.add(task)
    db.commit()
    root = pathlib.Path(tempfile.mkdtemp())
    ex = LocalExchange(root / "t", root / "r", root / "a")
    ex._ensure_dirs()
    (root / "r" / "result_x.txt").write_bytes("﻿first|OK|ЦБ000000186".encode("utf-8"))
    stats = apply_result_batch(db, ex.download_and_archive_result("result_x.txt"))
    assert stats == {"ok": 1, "error": 0, "unmatched": 0}
    db.refresh(task)
    assert task.status == FtpTaskStatus.done and task.result_detail == "ЦБ000000186"


def test_apply_result_batch_closes_timed_out_task_on_late_result(db):
    """Опоздавший ответ 1С по просроченному (timeout) заданию закрывает его как done."""
    from app.workers.ftp_channel import apply_result_batch
    account = make_account(db, Platform.wb)
    task = FtpTask(command="CREATE_MOVEMENT", barcode="111", quantity=1, order_id="late",
                   account_id=account.id, status=FtpTaskStatus.timeout,
                   sent_at=now_utc() - timedelta(hours=2))
    db.add(task)
    db.commit()
    stats = apply_result_batch(db, "late|OK|ЦБ000000186")
    assert stats["ok"] == 1
    db.refresh(task)
    assert task.status == FtpTaskStatus.done and task.result_detail == "ЦБ000000186"


def test_scheduler_hourly_jobs_fire_right_after_start(web_db):
    """Часовые задания стартуют сразу после запуска воркера (раньше каждый деплой
    откладывал их на час), и СВЕРКА ИДЁТ ПОСЛЕ ЗАПРОСА ВЫГРУЗКИ с запасом на ответ
    1С: обработка запускается по своему расписанию, и читать результат через десять
    секунд означало читать снимок прошлого цикла."""
    from datetime import timezone
    from app.workers.scheduler import build_scheduler

    sched = build_scheduler()  # не запускаем — только состав заданий и их первый запуск
    now = datetime.now(timezone.utc)

    request = sched.get_job("ftp_send_export_request")
    reconciliation = sched.get_job("reconciliation")
    assert request is not None and reconciliation is not None

    # запрос выгрузки — в первую минуту после старта
    assert (request.next_run_time - now).total_seconds() < 60
    # сверка — позже запроса, но в пределах разумного окна
    gap = (reconciliation.next_run_time - request.next_run_time).total_seconds()
    assert 60 <= gap <= 900, f"сверка должна идти через несколько минут после запроса, а не через {gap} с"


def test_scheduler_ftp_jobs_have_separate_heartbeats():
    """Три расписания зовут один job_ftp_send. Если бы они писали heartbeat под общим
    именем, минутный прогон затирал бы остальные и остановка часового запроса выгрузки
    (то есть остановка сверки) была бы не видна в /health."""
    import inspect
    from app.workers import scheduler

    src = inspect.getsource(scheduler.build_scheduler)
    assert 'heartbeat_name="ftp_send_export_request"' in src
    assert 'heartbeat_name="ftp_send_barcode_request"' in src

    from app.routers.health import EXPECTED_INTERVAL_SECONDS
    assert "ftp_send_export_request" in EXPECTED_INTERVAL_SECONDS
    assert "ftp_send_barcode_request" in EXPECTED_INTERVAL_SECONDS

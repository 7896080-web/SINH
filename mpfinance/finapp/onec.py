"""Обмен с 1С через папки — своими файлами `task_fin_*.txt`.

Канал общий с sync_admin, «Маркировкой» и «Репрайсером»: та же обработка
`ОбменССайтом`, та же папка заданий. Правила:

0. **Выключатель.** Обмен включает человек на «Диагностике» (`enabled`), по
   умолчанию он выключен: программу сначала проверяют без 1С. Выключен — ни
   одного файла в папках обмена, ни одного чтения из них.
1. **Свои команды.** FIN_PING (версия обработки), BARCODE_DICT (справочник
   баркодов), EXPORT_COST_PRICES (себестоимость) — только читают; SALES_REPORT
   (отчёт о продажах → документы) — пишет, но уходит ТОЛЬКО по кнопке на
   странице отчёта (`finance/sales_1c.py`).
2. **Сначала FIN_PING — это проверка версии обработки.** Ответы на задания
   `task_fin_*` кладёт в `results\\finance` только обработка mark-4. Старая
   (mark-3) положила бы ответ на BARCODE_DICT или EXPORT_COST_PRICES в ОБЩУЮ
   папку results — его забрал бы sync_admin как чужой. FIN_PING же старая
   обработка не знает и молча пропускает: ответа нет, задание уходит в «нет
   ответа», и «Диагностика» говорит, что обработку надо обновить. Ответ
   «mark-4» (или новее) доказывает обратное, и только после него уходят
   остальные команды.
3. **Отчёт о продажах едет своим файлом**, отдельно от остальных команд:
   команда новая и на базе ещё не обкатана, и если обработка на ней
   споткнётся, вместе с ней не должны пропасть ни справочник, ни себестоимость.
4. **Файл ответа архивируется после коммита разбора**, сломанный файл не
   останавливает остальные.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from finapp import catalog, config, exchange, settings
from finapp.finance import costs, sales_1c
from finapp.audit import log_action
from finapp.models import OnecTask, OnecTaskStatus
from finapp.timeutils import now_utc

PROBE = "FIN_PING"
COMMANDS = {
    PROBE: "Проверка версии обработки",
    "BARCODE_DICT": "Справочник баркодов",
    "EXPORT_COST_PRICES": "Себестоимость",
}
KEY_PREFIX = {PROBE: "FINPING", "BARCODE_DICT": "FINDICT", "EXPORT_COST_PRICES": "FINCOST"}
MIN_VERSION = 4
OPEN = (OnecTaskStatus.pending.value, OnecTaskStatus.sent.value)
AWAITING = (OnecTaskStatus.sent.value, OnecTaskStatus.timeout.value)


class OnecError(ValueError):
    pass


# --- Выключатель и версия обработки --------------------------------------------

def enabled(db: Session) -> bool:
    return settings.get(db, settings.ONEC_ENABLED) == "1"


DISABLED = ("обмен с 1С выключен — его включают последним этапом на «Диагностике» "
            "(«Включить обмен с 1С»), когда модуль обработки обновлён до mark-4")


def epf_version(db: Session) -> int:
    """Номер версии по ответу на FIN_PING: «mark-4» → 4; ответа не было — 0."""
    m = re.search(r"mark-(\d+)", settings.get(db, settings.ONEC_EPF_VERSION))
    return int(m.group(1)) if m else 0


def epf_ready(db: Session) -> bool:
    return epf_version(db) >= MIN_VERSION


NOT_READY = ("обработка 1С ещё не подтвердила версию mark-4 — на «Диагностике» нажмите "
             "«Проверить обработку 1С» и дождитесь ответа (старая обработка положила бы "
             "ответ в общую папку sync_admin)")


# --- Очередь ---------------------------------------------------------------------

def withdraw_unsent(db: Session) -> int:
    """Обмен выключили: то, что ещё НЕ ушло в файл, снимается с очереди. Иначе
    отчёт навсегда стоял бы в «ждёт отправки» — ни удалить (он «в пути»), ни
    отправить заново, — а полоса фоновых дел писала бы «идёт». Ушедшее в файл
    не трогаем: 1С могла его уже обработать, и поздний ответ мы примем."""
    from finapp.models import OneCUploadStatus, SalesReport
    tasks = db.query(OnecTask).filter(OnecTask.status == OnecTaskStatus.pending.value).all()
    for task in tasks:
        task.status = OnecTaskStatus.failed.value
        task.result_detail = "снято с очереди: обмен с 1С выключен до отправки"
    reports = db.query(SalesReport).filter(SalesReport.onec_status == OneCUploadStatus.pending).all()
    for report in reports:
        report.onec_status = OneCUploadStatus.none
        report.onec_detail = "отправка отменена: обмен с 1С выключен до отправки"
    return len(tasks) + len(reports)

def enqueue(db: Session, command: str, user: str = "system") -> OnecTask:
    if command not in COMMANDS:
        raise OnecError(f"команда {command} не наша — в 1С её не посылаем")
    if not enabled(db):
        raise OnecError(DISABLED)
    if command != PROBE and not epf_ready(db):
        raise OnecError(NOT_READY)
    open_task = db.query(OnecTask).filter(OnecTask.command == command,
                                          OnecTask.status.in_(OPEN)).first()
    if open_task is not None:
        return open_task      # такая же команда уже в пути — вторую не шлём
    task = OnecTask(command=command, key="tmp", line="tmp", requested_by=user)
    db.add(task)
    db.flush()
    task.key = f"{KEY_PREFIX[command]}-{task.id}"
    task.line = f"{command}|{task.key}"
    return task


def _task_filename(moment: datetime) -> str:
    return f"{exchange.TASK_PREFIX}{moment:%Y%m%d%H%M%S%f}.txt"


def _free_name(ex) -> str:
    moment = now_utc()
    name = _task_filename(moment)
    while ex.task_exists(name):
        moment += timedelta(microseconds=1)
        name = _task_filename(moment)
    return name


def publish_pending(db: Session, ex) -> dict:
    """Ждущие команды — одним файлом, отчёты о продажах — своим. Порядок
    «пометить → файл → коммит»: сбой записи файла откатывает пометки."""
    stats = {"files": 0, "tasks": 0, "reports": 0}
    ready = epf_ready(db)

    reports = sales_1c.pending_reports(db) if ready else []
    if reports:
        name = _free_name(ex)
        lines = sales_1c.task_lines(db, reports, name)
        try:
            if lines:
                ex.put_task(name, "\n".join(lines))
                stats["files"] += 1
                stats["reports"] += len({line.split("|")[1] for line in lines})
            db.commit()
        except Exception:
            db.rollback()
            raise

    query = db.query(OnecTask).filter(OnecTask.status == OnecTaskStatus.pending.value)
    if not ready:
        query = query.filter(OnecTask.command == PROBE)
    tasks = query.order_by(OnecTask.id).all()
    if tasks:
        name = _free_name(ex)
        now = now_utc()
        for task in tasks:
            task.status = OnecTaskStatus.sent.value
            task.sent_at = now
            task.filename = name
        try:
            ex.put_task(name, "\n".join(t.line for t in tasks))
            db.commit()
        except Exception:
            db.rollback()
            raise
        stats["files"] += 1
        stats["tasks"] += len(tasks)
    return stats


# --- Разбор ответов -----------------------------------------------------------

def parse_result_line(line: str) -> tuple[str, str, str, str] | None:
    """`ключ|OK|подробности|КОМАНДА`; подробности могут содержать «|»."""
    parts = line.strip().split("|")
    if len(parts) < 4 or not parts[0].strip():
        return None
    return (parts[0].strip(), parts[1].strip().upper(), "|".join(parts[2:-1]).strip(),
            parts[-1].strip())


def apply_result_text(db: Session, text: str, cost_text: str = "", dict_text: str = "",
                      task_file: str | None = None) -> dict:
    """Применяет файл ответа. Не коммитит. `task_file` — файл заданий, на который
    этот ответ (по метке имени)."""
    stats = {"ok": 0, "error": 0, "unmatched": 0}
    for raw in text.splitlines():
        parsed = parse_result_line(raw)
        if parsed is None:
            continue
        key, status, detail, command = parsed
        ok = status == "OK"
        if sales_1c.is_sales_report_result(key, command):
            if sales_1c.apply_result(db, key, status, detail, task_file):
                stats["ok" if ok else "error"] += 1
            else:
                stats["unmatched"] += 1
            continue
        task = db.query(OnecTask).filter(OnecTask.key == key, OnecTask.command == command,
                                         OnecTask.status.in_(AWAITING)).first()
        if task is None:
            stats["unmatched"] += 1
            continue
        task.result_status = status
        task.result_detail = detail[:2000]
        task.answered_at = now_utc()
        task.status = OnecTaskStatus.done.value if ok else OnecTaskStatus.failed.value
        stats["ok" if ok else "error"] += 1
        if not ok:
            continue
        if command == PROBE:
            settings.put(db, settings.ONEC_EPF_VERSION, detail[:32])
            settings.put(db, settings.ONEC_EPF_CHECKED_AT, now_utc().isoformat(timespec="seconds"))
            if not epf_ready(db):
                task.status = OnecTaskStatus.failed.value
                task.result_detail = (f"обработка ответила версией «{detail[:32]}», нужна mark-4 "
                                      f"или новее — обновите её в Конфигураторе")
        elif command == "BARCODE_DICT":
            try:
                st = catalog.load_catalog(db, catalog.parse_barcode_dict(dict_text))
                task.result_detail = (f"справочник: баркодов {st['barcodes']}, товаров "
                                      f"{st['products']} (новых {st['new_products']})")
            except catalog.CatalogError as e:
                task.status = OnecTaskStatus.failed.value
                task.result_detail = f"справочник не принят: {e}"
        elif command == "EXPORT_COST_PRICES":
            parsed_costs, bad = costs.parse_cost_file(cost_text)
            if not parsed_costs:
                task.status = OnecTaskStatus.failed.value
                task.result_detail = "в файле себестоимости нет ни одной строки"
                continue
            st = costs.import_costs(db, parsed_costs)
            task.result_detail = costs.summary(st, bad)
    return stats


def collect_results(db: Session, ex) -> dict:
    total = {"files": 0, "ok": 0, "error": 0, "unmatched": 0, "failed_files": []}
    for name in ex.result_names():
        label = name[len("result_"):-len(".txt")]
        cost_name, dict_name = f"cost_{label}.txt", f"barcodes_{label}.txt"
        try:
            text = ex.read_result(name)
            if text is None:
                continue
            stats = apply_result_text(db, text, ex.read_result(cost_name) or "",
                                      ex.read_result(dict_name) or "", task_file=f"task_{label}.txt")
            if stats["unmatched"]:
                # Несопоставленный ответ — в журнал действий: отметка фона
                # затирается через 30 с, а ответ 1С больше не придёт.
                log_action(db, "1С", "onec_unmatched",
                           f"{name}: строк ответа без задания или на прежнюю отправку — {stats['unmatched']}")
            db.commit()
        except Exception as e:                    # noqa: BLE001 — файл остаётся, разберём снова
            db.rollback()
            total["failed_files"].append(f"{name}: {type(e).__name__}: {e}"[:300])
            continue
        try:
            for archived in (name, cost_name, dict_name):
                ex.archive_result(archived)
        except OSError as e:
            total["failed_files"].append(f"{name}: разобран, но не перенесён в архив: {e}"[:300])
        total["files"] += 1
        for k in ("ok", "error", "unmatched"):
            total[k] += stats[k]
    return total


def mark_timeouts(db: Session) -> int:
    limit = now_utc() - timedelta(minutes=config.ONEC_TIMEOUT_MINUTES)
    stuck = db.query(OnecTask).filter(OnecTask.status == OnecTaskStatus.sent.value,
                                      OnecTask.sent_at < limit).all()
    for task in stuck:
        task.status = OnecTaskStatus.timeout.value
        if task.command == PROBE and not epf_ready(db):
            task.result_detail = ("1С не ответила. Если обработка «ОбменССайтом» старше mark-4 — "
                                  "обновите её (FIN_PING и папка results\\finance ей незнакомы); "
                                  "если новая — проверьте, что она запускается")
        else:
            task.result_detail = "1С не ответила — проверьте, что обработка «ОбменССайтом» запускается"
    reports = sales_1c.detect_timed_out(db)
    if stuck:
        db.commit()
    return len(stuck) + len(reports)


def exchange_once(db: Session, ex=None) -> dict:
    """Один проход обмена: просроченные → отправка ждущих → разбор ответов."""
    if not enabled(db):
        return {"disabled": True, "sent": {}, "files": 0, "stuck": 0, "failed_files": [], "unmatched": 0}
    stuck = mark_timeouts(db)
    ex = ex or exchange.current()
    sent = publish_pending(db, ex)
    got = collect_results(db, ex)
    got.update(sent=sent, stuck=stuck)
    return got

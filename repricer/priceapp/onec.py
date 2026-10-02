"""Обмен с 1С через папки — по образцу «Маркировки».

Канал общий с sync_admin и «Маркировкой»: та же обработка `ОбменССайтом`, та
же папка заданий. Правила:

1. **Только свои команды** (`COMMANDS`): EXPORT_COST_PRICES, BARCODE_DICT, PING.
   Ни одна ничего в 1С не создаёт и не меняет — только читает.
2. **Файлы заданий — `task_price_*.txt`.** Обработка (часть mark-3) называет
   ответ по имени задания и для меток `price_*` кладёт его в `results\\pricing`,
   куда sync_admin не заглядывает.
3. **Первой идёт EXPORT_COST_PRICES — и это проверка версии обработки.**
   Старая обработка (mark-2) про `price_` не знает и положила бы ответ на
   BARCODE_DICT или PING в ОБЩУЮ папку results — его забрал бы sync_admin
   как чужой. А EXPORT_COST_PRICES она не знает вовсе и молча пропускает: ответа
   нет, задание уходит в «нет ответа», и страница говорит, что обработку надо
   обновить. Первый ответ OK на неё доказывает, что обработка новая
   (`settings.EPF_READY_AT`), и только после этого уходят остальные команды.
4. **Файл ответа архивируется после коммита разбора**, сломанный файл не
   останавливает остальные.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from priceapp import config, exchange, mapping, settings
from priceapp.models import OnecCost, OnecTask, OnecTaskStatus
from priceapp.timeutils import now_utc

COMMANDS = ("EXPORT_COST_PRICES", "BARCODE_DICT", "PING")
PROBE = "EXPORT_COST_PRICES"
AWAITING = (OnecTaskStatus.sent.value, OnecTaskStatus.timeout.value)


class OnecError(ValueError):
    pass


def epf_ready(db: Session) -> bool:
    return bool(settings.get(db, settings.EPF_READY_AT))


def _enqueue(db: Session, command: str, prefix: str) -> OnecTask:
    if command not in COMMANDS:
        raise OnecError(f"команда {command} не наша — в 1С её не посылаем")
    open_task = (db.query(OnecTask)
                 .filter(OnecTask.command == command,
                         OnecTask.status.in_((OnecTaskStatus.pending.value, OnecTaskStatus.sent.value)))
                 .first())
    if open_task is not None:
        return open_task      # такая же команда уже в пути — вторую не шлём
    task = OnecTask(command=command, order_id="tmp", line="tmp")
    db.add(task)
    db.flush()
    task.order_id = f"{prefix}-{task.id}"
    task.line = f"{command}|{task.order_id}"
    return task


def enqueue_cost(db: Session) -> OnecTask:
    return _enqueue(db, "EXPORT_COST_PRICES", "cost")


def enqueue_dict(db: Session) -> OnecTask:
    if not epf_ready(db):
        raise OnecError("обработка 1С ещё не подтвердила версию mark-3 — сначала запросите "
                        "себестоимость: её ответ и есть проверка (старая обработка положила бы "
                        "ответ на справочник в общую папку sync_admin)")
    return _enqueue(db, "BARCODE_DICT", "dict")


def enqueue_ping(db: Session) -> OnecTask:
    if not epf_ready(db):
        raise OnecError("PING до подтверждения mark-3 не шлём — ответ старой обработки ушёл бы "
                        "в общую папку sync_admin. Запросите себестоимость")
    return _enqueue(db, "PING", "ping")


_last_timeout_poll = None
TIMEOUT_POLL = timedelta(minutes=10)


def has_work(db: Session) -> bool:
    """Обращаемся к 1С, только если задание ждёт отправки или ответа. Зависшие
    (timeout) ещё сутки проверяем на поздний ответ — но раз в 10 минут, а не на
    каждом такте: иначе одно зависшее задание сутки дёргало бы сервер по SFTP
    каждые 30 секунд."""
    global _last_timeout_poll
    if db.query(OnecTask).filter(OnecTask.status.in_((OnecTaskStatus.pending.value,
                                                       OnecTaskStatus.sent.value))).first() is not None:
        return True
    recent = now_utc() - timedelta(days=1)
    if db.query(OnecTask).filter(OnecTask.status == OnecTaskStatus.timeout.value,
                                 OnecTask.sent_at > recent).first() is None:
        return False
    if _last_timeout_poll is not None and now_utc() - _last_timeout_poll < TIMEOUT_POLL:
        return False
    _last_timeout_poll = now_utc()
    return True


def _task_filename(now: datetime) -> str:
    return f"task_price_{now:%Y%m%d%H%M%S%f}.txt"


def publish_pending(db: Session, ex) -> int:
    """Все ждущие задания — одним файлом. Порядок «файл → коммит»: все наши
    команды только читают и повтор переживают."""
    pending = (db.query(OnecTask).filter(OnecTask.status == OnecTaskStatus.pending.value)
               .order_by(OnecTask.id).all())
    if not epf_ready(db):
        pending = [t for t in pending if t.command == PROBE]
    if not pending:
        return 0
    now = now_utc()
    name = _task_filename(now)
    while ex.task_exists(name):
        now += timedelta(microseconds=1)
        name = _task_filename(now)
    ex.put_task(name, "\n".join(t.line for t in pending))
    for t in pending:
        t.status = OnecTaskStatus.sent.value
        t.sent_at = now
        t.filename = name
    db.commit()
    return len(pending)


# --- Разбор ответов -----------------------------------------------------------

@dataclass
class ResultLine:
    order_id: str
    status: str
    detail: str
    command: str


def parse_result_line(line: str) -> ResultLine | None:
    parts = line.rstrip("\r\n").split("|")
    if len(parts) < 4 or not parts[0]:
        return None
    return ResultLine(parts[0].strip(), parts[1].strip().upper(),
                      "|".join(parts[2:-1]).strip(), parts[-1].strip())


def parse_cost(text: str) -> dict[str, Decimal]:
    """cost_*.txt: `uid_1c|себестоимость` (в долларах; точка или запятая).
    Мусор и неположительная себестоимость пропускаются: нулевая база дала бы
    нулевую цену на площадке."""
    out = {}
    for raw in text.splitlines():
        parts = raw.strip().split("|")
        if len(parts) < 2 or not parts[0].strip():
            continue
        try:
            value = Decimal(parts[1].strip().replace(" ", "").replace(" ", "").replace(",", "."))
        except InvalidOperation:
            continue
        if value.is_finite() and value > 0:
            out[parts[0].strip()] = value.quantize(Decimal("0.01"))
    return out


def load_costs(db: Session, costs: dict[str, Decimal]) -> dict:
    """Обновить себестоимость. SKU, которого нет в файле, сохраняет прежнюю:
    пропуск в выгрузке — не повод обнулять базу расчёта. Не коммитит."""
    if not costs:
        raise OnecError("в файле себестоимости нет ни одной строки")
    existing = {c.item_id: c for c in db.query(OnecCost)}
    now, changed = now_utc(), 0
    for item_id, value in costs.items():
        row = existing.get(item_id)
        if row is None:
            db.add(OnecCost(item_id=item_id[:64], cost_usd=value, loaded_at=now))
            changed += 1
        else:
            if Decimal(str(row.cost_usd)) != value:
                changed += 1
            row.cost_usd = value
            row.loaded_at = now
    settings.put(db, settings.COST_LOADED_AT, now.isoformat(timespec="seconds"))
    settings.put(db, settings.COST_ROWS, str(len(costs)))
    return {"rows": len(costs), "changed": changed}


def apply_result_text(db: Session, text: str, cost_text: str = "", dict_text: str = "") -> dict:
    """Применяет файл ответа. Возвращает счётчики. Не коммитит."""
    stats = {"ok": 0, "error": 0, "unmatched": 0}
    for raw in text.splitlines():
        res = parse_result_line(raw)
        if res is None:
            continue
        task = (db.query(OnecTask)
                .filter(OnecTask.order_id == res.order_id, OnecTask.command == res.command,
                        OnecTask.status.in_(AWAITING))
                .order_by(OnecTask.id).first())
        if task is None:
            stats["unmatched"] += 1
            continue
        ok = res.status == "OK"
        task.status = OnecTaskStatus.done.value if ok else OnecTaskStatus.failed.value
        task.result_status = res.status
        task.result_detail = res.detail
        task.answered_at = now_utc()
        stats["ok" if ok else "error"] += 1
        if not ok:
            continue
        if task.command == PROBE:
            # Любой ответ на неё — от новой обработки (старая молчит), значит
            # можно и остальные команды.
            if not epf_ready(db):
                settings.put(db, settings.EPF_READY_AT, now_utc().isoformat(timespec="seconds"))
            try:
                st = load_costs(db, parse_cost(cost_text))
                task.result_detail = f"себестоимость: строк {st['rows']}, изменилось {st['changed']}"
            except OnecError as e:
                task.status = OnecTaskStatus.failed.value
                task.result_detail = f"себестоимость не принята: {e}"
        elif task.command == "BARCODE_DICT":
            try:
                st = mapping.load_dictionary(db, mapping.parse_barcode_dict(dict_text),
                                             f"1С, задание {task.order_id}")
                task.result_detail = f"справочник: строк {st['rows']}, SKU {st['items']}"
            except mapping.MappingError as e:
                task.status = OnecTaskStatus.failed.value
                task.result_detail = f"справочник не принят: {e}"
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
                                      ex.read_result(dict_name) or "")
            db.commit()
        except Exception as e:
            db.rollback()
            total["failed_files"].append(f"{name}: {type(e).__name__}: {e}"[:300])
            continue
        try:
            ex.archive_result(name)
            ex.archive_result(cost_name)
            ex.archive_result(dict_name)
        except Exception as e:
            total["failed_files"].append(f"{name}: разобран, но не перенесён в архив: "
                                         f"{type(e).__name__}: {e}"[:300])
        total["files"] += 1
        for k in ("ok", "error", "unmatched"):
            total[k] += stats[k]
    return total


def mark_timeouts(db: Session) -> int:
    limit = now_utc() - timedelta(minutes=config.ONEC_TIMEOUT_MINUTES)
    stuck = (db.query(OnecTask)
             .filter(OnecTask.status == OnecTaskStatus.sent.value, OnecTask.sent_at < limit).all())
    for t in stuck:
        t.status = OnecTaskStatus.timeout.value
        if t.command == PROBE and not epf_ready(db):
            t.result_detail = ("1С не ответила. Если обработка «ОбменССайтом» ещё mark-2 — "
                               "обновите её до mark-3 (команда EXPORT_COST_PRICES ей незнакома)")
    if stuck:
        db.commit()
    return len(stuck)


def exchange_once(db: Session) -> dict:
    """Один проход обмена: положить ждущие, разобрать ответы. Сервер трогаем,
    только если есть работа."""
    stuck = mark_timeouts(db)
    if not has_work(db):
        return {"sent": 0, "files": 0, "stuck": stuck, "failed_files": [], "unmatched": 0}
    with exchange.current() as ex:
        sent = publish_pending(db, ex)
        got = collect_results(db, ex)
    got.update(sent=sent, stuck=stuck)
    return got

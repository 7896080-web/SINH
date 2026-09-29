"""Обмен с 1С через папки (ТЗ, разд. 9).

Канал общий с sync_admin: та же обработка `ОбменССайтом`, та же папка заданий.
Три правила, из-за которых этот модуль устроен именно так:

1. **Только свои команды** (`COMMANDS`). Старая обработка на незнакомую
   команду молчит, а на знакомую (`CREATE_MOVEMENT`, `CANCEL_MOVEMENT` …)
   ответила бы в ОБЩУЮ папку результатов, и ответ достался бы sync_admin.
2. **Файлы заданий — `task_mark_*.txt`.** Обработка называет ответ по имени
   задания (`result_` + метка), а для меток `mark_*` кладёт ответы в
   подкаталог `results\\marking`, куда sync_admin не заглядывает.
3. **У перемещения нет поля даты.** Дельта остатка ЦС, по которой sync_admin
   узнаёт об уходе товара, выбирает движения по ДАТЕ ДОКУМЕНТА: документ,
   датированный днём поставки, остался бы вне дельты, и sync_admin продавал бы
   эти штуки на WB/Ozon/Kit до наступления даты. Обработка всегда ставит
   текущую дату; дата поставки едет только в комментарий.
"""
from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from markapp import audit, config, settings
from markapp.models import OnecTask, OnecTaskStatus, Supply, SupplyStatus
from markapp.supplies import movement_problems
from markapp.timeutils import now_utc, ru

COMMANDS = ("PING", "SUPPLY_CHECK", "SUPPLY_MOVEMENT")
FINAL = (OnecTaskStatus.done.value, OnecTaskStatus.failed.value)
# Ответ можно применить и к заданию, объявленному зависшим: опоздавший ответ
# честнее, чем вечное «timeout».
AWAITING = (OnecTaskStatus.sent.value, OnecTaskStatus.timeout.value)

EPF_VERSION = "onec_epf_version"
EPF_PING_AT = "onec_ping_ok_at"


class OnecError(ValueError):
    pass


def _clean(value: str) -> str:
    """Поля строки задания не могут нести разделители формата."""
    text = str(value).strip()
    for bad in ("|", ";", ":", "\r", "\n", "\t"):
        if bad in text:
            raise OnecError(f"недопустимый символ {bad!r} в значении {text!r}")
    return text


def positions_of(supply: Supply) -> "OrderedDict[str, int]":
    """Баркод -> количество. Одинаковые баркоды складываются в одну позицию."""
    out: OrderedDict[str, int] = OrderedDict()
    for row in supply.rows:
        if not row.ean:
            raise OnecError(f"строка {row.position}: нет штрихкода")
        bc = _clean(row.ean)
        out[bc] = out.get(bc, 0) + int(row.qty)
    return out


def supply_line(command: str, order_id: str, supply: Supply) -> str:
    """SUPPLY_CHECK / SUPPLY_MOVEMENT:
    команда|order_id|склад откуда|склад куда|баркод:кол;…|комментарий-дополнение

    Поля даты нет намеренно (см. модуль). В комментарий документа обработка
    пишет `mark order_id=<order_id> lamoda <дополнение>` — НЕ на `sync`: иначе
    sync_admin отбросил бы дельту по этому движению как свою.
    """
    if command not in ("SUPPLY_CHECK", "SUPPLY_MOVEMENT"):
        raise OnecError(command)
    pos = ";".join(f"{bc}:{q}" for bc, q in positions_of(supply).items())
    note = _clean(f"поставка {supply.number} от {ru(supply.supply_date)}".strip())
    return "|".join([command, _clean(order_id), _clean(config.ONEC_WAREHOUSE_FROM),
                     _clean(config.ONEC_WAREHOUSE_TO), pos, note])


def movement_order_id(supply: Supply) -> str:
    """Ключ идемпотентности перемещения: повтор задания вернёт тот же документ."""
    return f"lamoda-{supply.number}"


def enqueue(db: Session, command: str, order_id: str, line: str,
            supply: Supply | None = None) -> OnecTask:
    if command not in COMMANDS:
        raise OnecError(f"команда {command} не наша — в 1С её не посылаем")
    task = OnecTask(command=command, order_id=order_id, line=line,
                    supply_id=supply.id if supply else None,
                    is_test=bool(supply.is_test) if supply else False)
    db.add(task)
    db.flush()
    return task


def enqueue_check(db: Session, supply: Supply) -> OnecTask:
    # У каждой проверки свой order_id: проверок по поставке бывает несколько,
    # и ответ должен лечь ровно на свою.
    task = enqueue(db, "SUPPLY_CHECK", "tmp", "tmp", supply)
    task.order_id = f"lamoda-{supply.number}-c{task.id}"
    task.line = supply_line("SUPPLY_CHECK", task.order_id, supply)
    return task


def enqueue_movement(db: Session, supply: Supply) -> OnecTask:
    oid = movement_order_id(supply)
    return enqueue(db, "SUPPLY_MOVEMENT", oid, supply_line("SUPPLY_MOVEMENT", oid, supply), supply)


def enqueue_ping(db: Session) -> OnecTask:
    task = enqueue(db, "PING", "tmp", "tmp")
    task.order_id = f"ping-{task.id}"
    task.line = f"PING|{task.order_id}"
    return task


def epf_ready(db: Session) -> bool:
    """Обработка 1С обновлена под маркировку — пришёл ответ на PING.

    Пока нет, задания поставок не посылаются: старая обработка их молча
    пропустила бы, и поставка висела бы «в пути» без объяснения.
    """
    return bool(settings.get(db, EPF_VERSION))


# --- Публикация -----------------------------------------------------------------

def _task_filename(now: datetime) -> str:
    return f"task_mark_{now:%Y%m%d%H%M%S%f}.txt"


def publish_pending(db: Session, tasks_dir: Path | None = None) -> int:
    """Кладёт все ждущие задания одним файлом. Возвращает число строк.

    Порядок «файл → коммит», а не наоборот (у sync_admin наоборот — из-за
    неидемпотентной отмены). Все наши команды переживают повтор: проверка
    ничего не создаёт, перемещение идемпотентно по order_id, PING безвреден.
    Упади коммит после записи файла — задание уйдёт ещё раз, и это безопасно;
    при обратном порядке оно считалось бы отправленным, не будучи им.
    """
    tasks_dir = Path(tasks_dir or config.ONEC_TASKS_DIR)
    pending = (db.query(OnecTask)
               .filter(OnecTask.status == OnecTaskStatus.pending.value,
                       OnecTask.is_test.is_(False))
               .order_by(OnecTask.id).all())
    pending = [t for t in pending if t.command == "PING" or epf_ready(db)]
    if not pending:
        return 0
    tasks_dir.mkdir(parents=True, exist_ok=True)
    now = now_utc()
    name = _task_filename(now)
    while (tasks_dir / name).exists():
        now += timedelta(microseconds=1)
        name = _task_filename(now)
    tmp = tasks_dir / (name + ".part")
    # 1С не должна увидеть недописанный файл: пишем под временным именем.
    tmp.write_text("\n".join(t.line for t in pending), encoding="utf-8")
    os.replace(tmp, tasks_dir / name)
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
    # Подробность может содержать «|» только если обработка её не очистила;
    # команда всегда последняя.
    return ResultLine(parts[0].strip(), parts[1].strip().upper(),
                      "|".join(parts[2:-1]).strip(), parts[-1].strip())


CHECK_FIELDS = ("barcode", "item_id", "article", "name", "size", "color", "stock", "need", "status")


def parse_check_file(text: str) -> dict[str, dict]:
    """supplycheck_<метка>.txt: строка на баркод,
    баркод|ID товара|артикул|наименование|размер|цвет|остаток ЦС|нужно|статус."""
    out = {}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("|")
        if len(parts) != len(CHECK_FIELDS):
            continue
        rec = dict(zip(CHECK_FIELDS, (p.strip() for p in parts)))
        out[rec["barcode"]] = rec
    return out


def _to_int(s: str) -> int | None:
    try:
        return int(float(s.replace(",", ".")))
    except (ValueError, AttributeError):
        return None


def _apply_check(db: Session, task: OnecTask, res: ResultLine, check: dict[str, dict]) -> None:
    supply = db.get(Supply, task.supply_id) if task.supply_id else None
    if supply is None:
        return
    for row in supply.rows:
        rec = check.get(row.ean)
        if rec is None:
            row.onec_status = "not_found" if check else ""
            continue
        row.onec_item_id = rec["item_id"]
        row.onec_article = rec["article"]
        row.onec_name = rec["name"]
        row.onec_size = rec["size"]
        row.onec_color = rec["color"]
        row.onec_stock = _to_int(rec["stock"])
        row.onec_status = rec["status"].lower() or "ok"
    # Статус поставки двигает только ответ на ПОСЛЕДНЮЮ проверку: правка строк
    # между проверками возвращает поставку в черновик, и старый ответ не должен
    # объявить проверенным изменённый состав.
    latest = (db.query(OnecTask).filter(OnecTask.supply_id == supply.id,
                                        OnecTask.command == "SUPPLY_CHECK")
              .order_by(OnecTask.id.desc()).first())
    if latest is not None and latest.id != task.id:
        return
    if supply.status in (SupplyStatus.draft.value, SupplyStatus.checked.value):
        # 1С ответила OK, но два артикула легли на один SKU 1С — сопоставление не
        # один к одному, перемещать нельзя (`supplies.movement_problems`).
        ok = res.status == "OK" and not movement_problems(supply)
        supply.status = SupplyStatus.checked.value if ok else SupplyStatus.draft.value


def _apply_movement(db: Session, task: OnecTask, res: ResultLine) -> None:
    supply = db.get(Supply, task.supply_id) if task.supply_id else None
    if supply is None:
        return
    if res.status == "OK":
        supply.status = SupplyStatus.moved.value
        supply.onec_document = res.detail[:50]
        supply.moved_at = now_utc()
        audit.log(db, "1С", "supply_moved", f"поставка {supply.number}",
                  f"документ 1С {res.detail}")
    elif supply.status == SupplyStatus.checked.value:
        # 1С отказала (чаще всего нехватка: штуку продали между проверкой и
        # перемещением). Остаток изменился — нужна новая проверка.
        supply.status = SupplyStatus.draft.value


def apply_result_text(db: Session, text: str, check_text: str = "") -> dict:
    """Применяет файл ответа. Возвращает счётчики. Не коммитит."""
    stats = {"ok": 0, "error": 0, "unmatched": 0}
    check = parse_check_file(check_text) if check_text else {}
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
        if task.command == "PING" and ok:
            settings.put(db, EPF_VERSION, res.detail or "?")
            settings.put(db, EPF_PING_AT, now_utc().isoformat(timespec="seconds"))
        elif task.command == "SUPPLY_CHECK":
            _apply_check(db, task, res, check)
        elif task.command == "SUPPLY_MOVEMENT":
            _apply_movement(db, task, res)
    return stats


def collect_results(db: Session, results_dir: Path | None = None,
                    archive_dir: Path | None = None) -> dict:
    """Разбирает `result_mark_*.txt` из своего подкаталога.

    Файл архивируется ПОСЛЕ коммита разбора: архивирование первым делом
    означало бы, что сбой записи в базу уносит ответ безвозвратно — повторно
    1С его не пришлёт. Разбор идемпотентен (закрытое задание второй раз не
    найдётся), поэтому перечитать тот же файл безопасно.
    """
    results_dir = Path(results_dir or config.ONEC_RESULTS_DIR)
    archive_dir = Path(archive_dir or config.ONEC_ARCHIVE_DIR)
    total = {"files": 0, "ok": 0, "error": 0, "unmatched": 0}
    if not results_dir.exists():
        return total
    for path in sorted(results_dir.glob("result_mark_*.txt")):
        label = path.name[len("result_"):-len(".txt")]
        check_path = results_dir / f"supplycheck_{label}.txt"
        text = path.read_text(encoding="utf-8-sig")
        check_text = check_path.read_text(encoding="utf-8-sig") if check_path.exists() else ""
        try:
            stats = apply_result_text(db, text, check_text)
            db.commit()
        except Exception:
            db.rollback()
            raise
        archive_dir.mkdir(parents=True, exist_ok=True)
        os.replace(path, archive_dir / path.name)
        if check_path.exists():
            os.replace(check_path, archive_dir / check_path.name)
        total["files"] += 1
        for k in ("ok", "error", "unmatched"):
            total[k] += stats[k]
    return total


def mark_timeouts(db: Session) -> int:
    limit = now_utc() - timedelta(minutes=config.ONEC_TIMEOUT_MINUTES)
    stuck = (db.query(OnecTask)
             .filter(OnecTask.status == OnecTaskStatus.sent.value, OnecTask.sent_at < limit)
             .all())
    for t in stuck:
        t.status = OnecTaskStatus.timeout.value
    if stuck:
        db.commit()
    return len(stuck)

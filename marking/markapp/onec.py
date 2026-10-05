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

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from markapp import audit, config, exchange, settings
from markapp.models import OnecTask, OnecTaskStatus, Supply, SupplyStatus
from markapp.supplies import SupplyError, blocking_problems, ensure_editable, movement_problems
from markapp.timeutils import now_utc, ru

COMMANDS = ("PING", "SUPPLY_CHECK", "SUPPLY_MOVEMENT", "BARCODE_DICT")
# Какая версия части маркировки в обработке 1С умеет команду. Старая на
# незнакомую молчит — задание висело бы без ответа, поэтому не шлём.
MIN_VERSION = {"BARCODE_DICT": 2}
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
    команда|order_id|код склада откуда|код склада куда|баркод:кол;…|комментарий-дополнение

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


def epf_version(db: Session) -> int:
    """Номер версии части маркировки по ответу на PING: «mark-2» -> 2, нет — 0."""
    raw = settings.get(db, EPF_VERSION)
    try:
        return int(raw.rsplit("-", 1)[-1])
    except (ValueError, AttributeError):
        return 0


def enqueue_barcode_dict(db: Session) -> OnecTask:
    """Справочник баркодов 1С для страницы сопоставления (`mapping.py`)."""
    need = MIN_VERSION["BARCODE_DICT"]
    if epf_version(db) < need:
        raise OnecError(f"обработка 1С не умеет выгружать справочник для маркировки — нужна версия "
                        f"mark-{need} (сейчас: {settings.get(db, EPF_VERSION) or 'нет ответа на PING'}). "
                        "Пока можно загрузить файл справочника вручную")
    task = enqueue(db, "BARCODE_DICT", "tmp", "tmp")
    task.order_id = f"dict-{task.id}"
    task.line = f"BARCODE_DICT|{task.order_id}"
    return task


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


def has_work(db: Session) -> bool:
    """Есть ли зачем обращаться к 1С: задание ждёт отправки или ответа.

    Обмен по факту работы с поставкой, а не постоянный опрос: пока заданий нет,
    сервер не трогаем вовсе. Зависшие ждём ещё неделю — опоздавший ответ честнее
    вечного «timeout» (AWAITING), но опрашивать их вечно незачем.
    """
    # Неделя, а не сутки: компьютер выключают на выходные, и ответ на пятничное
    # задание должен забраться в понедельник.
    recent = now_utc() - timedelta(days=7)
    return db.query(OnecTask).filter(
        OnecTask.is_test.is_(False),
        (OnecTask.status.in_((OnecTaskStatus.pending.value, OnecTaskStatus.sent.value)))
        | ((OnecTask.status == OnecTaskStatus.timeout.value) & (OnecTask.sent_at > recent)),
    ).first() is not None


def publish_pending(db: Session, ex=None) -> int:
    """Кладёт все ждущие задания одним файлом. Возвращает число строк.

    Порядок «файл → коммит», а не наоборот (у sync_admin наоборот — из-за
    неидемпотентной отмены). Все наши команды переживают повтор: проверка
    ничего не создаёт, перемещение идемпотентно по order_id, PING безвреден.
    Упади коммит после записи файла — задание уйдёт ещё раз, и это безопасно;
    при обратном порядке оно считалось бы отправленным, не будучи им.
    """
    pending = (db.query(OnecTask)
               .filter(OnecTask.status == OnecTaskStatus.pending.value,
                       OnecTask.is_test.is_(False))
               .order_by(OnecTask.id).all())
    pending = [t for t in pending if t.command == "PING" or epf_ready(db)]
    # Не больше ОДНОЙ проверки поставки на файл. Подробности проверки обработка
    # пишет одним `supplycheck_<метка>.txt` на весь файл, строкой на штрихкод и
    # без order_id. Две проверки в одном файле дали бы общий список, и строки
    # поставки A получили бы остаток и статус по «нужно» поставки B (общий
    # штрихкод — обычное дело). Остальные проверки уйдут следующими циклами.
    first_check = next((t for t in pending if t.command == "SUPPLY_CHECK"), None)
    pending = [t for t in pending if t.command != "SUPPLY_CHECK" or t is first_check]
    if not pending:
        return 0
    if ex is None:
        with exchange.current() as ex:
            return _publish(db, ex, pending)
    return _publish(db, ex, pending)


def _publish(db: Session, ex, pending: list[OnecTask]) -> int:
    now = now_utc()
    name = _task_filename(now)
    while ex.task_exists(name):
        now += timedelta(microseconds=1)
        name = _task_filename(now)
    # 1С не должна увидеть недописанный файл: транспорт пишет `.part` и
    # переименовывает.
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
    # Ответ применяется, только если это ПОСЛЕДНЯЯ проверка И состав с её
    # отправки не менялся (строка задания та же). Иначе старый ответ расставил
    # бы строкам данные чужого состава, а «OK» объявил бы проверенным состав,
    # который 1С не видела (правка после отправки без новой проверки).
    latest = (db.query(OnecTask).filter(OnecTask.supply_id == supply.id,
                                        OnecTask.command == "SUPPLY_CHECK")
              .order_by(OnecTask.id.desc()).first())
    if latest is not None and latest.id != task.id:
        return
    if task.line != supply_line("SUPPLY_CHECK", task.order_id, supply):
        if supply.status == SupplyStatus.checked.value:
            supply.status = SupplyStatus.draft.value
        return
    for row in supply.rows:
        rec = check.get(row.ean)
        if rec is None:
            row.onec_status = "not_found" if check else ""
            row.onec_item_id = row.onec_article = row.onec_name = ""
            row.onec_size = row.onec_color = ""
            row.onec_stock = None
            continue
        row.onec_item_id = rec["item_id"]
        row.onec_article = rec["article"]
        row.onec_name = rec["name"]
        row.onec_size = rec["size"]
        row.onec_color = rec["color"]
        row.onec_stock = _to_int(rec["stock"])
        row.onec_status = rec["status"].lower() or "ok"
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
        if supply.status not in (SupplyStatus.draft.value, SupplyStatus.checked.value):
            # Опоздавший ответ на повтор: поставка уже перемещена (и, может быть,
            # с УПД). Назад в «перемещено» её не возвращаем.
            return
        supply.status = SupplyStatus.moved.value
        supply.onec_document = res.detail[:50]
        supply.moved_at = now_utc()
        audit.log(db, "1С", "supply_moved", f"поставка {supply.number}",
                  f"документ 1С {res.detail}")
    elif supply.status == SupplyStatus.checked.value:
        # 1С отказала (чаще всего нехватка: штуку продали между проверкой и
        # перемещением). Остаток изменился — нужна новая проверка.
        supply.status = SupplyStatus.draft.value


def apply_result_text(db: Session, text: str, check_text: str = "", dict_text: str = "",
                      label: str = "") -> dict:
    """Применяет файл ответа. Возвращает счётчики. Не коммитит."""
    stats = {"ok": 0, "error": 0, "unmatched": 0}
    check = parse_check_file(check_text) if check_text else {}
    for raw in text.splitlines():
        res = parse_result_line(raw)
        if res is None:
            continue
        found = (db.query(OnecTask)
                 .filter(OnecTask.order_id == res.order_id, OnecTask.command == res.command,
                         OnecTask.status.in_(AWAITING))
                 .order_by(OnecTask.id).all())
        # У повторов перемещения order_id один: ответ — заданию из того же файла.
        task = next((t for t in found if label and t.filename == f"task_{label}.txt"),
                    found[0] if found else None)
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
        elif task.command == "BARCODE_DICT" and ok:
            from markapp import mapping
            try:
                st = mapping.load_dictionary(db, mapping.parse_barcode_dict(dict_text),
                                             f"1С, задание {task.order_id}")
                task.result_detail = f"строк {st['rows']}, SKU {st['items']}"
            except mapping.MappingError as e:
                # 1С ответила OK, а файла нет или он пуст — прежний снимок не трогаем.
                task.status = OnecTaskStatus.failed.value
                task.result_detail = f"справочник не принят: {e}"
    return stats


def collect_results(db: Session, ex=None) -> dict:
    """Разбирает `result_mark_*.txt` из своего подкаталога.

    Файл архивируется ПОСЛЕ коммита разбора: архивирование первым делом
    означало бы, что сбой записи в базу уносит ответ безвозвратно — повторно
    1С его не пришлёт. Разбор идемпотентен (закрытое задание второй раз не
    найдётся), поэтому перечитать тот же файл безопасно.
    """
    if ex is None:
        with exchange.current() as ex:
            return _collect(db, ex)
    return _collect(db, ex)


def _collect(db: Session, ex) -> dict:
    total = {"files": 0, "ok": 0, "error": 0, "unmatched": 0, "failed_files": []}
    for name in ex.result_names():
        label = name[len("result_"):-len(".txt")]
        check_name = f"supplycheck_{label}.txt"
        dict_name = f"barcodes_{label}.txt"
        # Файл, который не разбирается, НЕ останавливает остальные: раньше
        # исключение уходило наверх, и каждый цикл спотыкался об один и тот же
        # первый по имени файл — ответы за ним не применялись никогда, а зависшие
        # задания не отмечались. Сломанный файл остаётся на месте (повторно 1С
        # его не пришлёт) и называется в отметке задания.
        try:
            text = ex.read_result(name)
            if text is None:
                continue
            check_text = ex.read_result(check_name) or ""
            dict_text = ex.read_result(dict_name) or ""
            if not check_text and any((r := parse_result_line(x)) and r.command == "SUPPLY_CHECK"
                                      and r.status == "OK" for x in text.splitlines()):
                # На OK проверки 1С всегда пишет supplycheck_ ДО result_. Его нет —
                # сбой чтения; применить «OK» без построчных данных значило бы
                # объявить поставку проверенной вслепую. Ждём следующего прохода.
                total["failed_files"].append(f"{name}: нет {check_name} — ответ отложен")
                continue
            stats = apply_result_text(db, text, check_text, dict_text, label=label)
            db.commit()
        except Exception as e:
            db.rollback()
            total["failed_files"].append(f"{name}: {type(e).__name__}: {e}"[:300])
            continue
        # Разбор уже закоммичен. Не удался перенос в архив (права на сервере,
        # занятый файл) — ответ не теряется и не применяется второй раз (задание
        # закрыто), но файл останется лежать и будет виден: называем его, а не
        # роняем обмен, иначе следующие ответы за ним не разобрались бы вовсе.
        try:
            ex.archive_result(name)
            ex.archive_result(check_name)
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
             .filter(OnecTask.status == OnecTaskStatus.sent.value, OnecTask.sent_at < limit)
             .all())
    for t in stuck:
        t.status = OnecTaskStatus.timeout.value
    if stuck:
        db.commit()
    return len(stuck)


def fit_to_stock(db: Session, supply: Supply) -> list[str]:
    """«Уменьшить до остатка 1С»: строки «не хватает» — до остатка ЦС из ответа
    ПОСЛЕДНЕЙ проверки, остаток 0 и меньше — строка убирается. Если после этого
    всё сходится, поставка — «проверено в 1С» без нового запроса: данные те же,
    что прислала 1С, а составом стало меньше. Перемещение в 1С всё равно сверяет
    остаток заново и при нехватке документа не создаёт (атомарно).

    Только если состав с той проверки не менялся: иначе остатки относятся к
    другому составу, и нужна новая проверка. Возвращает список изменений.
    """
    ensure_editable(supply)
    last = (db.query(OnecTask).filter(OnecTask.supply_id == supply.id, OnecTask.command == "SUPPLY_CHECK")
            .order_by(OnecTask.id.desc()).first())
    if last is None or last.status not in (OnecTaskStatus.done.value, OnecTaskStatus.failed.value):
        raise SupplyError("нет ответа 1С на проверку — сначала «Проверить в 1С»")
    if last.line != supply_line("SUPPLY_CHECK", last.order_id, supply):
        raise SupplyError("состав менялся после последней проверки — остатки 1С к нему не относятся, "
                          "проверьте в 1С заново")
    if any(r.onec_status in ("not_found", "ambiguous", "") for r in supply.rows):
        raise SupplyError("есть строки без ответа 1С или с ненайденным штрихкодом — их остаток не известен")
    short = [r for r in supply.rows if r.onec_status == "short"]
    eans = [r.ean for r in short]
    if len(eans) != len(set(eans)):
        raise SupplyError("один штрихкод в нескольких строках «не хватает» — поправьте количества вручную")
    changes = []
    for r in short:
        stock = max(0, r.onec_stock or 0)
        changes.append(f"{r.supplier_sku}: {r.qty} → {stock}" + (" (строка убрана)" if stock == 0 else ""))
        if stock == 0:
            supply.rows.remove(r)
        else:
            r.qty = stock
            r.onec_status = "ok"
    if not changes:
        raise SupplyError("строк «не хватает» нет")
    db.flush()
    if not supply.rows:
        raise SupplyError("после уменьшения в поставке не осталось строк")
    ok = not blocking_problems(supply) and not movement_problems(supply)
    supply.status = SupplyStatus.checked.value if ok else SupplyStatus.draft.value
    return changes

import logging
import os
import re
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from app.timeutils import now_utc

from sqlalchemy.orm import Session

from app.models import (FtpTask, FtpTaskStatus, Platform, StockDateRow, StockDateSnapshot,
                        StockDateStatus)

logger = logging.getLogger("sync_worker")

TASK_TIMEOUT_MINUTES = 15

# Выгрузка остатков на заданное число: команда в task_*.txt и ответ ondate_*.txt.
STOCK_ON_DATE_COMMAND = "EXPORT_STOCK_ON_DATE"
# Имя файла ответа: ondate_<дата среза ГГГГММДД>_<метка обработки ГГГГММДДЧЧММСС>.txt
STOCK_ON_DATE_FILE_RE = re.compile(r"^ondate_(\d{8})_\d+\.txt$", re.IGNORECASE)
# За раз просим не больше нескольких дат: каждая — отдельный запрос по регистру
# остатков на боевой базе, и десяток сразу заметно её нагрузил бы.
MAX_DATE_REQUESTS_PER_BATCH = 3
# Обработка 1С запускается по своему расписанию, поэтому ждём ответа заметно
# дольше, чем по обычному заданию (TASK_TIMEOUT_MINUTES).
STOCK_ON_DATE_TIMEOUT_MINUTES = 180
# Сколько выгрузок храним: это справка, а не данные системы, и полный снимок
# склада за каждое число быстро раздул бы базу.
STOCK_ON_DATE_KEEP = 10


class LocalExchange:
    """Обмен с 1С через ЛОКАЛЬНУЮ ПАПКУ (без FTP): приложение и .epf-обработка
    работают с одними каталогами tasks/results/archive на этой же машине.
    Публикация атомарная: пишем '<имя>.part', затем os.replace → '<имя>'
    (rename в пределах одного каталога атомарен), поэтому 1С не прочитает
    полуфайл. Файлы результатов/остатков 1С публикует так же, а недописанные
    '.part' сюда не попадают (glob по '*.txt' их не берёт)."""

    def __init__(self, dir_tasks: str, dir_results: str, dir_archive: str):
        self.dir_tasks = Path(dir_tasks)
        self.dir_results = Path(dir_results)
        self.dir_archive = Path(dir_archive)

    def _ensure_dirs(self):
        for d in (self.dir_tasks, self.dir_results, self.dir_archive):
            d.mkdir(parents=True, exist_ok=True)

    def task_file_exists(self, filename: str) -> bool:
        return (self.dir_tasks / filename).exists() or (self.dir_archive / filename).exists()

    def upload_task_file(self, filename: str, content: str):
        """Публикация задания. Перезапись существующего файла — ОШИБКА, а не
        обычный ход: задания внутри него уже помечены отправленными, и затирание
        означало бы, что в 1С они не попадут никогда и молча."""
        self._ensure_dirs()
        final = self.dir_tasks / filename
        if self.task_file_exists(filename):
            raise FileExistsError(f"задание {filename} уже существует — перезапись затёрла бы отправленные строки")
        tmp = self.dir_tasks / (filename + ".part")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, final)

    def list_result_files(self) -> list[str]:
        if not self.dir_results.exists():
            return []
        return sorted(p.name for p in self.dir_results.glob("result_*.txt"))

    def list_stock_files(self) -> list[str]:
        if not self.dir_results.exists():
            return []
        return sorted(p.name for p in self.dir_results.glob("stock_*.txt"))

    def list_stock_on_date_files(self) -> list[str]:
        """Выгрузка остатков НА ДАТУ. Префикс намеренно другой (`ondate_`, а не
        `stock_`): файл со складом за прошлое число не должен попасть ни в
        сверку, ни в остаток товара — иначе на площадки уехали бы цифры той
        давности, которую запросил оператор для справки."""
        if not self.dir_results.exists():
            return []
        return sorted(p.name for p in self.dir_results.glob("ondate_*.txt"))

    def file_mtime_utc(self, filename: str) -> datetime | None:
        """Время последней записи файла результата в naive UTC — тем же масштабом,
        что и now_utc(). Нужно, чтобы отличить свежую выгрузку 1С от той, что
        лежит с прошлого цикла."""
        p = self.dir_results / filename
        if not p.exists():
            return None
        # Явная конвертация эпохи в naive UTC. utcfromtimestamp устарел, а
        # fromtimestamp без пояса вернул бы ЛОКАЛЬНОЕ время: на сервере UTC+3 это
        # сдвинуло бы сравнение со временем запроса на три часа.
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)

    def list_barcode_files(self) -> list[str]:
        if not self.dir_results.exists():
            return []
        return sorted(p.name for p in self.dir_results.glob("barcodes_*.txt"))

    def download_and_archive_result(self, filename: str) -> str:
        self._ensure_dirs()
        src = self.dir_results / filename
        # utf-8-sig: обработка 1С пишет файлы через ЗаписьТекста(…, КодировкаТекста.UTF8),
        # то есть С BOM. При чтении как чистого utf-8 три байта BOM прилипали к первому
        # полю первой строки — первая строка result_*.txt не сопоставлялась с заданием
        # (задание вечно "sent"), а первый uid в stock_*.txt/barcodes_*.txt искажался.
        content = src.read_text(encoding="utf-8-sig")
        os.replace(src, self.dir_archive / filename)
        return content


# Полей в строке справочника баркодов: uid|артикул|наименование|баркод|размер|цвет.
BARCODE_DICT_FIELDS = 6
# Полей в строке выгрузки остатков: uid|артикул|наименование|кол-во|баркоды|размер|цвет.
STOCK_EXPORT_FIELDS = 7


def _split_from_the_right(line: str, fields: int) -> list[str] | None:
    """Разбор строки, устойчивый к разделителю ВНУТРИ наименования.

    Обработка 1С чистит `|` в размере, цвете и баркодах, но не в артикуле и
    наименовании: товар «Джинсы|36» сдвигал все поля вправо, и количество
    читалось из соседней колонки, а баркод — из следующей. Хвост строки
    (количество, баркоды, размер, цвет) имеет фиксированную длину, поэтому
    режем его СПРАВА, а всё лишнее отдаём наименованию — оно и так текст.

    Возвращает ровно `fields` кусков или None, если полей меньше минимума."""
    parts = line.split("|")
    if len(parts) < fields:
        return None
    if len(parts) == fields:
        return parts
    tail = fields - 3                       # сколько полей справа от наименования
    return [parts[0], parts[1], "|".join(parts[2:-tail])] + parts[-tail:]


def parse_barcode_dict(content: str) -> list[dict]:
    """Разбор barcodes_*.txt (полный справочник из .epf):
    'uid|артикул|наименование|баркод|размер|цвет' — одна строка на баркод."""
    rows = []
    for line in content.splitlines():
        line = line.rstrip("\n")
        p = _split_from_the_right(line, BARCODE_DICT_FIELDS)
        if p is None:
            # Старый формат без размера и цвета — режем как раньше, слева.
            p = line.split("|")
            if len(p) < 4:
                continue
        rows.append({
            "uid_1c": p[0].strip(), "article": p[1].strip(), "name": p[2].strip(),
            "barcode": p[3].strip(),
            "size": p[4].strip() if len(p) > 4 else "",
            "color": p[5].strip() if len(p) > 5 else "",
        })
    return rows


def fetch_barcode_dict_files(exchange: "LocalExchange") -> list[dict]:
    """Забирает barcodes_*.txt из results, объединяет строки, архивирует."""
    rows = []
    for filename in exchange.list_barcode_files():
        content = exchange.download_and_archive_result(filename)
        rows += parse_barcode_dict(content)
    return rows


def _unique_task_filename(exchange: "LocalExchange | None" = None) -> str:
    """Имя файла задания. Секунды не хватало: минутная отправка и суточный запрос
    справочника попадали в одну и ту же секунду, второй файл затирал первый через
    replace — а строки затёртого уже были помечены отправленными и в 1С не
    попадали никогда. Теперь в имени микросекунды, и если имя всё же занято
    (архив тоже проверяем), берём следующее свободное."""
    base = now_utc()
    for bump in range(1000):
        stamp = base + timedelta(microseconds=bump)
        name = f"task_{stamp:%Y%m%d%H%M%S%f}.txt"
        if exchange is None or not exchange.task_file_exists(name):
            return name
    raise RuntimeError("не удалось подобрать свободное имя файла задания")


def build_task_batch(db: Session, max_lines: int = 500, request_stock_export: bool = False,
                     request_barcode_export: bool = False,
                     exchange: "LocalExchange | None" = None) -> tuple[str, str] | None:
    """Собирает файл-задание из накопившихся FtpTask со статусом pending.
    Возвращает (имя_файла, содержимое) или None, если отправлять нечего.
    request_stock_export=True добавляет строку EXPORT_STOCK_ON_HAND — сигнал
    .epf сделать полную выгрузку остатков ЦС (раздел 8), используется
    реже, чем обычные задания (например, раз в час перед сверкой).

    Накопившиеся заявки на остатки НА ДАТУ (`StockDateSnapshot` в состоянии
    pending) уходят той же машиной, строкой `EXPORT_STOCK_ON_DATE|ГГГГММДД`.

    `exchange` нужен, чтобы проверить, что имя файла свободно, ДО того как строки
    помечены отправленными: иначе занятое имя означало бы потерю целого батча."""

    tasks = db.query(FtpTask).filter(
        FtpTask.status == FtpTaskStatus.pending,
        FtpTask.is_test.is_(False),  # тестовые задания со страницы тестирования — никогда не уходят в реальный файл для 1С
    ).limit(max_lines).all()
    # Запросы остатков на дату (страница «Остатки на дату»). Флага is_test у них
    # нет и не нужно: команда только ЧИТАЕТ регистр остатков и ничего в 1С не
    # создаёт и не меняет — побочного эффекта, от которого защищает is_test, у
    # неё не существует.
    date_requests = db.query(StockDateSnapshot).filter(
        StockDateSnapshot.status == StockDateStatus.pending,
    ).order_by(StockDateSnapshot.id.asc()).limit(MAX_DATE_REQUESTS_PER_BATCH).all()

    if not tasks and not date_requests and not request_stock_export and not request_barcode_export:
        return None

    filename = _unique_task_filename(exchange)
    lines = []
    if request_stock_export:
        lines.append("EXPORT_STOCK_ON_HAND")
    if request_barcode_export:
        lines.append("EXPORT_BARCODES")

    sent_dates = set()
    for snapshot in date_requests:
        # Две заявки на одну дату дают ОДНУ строку: 1С назовёт файл по дате, и
        # второй ответ просто затёр бы первый.
        if snapshot.snapshot_date not in sent_dates:
            lines.append(f"{STOCK_ON_DATE_COMMAND}|{snapshot.snapshot_date:%Y%m%d}")
            sent_dates.add(snapshot.snapshot_date)
        snapshot.status = StockDateStatus.sent
        snapshot.sent_at = now_utc()
        snapshot.batch_filename = filename

    for t in tasks:
        # Строка в файле для 1С по-прежнему содержит площадку (не кабинет) —
        # .epf на старой базе про кабинеты ничего не знает, ей нужен только
        # физический товар и склад. Имя кабинета оседает в комментарии
        # документа, если понадобится аудит "какой именно ИП продал".
        platform_value = t.account.platform.value
        # Дата документа (старт задним числом), ГГГГММДД; пусто = текущая дата в 1С.
        mdate = t.movement_date.strftime("%Y%m%d") if t.movement_date else ""
        if t.command == "CREATE_MOVEMENT":
            lines.append("|".join([
                "CREATE_MOVEMENT", t.barcode, t.warehouse_from or "ЦС Склад", t.warehouse_to or "",
                str(t.quantity or 0), t.order_id, platform_value, mdate,
            ]))
        elif t.command == "CONFIRM_MOVEMENT":
            # Подтверждение: «<Площадка>.Ожидает» → «Склад <Площадка>».
            lines.append("|".join([
                "CONFIRM_MOVEMENT", t.barcode, t.warehouse_from or "", t.warehouse_to or "",
                str(t.quantity or 0), t.order_id, platform_value, mdate,
            ]))
        elif t.command == "CANCEL_MOVEMENT":
            lines.append("|".join(["CANCEL_MOVEMENT", t.order_id, platform_value]))

        t.status = FtpTaskStatus.sent
        t.batch_filename = filename
        t.sent_at = now_utc()

    db.commit()
    return filename, "\n".join(lines)


def parse_stock_export_file(content: str) -> dict[str, int]:
    """Разбирает stock_*.txt от .epf: 'uid|артикул|наименование|количество|баркод1,баркод2'.
    Возвращает {баркод: количество} — плоский вид, удобный для reconciliation.py."""
    result = {}
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = _split_from_the_right(line, STOCK_EXPORT_FIELDS) or line.split("|")
        if len(parts) < 5:
            continue
        digits = parts[3].strip().lstrip("-")
        if not digits.isdigit():
            continue
        quantity = int(parts[3])
        barcodes = [b for b in parts[4].split(",") if b]
        for barcode in barcodes:
            result[barcode] = quantity
    return result


def fetch_stock_export_files(exchange: "LocalExchange") -> dict[str, int]:
    """Забирает все накопившиеся stock_*.txt из каталога results, объединяет,
    архивирует. Обычно один файл за раз, но на всякий случай — несколько."""
    combined = {}
    for filename in exchange.list_stock_files():
        content = exchange.download_and_archive_result(filename)
        combined.update(parse_stock_export_file(content))
    return combined


def parse_stock_export_rows(content: str) -> list[dict]:
    """Полный разбор stock_*.txt: 'uid|артикул|наименование|кол-во|баркод1,баркод2|размер|цвет'.
    Возвращает список словарей — для заведения/обновления товаров (size/color
    появились позже, поэтому поля 6-7 опциональны)."""
    rows = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = _split_from_the_right(line, STOCK_EXPORT_FIELDS)
        if parts is None:
            # Старый формат (без размера и цвета) — как раньше, слева.
            parts = line.split("|")
            if len(parts) < 5:
                continue
        digits = parts[3].strip().lstrip("-")
        if not digits.isdigit():
            continue
        rows.append({
            "uid_1c": parts[0].strip(),
            "article": parts[1].strip(),
            "name": parts[2].strip(),
            "quantity": int(parts[3]),
            "barcodes": [b.strip() for b in parts[4].split(",") if b.strip()],
            "size": parts[5].strip() if len(parts) > 5 else "",
            "color": parts[6].strip() if len(parts) > 6 else "",
        })
    return rows


def fetch_stock_export_snapshot(exchange: "LocalExchange",
                                not_older_than: datetime | None = None) -> tuple[list[dict], datetime | None]:
    """То же, что `fetch_stock_export_rows`, но вместе со ВРЕМЕНЕМ снимка.

    Время нужно сверке: задание, закрытое уже после выгрузки, в самой выгрузке
    ещё не проведено, и без этой поправки сверка считает его приходом на склад
    (см. `reconciliation._in_flight_adjustment`). Может быть None, если файловая
    система не отдала время модификации, — тогда сверка считает «в пути» только
    по заданиям, открытым прямо сейчас (прежнее поведение).

    `not_older_than` — момент, когда мы в последний раз ПОПРОСИЛИ у 1С выгрузку.
    Файл старше этого момента — ответ на прошлый запрос: он отражает склад часовой
    давности, и применять его нельзя (продажи за этот час вернутся «обратно», и
    завышенный остаток уедет на площадки). Такие файлы архивируются без разбора.

    Из оставшихся берётся ТОЛЬКО САМЫЙ СВЕЖИЙ: выгрузка 1С — это снимок склада
    целиком, и склеивать два снимка нельзя (товар, распроданный в ноль, исчезает
    из нового файла и «воскрес» бы из старого)."""
    files = exchange.list_stock_files()          # имена вида stock_ГГГГММДДЧЧММСС.txt, отсортированы
    if not files:
        return [], None

    fresh = []
    for filename in files:
        mtime = exchange.file_mtime_utc(filename)
        if not_older_than is not None and mtime is not None and mtime < not_older_than:
            exchange.download_and_archive_result(filename)
            logger.warning("stock: %s старше последнего запроса выгрузки (%s < %s) — пропущен",
                           filename, mtime, not_older_than)
            continue
        fresh.append(filename)

    if not fresh:
        return [], None

    newest = fresh[-1]
    for filename in fresh[:-1]:
        exchange.download_and_archive_result(filename)
        logger.info("stock: %s заменён более свежим снимком %s", filename, newest)

    taken_at = exchange.file_mtime_utc(newest)   # до архивации: после неё файла уже нет
    return parse_stock_export_rows(exchange.download_and_archive_result(newest)), taken_at


def fetch_stock_export_rows(exchange: "LocalExchange", not_older_than: datetime | None = None) -> list[dict]:
    """Только строки выгрузки, без времени снимка — для вызовов, которым время
    не нужно (разовые скрипты, тесты разбора)."""
    rows, _ = fetch_stock_export_snapshot(exchange, not_older_than=not_older_than)
    return rows


def parse_stock_on_date_filename(filename: str) -> "date | None":
    """Дата среза из имени ondate_ГГГГММДД_ГГГГММДДЧЧММСС.txt."""
    match = STOCK_ON_DATE_FILE_RE.match(filename)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def apply_stock_on_date_files(db: Session, exchange: "LocalExchange") -> dict:
    """Забирает ondate_*.txt и раскладывает по заявкам «остатки на дату».

    Сознательно НЕ трогает ни `Product.stock_on_hand`, ни очередь рассылки, ни
    сверку: это снимок склада за прошлое число, и применение его как текущего
    остатка означало бы рассылку на площадки цифр той давности, которую оператор
    запросил всего лишь для справки. Всё, что делает функция, — складывает
    строки файла в отдельную таблицу и закрывает заявку."""
    stats = {"files": 0, "rows": 0, "unmatched": 0}

    for filename in exchange.list_stock_on_date_files():
        snapshot_date = parse_stock_on_date_filename(filename)
        if snapshot_date is None:
            exchange.download_and_archive_result(filename)
            stats["unmatched"] += 1
            logger.warning("stock_on_date: имя %s не разобрано — файл убран в архив", filename)
            continue

        snapshot = db.query(StockDateSnapshot).filter(
            StockDateSnapshot.snapshot_date == snapshot_date,
            StockDateSnapshot.status != StockDateStatus.done,
        ).order_by(StockDateSnapshot.id.asc()).first()

        content = exchange.download_and_archive_result(filename)
        if snapshot is None:
            stats["unmatched"] += 1
            logger.warning("stock_on_date: выгрузка за %s не сопоставлена ни с одной заявкой "
                           "(файл %s лежит в архиве)", snapshot_date, filename)
            continue

        rows = parse_stock_export_rows(content)
        # Повторный ответ на ту же заявку (например, обработку запустили дважды)
        # не должен задвоить строки.
        db.query(StockDateRow).filter(
            StockDateRow.snapshot_id == snapshot.id).delete(synchronize_session=False)
        for row in rows:
            db.add(StockDateRow(
                snapshot_id=snapshot.id, uid_1c=row["uid_1c"], article=row["article"],
                name=row["name"], size=row["size"], color=row["color"],
                barcodes=",".join(row["barcodes"]), quantity=row["quantity"],
            ))

        snapshot.status = StockDateStatus.done
        snapshot.received_at = now_utc()
        snapshot.result_filename = filename
        snapshot.rows_count = len(rows)
        # Пустой ответ — не ошибка канала, а нормальный результат для даты, на
        # которую остатков не было; но оператор должен видеть разницу между
        # «пусто» и «ещё не пришло».
        snapshot.note = "" if rows else "1С вернула пустую выгрузку"
        stats["files"] += 1
        stats["rows"] += len(rows)

    db.commit()
    return stats


def detect_timed_out_stock_date_requests(db: Session) -> list:
    """Заявка на остатки на дату без ответа дольше окна ожидания. Отдельное окно
    (часы, а не минуты): обработка 1С запускается по своему расписанию, и первые
    минуты молчания — норма."""
    cutoff = now_utc() - timedelta(minutes=STOCK_ON_DATE_TIMEOUT_MINUTES)
    stale = db.query(StockDateSnapshot).filter(
        StockDateSnapshot.status == StockDateStatus.sent,
        StockDateSnapshot.sent_at < cutoff,
    ).all()
    for snapshot in stale:
        snapshot.status = StockDateStatus.timeout
        snapshot.note = "1С не ответила — проверьте, что обработка «ОбменССайтом» запускается"
    if stale:
        db.commit()
    return stale


def prune_stock_date_snapshots(db: Session, keep: int = STOCK_ON_DATE_KEEP) -> int:
    """Оставляет последние `keep` заявок, остальные удаляет вместе со строками.
    Каждая выгрузка — полный снимок склада (тысячи строк), а нужна она обычно
    один раз; без уборки база росла бы от справок."""
    ids = [row.id for row in db.query(StockDateSnapshot.id)
           .order_by(StockDateSnapshot.id.desc()).offset(keep).all()]
    if not ids:
        return 0
    db.query(StockDateRow).filter(
        StockDateRow.snapshot_id.in_(ids)).delete(synchronize_session=False)
    db.query(StockDateSnapshot).filter(
        StockDateSnapshot.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return len(ids)


KNOWN_COMMANDS = ("CREATE_MOVEMENT", "CONFIRM_MOVEMENT", "CANCEL_MOVEMENT")


def parse_result_line(line: str) -> tuple[str, str, str, str] | None:
    """Разбор строки result_*.txt → (order_id, статус, подробность, команда).

    Базовый формат `order_id|СТАТУС|подробность` — его пишет обработка 1С сейчас.
    Необязательное ПОСЛЕДНЕЕ поле с именем команды (`…|CREATE_MOVEMENT`) делает
    сопоставление точным: по одному номеру заказа у нас бывает и создание, и
    отмена. Поле распознаётся только если это в точности одна из известных
    команд, поэтому обычная подробность с символом `|` за команду не сойдёт.
    Команда пустая — сопоставляем по номеру заказа, как раньше."""
    parts = line.split("|")
    if len(parts) < 3:
        return None
    command = ""
    if len(parts) >= 4 and parts[-1].strip() in KNOWN_COMMANDS:
        command = parts[-1].strip()
        parts = parts[:-1]
    return parts[0].strip(), parts[1].strip(), "|".join(parts[2:]), command


def apply_result_batch(db: Session, content: str) -> dict:
    """Разбирает файл result_*.txt и закрывает соответствующие FtpTask.

    Две вещи, из-за которых этот разбор раньше врал:

    1. Ответ `ERROR` закрывал задание как успешное (`done`). Документа в 1С при
       этом не существует, а система считала заказ проведённым. Теперь такое
       задание получает статус `failed` — оно видно в диагностике и продолжает
       считаться «в пути» при сверке, то есть остаток не задирается обратно.
    2. Задание искалось по одному номеру заказа, среди всех кабинетов и команд,
       и бралось САМОЕ СВЕЖЕЕ. Ответ мог закрыть чужое задание. Теперь берём
       самое СТАРОЕ незакрытое (1С отвечает в том же порядке, в каком получила
       строки), в пределах одного файла одно задание закрывается только один раз,
       а если 1С прислала имя команды — совпадение по команде обязательно."""
    stats = {"ok": 0, "error": 0, "unmatched": 0}
    closed_ids: set[int] = set()

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = parse_result_line(line)
        if parsed is None:
            continue
        order_id, result_status, detail, command = parsed

        # Принимаем результат и для просроченного (timeout) задания: опоздавший ответ 1С —
        # это ровно тот случай, ради которого timeout и существует; иначе задание, по
        # которому 1С документ создала, навсегда остаётся «без результата».
        query = db.query(FtpTask).filter(
            FtpTask.order_id == order_id,
            FtpTask.status.in_([FtpTaskStatus.sent, FtpTaskStatus.timeout]),
        )
        if command:
            query = query.filter(FtpTask.command == command)
        if closed_ids:
            query = query.filter(FtpTask.id.notin_(closed_ids))
        task = query.order_by(FtpTask.sent_at.asc(), FtpTask.id.asc()).first()

        if task is None:
            stats["unmatched"] += 1
            logger.warning("ftp_receive: ответ по заказу %s (%s) не сопоставлен ни с одним заданием",
                           order_id, result_status)
            continue

        ok = result_status == "OK"
        task.status = FtpTaskStatus.done if ok else FtpTaskStatus.failed
        task.result_status = result_status
        task.result_detail = detail[:255]
        task.completed_at = now_utc()
        closed_ids.add(task.id)

        if not ok:
            logger.error("1С отказала по заданию %s %s: %s", task.command, order_id, detail[:255])
        stats["ok" if ok else "error"] += 1

    db.commit()
    return stats


def detect_timed_out_tasks(db: Session) -> list[FtpTask]:
    """Раздел 13: задание, висящее без результата дольше окна ожидания —
    отдельная проблема (сбой канала/1С), не часть обычного цикла."""
    cutoff = now_utc() - timedelta(minutes=TASK_TIMEOUT_MINUTES)
    stale = db.query(FtpTask).filter(
        FtpTask.status == FtpTaskStatus.sent, FtpTask.sent_at < cutoff,
    ).all()
    for t in stale:
        t.status = FtpTaskStatus.timeout
    if stale:
        db.commit()
    return stale

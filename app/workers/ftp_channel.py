import logging
import os
import re
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from app.timeutils import now_utc

from sqlalchemy.orm import Session

from app.models import (FtpTask, FtpTaskStatus, Platform, StockDateRow, StockDateSnapshot,
                        StockDateStatus, StockDeltaDocument)

logger = logging.getLogger("sync_worker")

TASK_TIMEOUT_MINUTES = 15

# --- Перепроведение зависших перемещений -----------------------------------
# 1С иногда забирает файл задания и не отвечает по нему НИ ОДНОЙ строкой — ни OK,
# ни ERROR. 19.09 так пропали два файла из пятидесяти трёх (12 строк): остаток по
# 11 товарам навсегда занижен, потому что незакрытое задание вечно считается «в
# пути». Разобрать его было нечем: `timeout` означает «неизвестно, создан
# документ или нет», а повторная отправка вслепую завела бы ВТОРОЙ документ
# перемещения на тот же заказ.
#
# Механизм опирается на идемпотентность 1С по номеру заказа: обработка перед
# созданием ищет документ с этим номером и, если он есть, возвращает OK, ничего
# не создавая. Тогда повтор безопасен и одновременно служит проверкой — ответ OK
# значит «документ есть», независимо от того, был он раньше или создан сейчас.
#
# ВЫКЛЮЧЕНО ПО УМОЛЧАНИЮ. Пока идемпотентности в 1С нет, включать нельзя: каждый
# повтор задвоит документ. Включается переменной окружения на боевом сервере,
# осознанно и после подтверждения со стороны 1С.
MOVEMENT_REPOST_ENV = "MOVEMENT_REPOST_ENABLED"
# Сколько ждём после отметки `timeout`, прежде чем повторять. Ответ 1С штатно
# приходит за 4–7 минут, `timeout` ставится через 15 — к этому сроку опоздавший
# ответ уже закрыл бы задание сам (`apply_result_batch` принимает и поздние).
REPOST_AFTER_MINUTES = 30
# Больше пяти раз не долбим: если и после них тишина, дело не в случайности —
# задание уходит в ручной разбор на «Диагностику», а не молотит вечно.
MAX_REPOSTS = 5
# Повторы уходят ОТДЕЛЬНЫМ маленьким файлом, а не подмешиваются к свежим
# заданиям. Причина та же, из-за которой они и зависли: 1С роняет файл ЦЕЛИКОМ,
# и строка, на которой она спотыкается, утащила бы за собой ни в чём не повинные
# свежие перемещения.
REPOST_BATCH_LINES = 3

# --- Оперативное изменение остатка ЦС ---------------------------------------
# 1С сама кладёт `delta_ГГГГММДДЧЧММСС.txt`, когда меняется остаток ЦС, — чтобы не
# ждать часовой выгрузки (полный снимок это 152 тыс. товаров, чаще его не просят).
# Строка: `баркод|новый остаток ЦС|источник|идентификатор документа`.
#
# Количество — НОВЫЙ АБСОЛЮТНЫЙ остаток, а не приращение. Приращения копят ошибку:
# один потерянный или задвоенный файл — и расхождение остаётся навсегда, а
# абсолютное число самолечится следующим же сообщением.
#
# Два разных предохранителя, и нужны оба:
#  1. ИСТОЧНИК. Документы, созданные по нашим же заданиям, 1С помечает `sync`.
#     Их надо отбрасывать: наше перемещение уже уменьшило остаток в момент приёма
#     заказа, и применить его ещё раз значит списать единицу дважды. Это эхо
#     собственных действий, а не новость со склада.
#  2. ИДЕНТИФИКАТОР ДОКУМЕНТА. Один и тот же документ приезжает повторно при
#     переотправке файла, повторном проведении, ручном перезапуске обработки.
#     Применённые идентификаторы храним (`StockDeltaDocument`) и второй раз не
#     применяем — иначе порядок файлов начинает решать, и старое сообщение может
#     затереть новое.
#
# И главное: частичный файл применяется ТОЛЬКО как частичный. Полный снимок
# обнуляет всё, чего в нём нет (иначе распроданный товар транслировался бы
# вечно), и дельта, применённая как снимок, обнулила бы весь каталог с первого
# же сообщения.
STOCK_DELTA_FIELDS = 4
SYNC_SOURCE = "sync"

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
        """Полные снимки склада: `stock_ГГГГММДДЧЧММСС.txt`.

        Цифра в шаблоне не для красоты. Снимок применяется как ПОЛНЫЙ — товар,
        которого в нём нет, обнуляется, — поэтому попасть сюда не должен никакой
        другой файл, чьё имя начинается на `stock_`. Достаточно завести рядом
        `stock_delta_*` или `stock_backup_*`, и он был бы разобран как снимок
        склада со всеми последствиями.
        """
        if not self.dir_results.exists():
            return []
        return sorted(p.name for p in self.dir_results.glob("stock_[0-9]*.txt"))

    def list_stock_delta_files(self) -> list[str]:
        """Оперативные изменения остатка ЦС: `delta_ГГГГММДДЧЧММСС.txt`.

        Префикс намеренно НЕ начинается на `stock_`: это частичный файл, и
        применять его как снимок нельзя ни при каких обстоятельствах.
        """
        if not self.dir_results.exists():
            return []
        return sorted(p.name for p in self.dir_results.glob("delta_*.txt"))

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

    # ИНВАРИАНТ: повтор зависшего перемещения никогда не едет в одном файле ни с
    # чем другим. 1С роняет файл ЦЕЛИКОМ — именно так и зависли 12 строк, — значит
    # строка, на которой она спотыкается, утащила бы за собой и свежие
    # перемещения, и запрос выгрузки. Поэтому либо файл целиком из повторов, либо
    # повторов в нём нет вовсе.
    #
    # Файл с запросом выгрузки (часовой/суточный) повторы не забирает никогда:
    # иначе запрос остатков оказался бы проглочен, сверка встала бы на час, и мы
    # починили бы одно, сломав другое. Повторы уедут следующим минутным файлом.
    reposts = [t for t in tasks if (t.repost_count or 0) > 0]
    asking_export = request_stock_export or request_barcode_export
    if reposts and not asking_export:
        tasks = reposts[:REPOST_BATCH_LINES]
        date_requests_allowed = False
    else:
        tasks = [t for t in tasks if (t.repost_count or 0) == 0]
        date_requests_allowed = True
    # Запросы остатков на дату (страница «Остатки на дату»). Флага is_test у них
    # нет и не нужно: команда только ЧИТАЕТ регистр остатков и ничего в 1С не
    # создаёт и не меняет — побочного эффекта, от которого защищает is_test, у
    # неё не существует.
    date_requests = db.query(StockDateSnapshot).filter(
        StockDateSnapshot.status == StockDateStatus.pending,
    ).order_by(StockDateSnapshot.id.asc()).limit(MAX_DATE_REQUESTS_PER_BATCH).all() \
        if date_requests_allowed else []

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


def parse_stock_delta_file(content: str) -> list[dict]:
    """Разбор `delta_*.txt`: `баркод|новый остаток|источник|идентификатор документа`.

    Формат намеренно свой и минимальный, а не расширение строки `stock_*.txt`: там
    поля разбираются справа из-за наименований с «|» внутри, и подмешивать туда
    ещё два поля значило бы делать разбор хрупким ради экономии.
    """
    rows = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < STOCK_DELTA_FIELDS:
            continue
        barcode = parts[0].strip()
        digits = parts[1].strip().lstrip("-")
        if not barcode or not digits.isdigit():
            continue
        rows.append({
            "barcode": barcode,
            "quantity": int(parts[1]),
            "source": parts[2].strip().lower(),
            "document_id": parts[3].strip(),
        })
    return rows


def collect_stock_delta(db: Session, exchange: "LocalExchange") -> tuple[dict[str, int], dict]:
    """Забирает и архивирует `delta_*.txt`, отсеивая всё, что применять нельзя.

    Возвращает `({баркод: новый остаток}, статистика)`. САМ НИЧЕГО НЕ ПРИМЕНЯЕТ:
    остаток двигает `run_reconciliation` — та же функция, что и на часовой
    выгрузке, чтобы формула «остаток = 1С минус в пути» жила в одном месте.
    Вызывающий обязан звать её с `missing_means_zero=False`.
    """
    stats = {"files": 0, "lines": 0, "ours_skipped": 0, "already_applied": 0,
             "no_document_id": 0, "applied": 0}
    mapping: dict[str, int] = {}
    fresh_documents: dict[str, dict] = {}

    for filename in exchange.list_stock_delta_files():
        content = exchange.download_and_archive_result(filename)
        stats["files"] += 1
        for row in parse_stock_delta_file(content):
            stats["lines"] += 1

            if row["source"] == SYNC_SOURCE:
                # Наш же документ: остаток по нему уже списан в момент приёма
                # заказа. Применить ещё раз — списать дважды.
                stats["ours_skipped"] += 1
                continue

            doc_id = row["document_id"]
            if not doc_id:
                # Без идентификатора повтор не отличить от новости, и порядок
                # файлов начал бы решать. Пропускаем: часовая выгрузка всё равно
                # принесёт этот остаток, самое позднее через час.
                stats["no_document_id"] += 1
                continue

            if doc_id not in fresh_documents and db.query(StockDeltaDocument).filter(
                    StockDeltaDocument.document_id == doc_id).first() is not None:
                stats["already_applied"] += 1
                continue

            mapping[row["barcode"]] = row["quantity"]
            entry = fresh_documents.setdefault(doc_id, {"source": row["source"], "lines": 0})
            entry["lines"] += 1
            stats["applied"] += 1

    for doc_id, entry in fresh_documents.items():
        db.add(StockDeltaDocument(document_id=doc_id, source=entry["source"],
                                  lines=entry["lines"]))
    if fresh_documents:
        db.commit()

    if stats["files"]:
        logger.info("stock_delta: %s", stats)
    if stats["no_document_id"]:
        logger.warning("stock_delta: %d строк без идентификатора документа — "
                       "пропущены, защита от задвоения без него невозможна",
                       stats["no_document_id"])
    return mapping, stats


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

        # Товары, которым оператор задал эту дату для расчёта порога, ждали
        # именно этого файла. Подставляем им остаток на дату и пересчитываем
        # порог здесь же: между «задал дату» и «1С ответила» проходит до десяти
        # минут, и если не доделать сейчас, расчёт застрянет до тех пор, пока
        # оператор не тронет строку руками.
        from app.offset_base import fill_waiting_products   # локально: цикл импортов
        filled = fill_waiting_products(db, snapshot)
        if filled["filled"]:
            stats["offset_base_filled"] = stats.get("offset_base_filled", 0) + filled["filled"]
            logger.info("stock_on_date: порог посчитан для %d товаров (изменился у %d, "
                        "в очередь рассылки %d)", filled["filled"],
                        filled["offsets_changed"], filled["queued"])

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


def repost_enabled() -> bool:
    """Включено ли перепроведение зависших перемещений.

    Читаем окружение В МОМЕНТ ВЫЗОВА, а не при импорте: так состояние видно в
    тестах и меняется рестартом службы, без пересборки образа.
    """
    return os.environ.get(MOVEMENT_REPOST_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def repost_stuck_movements(db: Session) -> dict:
    """Возвращает зависшие перемещения в очередь на отправку.

    Берём только `timeout`: `failed` — это внятный отказ 1С, там документа
    заведомо нет и повтор ничего не проверяет, такое разбирает человек. `sent`
    ещё в работе. Отменам (`CANCEL_MOVEMENT`) повтор тоже не делаем: идемпотентность
    обещана по созданию, про отмену такой договорённости нет, а угадывать в
    сторону 1С нельзя.

    Само задание не пересоздаём, а возвращаем в `pending` — тогда у него
    сохраняется вся история (когда создано, сколько раз повторяли), и «в пути»
    оно как считалось, так и считается: остаток не дёргается туда-сюда, пока
    ответа нет.
    """
    stats = {"reposted": 0, "exhausted": 0, "skipped_disabled": 0}

    stuck = db.query(FtpTask).filter(
        FtpTask.status == FtpTaskStatus.timeout,
        FtpTask.command == "CREATE_MOVEMENT",
        FtpTask.is_test.is_(False),
    ).order_by(FtpTask.id).all()
    if not stuck:
        return stats

    cutoff = now_utc() - timedelta(minutes=REPOST_AFTER_MINUTES)
    ready = [t for t in stuck
             if t.sent_at is not None and t.sent_at < cutoff]

    stats["exhausted"] = len([t for t in ready if (t.repost_count or 0) >= MAX_REPOSTS])
    ready = [t for t in ready if (t.repost_count or 0) < MAX_REPOSTS]

    if not repost_enabled():
        # Молча ничего не делаем, но СЧИТАЕМ: иначе выключенный механизм выглядел
        # бы как отсутствие проблемы, а зависшие задания никуда не делись.
        stats["skipped_disabled"] = len(ready)
        return stats

    for t in ready[:REPOST_BATCH_LINES]:
        t.status = FtpTaskStatus.pending
        t.repost_count = (t.repost_count or 0) + 1
        t.batch_filename = None
        t.sent_at = None
        stats["reposted"] += 1

    if stats["reposted"]:
        db.commit()
        logger.info("repost_stuck_movements: вернули в очередь %d зависших перемещений",
                    stats["reposted"])
    return stats


def tasks_needing_review(db: Session) -> list[FtpTask]:
    """Задания, которые сами уже не разберутся — их закрывает человек.

    Сюда попадают три случая:
      * `failed` — 1С ответила ERROR, документа заведомо нет;
      * `timeout`, исчерпавший `MAX_REPOSTS` повторов;
      * `timeout` при ВЫКЛЮЧЕННОМ перепроведении — иначе, пока в 1С нет
        идемпотентности, зависшие задания не попадали бы в разбор вовсе и
        остаток молча занижался бы дальше. Именно это состояние на бою сейчас.

    Свежий `timeout` не берём: опоздавший ответ 1С закрывает такое задание сам
    (`apply_result_batch` принимает и поздние), и звать человека рано.
    """
    cutoff = now_utc() - timedelta(minutes=REPOST_AFTER_MINUTES)
    rows = db.query(FtpTask).filter(
        FtpTask.status.in_([FtpTaskStatus.timeout, FtpTaskStatus.failed]),
        FtpTask.is_test.is_(False),
    ).order_by(FtpTask.id).all()

    out = []
    for t in rows:
        if t.status is FtpTaskStatus.failed:
            out.append(t)
            continue
        if t.sent_at is not None and t.sent_at >= cutoff:
            continue                                  # ещё может закрыться сам
        if (t.repost_count or 0) >= MAX_REPOSTS or not repost_enabled():
            out.append(t)
    return out


def resolve_stuck_task(db: Session, task: FtpTask, document_exists: bool,
                       actor: str) -> FtpTask:
    """Закрывает зависшее задание решением человека, посмотревшего в 1С.

    `document_exists=True` — документ в 1С есть: задание закрываем как
    проведённое, «в пути» снимается, остаток сходится с 1С сам собой.

    `document_exists=False` — документа нет и не будет: задание получает
    `no_document`. Это ТОЖЕ снимает «в пути», и остаток вырастет на количество
    задания — потому что 1С эту единицу у себя так и не списала. Решение опасное
    и сознательно оставлено человеку: если товар на самом деле отгружен, возврат
    единицы в остаток означает, что площадки начнут продавать проданное.
    """
    task.status = FtpTaskStatus.done if document_exists else FtpTaskStatus.no_document
    task.result_status = "OK" if document_exists else "NO_DOCUMENT"
    task.result_detail = ("разобрано вручную (%s): документ в 1С %s"
                          % (actor, "найден" if document_exists else "не найден"))[:255]
    task.completed_at = now_utc()
    db.commit()
    logger.info("resolve_stuck_task: #%s %s -> %s (%s)",
                task.id, task.order_id, task.status.value, actor)
    return task


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

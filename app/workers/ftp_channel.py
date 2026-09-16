import logging
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from app.timeutils import now_utc

from sqlalchemy.orm import Session

from app.models import FtpTask, FtpTaskStatus, Platform

logger = logging.getLogger("sync_worker")

TASK_TIMEOUT_MINUTES = 15


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

    def upload_task_file(self, filename: str, content: str):
        self._ensure_dirs()
        final = self.dir_tasks / filename
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


def parse_barcode_dict(content: str) -> list[dict]:
    """Разбор barcodes_*.txt (полный справочник из .epf):
    'uid|артикул|наименование|баркод|размер|цвет' — одна строка на баркод."""
    rows = []
    for line in content.splitlines():
        p = line.rstrip("\n").split("|")
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


def build_task_batch(db: Session, max_lines: int = 500, request_stock_export: bool = False,
                     request_barcode_export: bool = False) -> tuple[str, str] | None:
    """Собирает файл-задание из накопившихся FtpTask со статусом pending.
    Возвращает (имя_файла, содержимое) или None, если отправлять нечего.
    request_stock_export=True добавляет строку EXPORT_STOCK_ON_HAND — сигнал
    .epf сделать полную выгрузку остатков ЦС (раздел 8), используется
    реже, чем обычные задания (например, раз в час перед сверкой)."""

    tasks = db.query(FtpTask).filter(
        FtpTask.status == FtpTaskStatus.pending,
        FtpTask.is_test.is_(False),  # тестовые задания со страницы тестирования — никогда не уходят в реальный файл для 1С
    ).limit(max_lines).all()
    if not tasks and not request_stock_export and not request_barcode_export:
        return None

    filename = f"task_{now_utc():%Y%m%d%H%M%S}.txt"
    lines = []
    if request_stock_export:
        lines.append("EXPORT_STOCK_ON_HAND")
    if request_barcode_export:
        lines.append("EXPORT_BARCODES")

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
        parts = line.split("|")
        if len(parts) < 5:
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


def fetch_stock_export_rows(exchange: "LocalExchange", not_older_than: datetime | None = None) -> list[dict]:
    """Полные строки выгрузки остатков (uid/артикул/имя/кол-во/баркоды/размер/цвет).

    `not_older_than` — момент, когда мы в последний раз ПОПРОСИЛИ у 1С выгрузку.
    Файл старше этого момента — ответ на прошлый запрос: он отражает склад часовой
    давности, и применять его нельзя (продажи за этот час вернутся «обратно», и
    завышенный остаток уедет на площадки). Такие файлы архивируются без разбора.

    Из оставшихся берётся ТОЛЬКО САМЫЙ СВЕЖИЙ: выгрузка 1С — это снимок склада
    целиком, и склеивать два снимка нельзя (товар, распроданный в ноль, исчезает
    из нового файла и «воскрес» бы из старого)."""
    files = exchange.list_stock_files()          # имена вида stock_ГГГГММДДЧЧММСС.txt, отсортированы
    if not files:
        return []

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
        return []

    newest = fresh[-1]
    for filename in fresh[:-1]:
        exchange.download_and_archive_result(filename)
        logger.info("stock: %s заменён более свежим снимком %s", filename, newest)

    return parse_stock_export_rows(exchange.download_and_archive_result(newest))


def apply_result_batch(db: Session, content: str) -> dict:
    """Разбирает файл result_*.txt и закрывает соответствующие FtpTask."""
    stats = {"ok": 0, "error": 0, "unmatched": 0}

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        order_id, result_status, detail = parts[0], parts[1], "|".join(parts[2:])

        # Принимаем результат и для просроченного (timeout) задания: опоздавший ответ 1С —
        # это ровно тот случай, ради которого timeout и существует; иначе задание, по
        # которому 1С документ создала, навсегда остаётся «без результата».
        task = db.query(FtpTask).filter(
            FtpTask.order_id == order_id,
            FtpTask.status.in_([FtpTaskStatus.sent, FtpTaskStatus.timeout]),
        ).order_by(FtpTask.sent_at.desc()).first()

        if task is None:
            stats["unmatched"] += 1
            continue

        task.status = FtpTaskStatus.done
        task.result_status = result_status
        task.result_detail = detail[:255]
        task.completed_at = now_utc()

        stats["ok" if result_status == "OK" else "error"] += 1

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

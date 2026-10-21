"""Разбор файлов FTP-моста 1С.

Остатки — формат обработки `ОбменССайтом` (тот же, что читает Sync Admin):
    uid_1c|артикул|наименование|количество|баркод1,баркод2|размер|цвет
Разбор перенесён из `app/workers/ftp_channel.py` Sync Admin без изменения логики
(хвост строки режется справа — `|` в наименовании не сдвигает поля; BOM UTF-8 снимается).

Продажи — ПРЕДЛОЖЕННЫЙ формат (у 1С такой выгрузки пока нет, см. README «Вопросы по 1С»):
    sales_ГГГГММДД_ГГГГММДД_ГГГГММДДЧЧММСС.txt — период «с … по …» и метка выгрузки,
    строка: дата(ГГГГММДД)|баркод|количество|сумма_руб|канал
количество < 0 — возврат; канал — код точки продаж (retail_1, retail_2, …).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

STOCK_FILE_RE = re.compile(r"^stock_(\d{14})\.txt$", re.IGNORECASE)
SALES_FILE_RE = re.compile(r"^sales_(\d{8})_(\d{8})_(\d{14})\.txt$", re.IGNORECASE)
STOCK_EXPORT_FIELDS = 7
SALES_FIELDS = 5
CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def read_text(path: Path) -> str:
    # utf-8-sig: обработка 1С пишет UTF-8 с BOM (иначе BOM прилипает к первому полю).
    return path.read_text(encoding="utf-8-sig")


def _split_from_the_right(line: str, fields: int) -> list[str] | None:
    """Хвост строки фиксированной длины режем справа, лишние `|` отдаём наименованию."""
    parts = line.split("|")
    if len(parts) < fields:
        return None
    if len(parts) == fields:
        return parts
    tail = fields - 3
    return [parts[0], parts[1], "|".join(parts[2:-tail])] + parts[-tail:]


def parse_stock_rows(content: str) -> list[dict]:
    rows = []
    for line in content.lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = _split_from_the_right(line, STOCK_EXPORT_FIELDS)
        if parts is None:
            parts = line.split("|")          # старый формат без размера и цвета
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


def stock_file_time(path: Path) -> datetime:
    """Момент выгрузки по имени файла (местное время сервера 1С, Москва); если имя
    нестандартное — по времени изменения файла."""
    match = STOCK_FILE_RE.match(path.name)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    return datetime.fromtimestamp(path.stat().st_mtime)


def find_newest_stock_file(dirs) -> Path | None:
    candidates = []
    for directory in dirs:
        directory = Path(directory)
        if directory.is_dir():
            candidates.extend(p for p in directory.glob("stock_*.txt") if p.is_file())
    if not candidates:
        return None
    return max(candidates, key=lambda p: (stock_file_time(p), p.name))


@dataclass
class SalesParseResult:
    rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _parse_amount(text: str) -> Decimal | None:
    text = text.strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"сумма «{text}» не число")


def parse_sales(content: str) -> SalesParseResult:
    result = SalesParseResult()
    for number, line in enumerate(content.lstrip("\ufeff").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != SALES_FIELDS:
            result.errors.append(f"строка {number}: полей {len(parts)}, ожидалось {SALES_FIELDS}")
            continue
        day_text, barcode, qty_text, amount_text, channel = parts
        try:
            day = datetime.strptime(day_text, "%Y%m%d").date()
            qty = Decimal(qty_text.replace(",", "."))
            amount = _parse_amount(amount_text)
        except (ValueError, InvalidOperation) as exc:
            result.errors.append(f"строка {number}: {exc}")
            continue
        channel = channel.lower()
        if not barcode or not CHANNEL_RE.match(channel) or channel == "lamoda":
            result.errors.append(f"строка {number}: пустой баркод или неверный канал «{channel}»")
            continue
        result.rows.append({"sale_date": day, "barcode": barcode, "qty": qty, "revenue": amount,
                            "channel": channel})
    return result


def sales_file_period(path: Path) -> tuple[date, date] | None:
    match = SALES_FILE_RE.match(path.name)
    if not match:
        return None
    return (datetime.strptime(match.group(1), "%Y%m%d").date(),
            datetime.strptime(match.group(2), "%Y%m%d").date())


def find_sales_files(directory: Path | None) -> list[Path]:
    if directory is None or not Path(directory).is_dir():
        return []
    return sorted(p for p in Path(directory).glob("sales_*.txt") if SALES_FILE_RE.match(p.name))

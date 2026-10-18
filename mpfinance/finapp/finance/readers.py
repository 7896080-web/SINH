"""Чтение файла отчёта в таблицы (лист → строки ячеек) и поиск строки заголовков.

Площадки отдают отчёты по-разному: WB — .xlsx внутри .zip, Lamoda и KIT —
.xlsx или .csv (разделитель «;» или «,», кодировка UTF-8 или Windows-1251).
Над таблицей бывают строки-шапки («Отчёт о реализации за период …»), поэтому
строка заголовков ищется, а не берётся первой.
"""
import csv
import io
import re
import zipfile

from openpyxl import load_workbook

from finapp import progress

# Защита от «zip-бомбы» и случайно выбранного гигантского файла: отчёт площадки
# за неделю — единицы мегабайт; распаковка без предела могла бы съесть память
# веб-процесса.
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_ROWS_PER_SHEET = 500_000
# Сколько первых строк листа просматриваем в поисках заголовков.
HEADER_SCAN_ROWS = 30


class ReportReadError(Exception):
    """Файл не читается как таблица — человекочитаемая причина для оператора."""


def _is_xlsx_zip(zf: zipfile.ZipFile) -> bool:
    return "[Content_Types].xml" in zf.namelist()


def _read_xlsx(file_bytes: bytes) -> list[tuple[str, list[list]]]:
    try:
        wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:
        raise ReportReadError(f"Файл не читается как .xlsx ({type(e).__name__}).") from e
    sheets = []
    progress.report(0, None, "чтение файла")
    try:
        for ws in wb.worksheets:
            # Размер листа в режиме read_only берётся из тега <dimension> файла,
            # а не из самих ячеек. Детализация WB приходит с <dimension ref="A1"/>
            # — и без сброса читалась одна колонка «№» из 88, отчёт «не
            # опознавался». Сброс заставляет мерить строки по факту.
            ws.reset_dimensions()
            rows = []
            for raw in ws.iter_rows(values_only=True):
                if len(rows) >= MAX_ROWS_PER_SHEET:
                    raise ReportReadError(
                        f"На листе «{ws.title}» больше {MAX_ROWS_PER_SHEET} строк — "
                        f"разбейте отчёт на несколько файлов по периодам.")
                rows.append(list(raw) if raw is not None else [])
                if len(rows) % 2000 == 0:
                    progress.report(len(rows), None, f"чтение файла: строк {len(rows)}")
            sheets.append((ws.title, rows))
    finally:
        wb.close()
    return sheets


def _decode(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ReportReadError("Не удалось определить кодировку CSV (ожидается UTF-8 или Windows-1251).")


def _read_csv(file_bytes: bytes) -> list[tuple[str, list[list]]]:
    text = _decode(file_bytes)
    sample = "\n".join(text.splitlines()[:HEADER_SCAN_ROWS])
    # csv.Sniffer на русских файлах ошибается: запятая там — десятичный
    # разделитель («1000,00»), и он выбирал её. Площадки пишут CSV через «;»
    # (или табуляцию); запятая-разделитель — только если других нет.
    if ";" in sample:
        delimiter = ";"
    elif "\t" in sample:
        delimiter = "\t"
    else:
        delimiter = ","
    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    if len(rows) > MAX_ROWS_PER_SHEET:
        raise ReportReadError(f"В файле больше {MAX_ROWS_PER_SHEET} строк — разбейте отчёт по периодам.")
    return [("CSV", rows)]


def _read_member(name: str, data: bytes) -> list[tuple[str, list[list]]]:
    lower = name.lower()
    if lower.endswith(".csv") or lower.endswith(".txt"):
        return _read_csv(data)
    return _read_xlsx(data)


def read_sheets(file_bytes: bytes, filename: str = "") -> list[tuple[str, list[list]]]:
    """Все листы файла: [(имя листа, строки)]. .zip распаковывается (берётся
    первый .xlsx/.csv внутри — так WB отдаёт детализацию)."""
    if not file_bytes:
        raise ReportReadError("Файл пустой.")
    if len(file_bytes) > MAX_UNPACKED_BYTES:
        raise ReportReadError("Файл слишком большой для отчёта площадки — разбейте его по периодам.")

    if file_bytes[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(file_bytes))
        except zipfile.BadZipFile as e:
            raise ReportReadError("Файл повреждён или докачался не полностью.") from e
        with zf:
            if _is_xlsx_zip(zf):
                return _read_xlsx(file_bytes)
            members = [i for i in zf.infolist()
                       if not i.is_dir() and not i.filename.startswith("__MACOSX")
                       and re.search(r"\.(xlsx|csv)$", i.filename, re.IGNORECASE)]
            if not members:
                raise ReportReadError("В архиве нет файла .xlsx или .csv.")
            if len(members) > 1:
                names = ", ".join(i.filename for i in members[:5])
                raise ReportReadError(
                    f"В архиве несколько таблиц ({names}) — загрузите их по одной, "
                    f"чтобы отчёты не склеились в один.")
            member = members[0]
            if member.file_size > MAX_UNPACKED_BYTES:
                raise ReportReadError("Файл в архиве слишком большой для отчёта площадки.")
            with zf.open(member) as fh:
                data = fh.read(MAX_UNPACKED_BYTES + 1)
            if len(data) > MAX_UNPACKED_BYTES:
                raise ReportReadError("Файл в архиве слишком большой для отчёта площадки.")
            return _read_member(member.filename, data)

    lower = filename.lower()
    if lower.endswith(".xls") or file_bytes[:4] == b"\xd0\xcf\x11\xe0":
        raise ReportReadError("Старый формат .xls не поддерживается — сохраните отчёт как .xlsx "
                              "(Excel: «Файл → Сохранить как → Книга Excel»).")
    return _read_csv(file_bytes)


# ---------------------------------------------------------------- заголовки

def normalize_header(value) -> str:
    """«К перечислению Продавцу за реализованный Товар » → «к перечислению продавцу
    за реализованный товар». Сравнение заголовков — только по такой форме."""
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("ё", "е").replace("Ё", "Е").lower()
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(":*").strip()
    # Единица в заголовке («Цена реализации, руб», «Комиссия (₽)», у Lamoda
    # «К перечислению комитенту, руб. вкл. НДС») на смысл колонки не влияет, а
    # сравнение по точному тексту из-за неё промахивалось.
    return re.sub(r",?\s*\(?(руб\.?|₽|rub)(\s*вкл\.?\s*ндс)?\)?$", "", text).strip()


def find_header(rows: list[list], aliases: dict[str, tuple[str, ...]],
                required: tuple[str, ...], required_any: tuple[tuple[str, ...], ...] = (),
                min_fields: int = 1) -> tuple[int, dict[str, int], int] | None:
    """Ищет в первых строках листа строку заголовков.

    `required` — поля, которые должны быть все; `required_any` — группы, из
    каждой нужно хотя бы одно поле (например, «выручка ИЛИ цена»); `min_fields` —
    сколько полей минимум, чтобы случайная строка с одним словом «Сумма» не
    сошла за заголовок.

    Возвращает (номер строки, {поле: индекс колонки}, сколько полей нашлось) для
    строки, где совпало больше всего полей, или None."""
    normalized_aliases = {field: [normalize_header(a) for a in names]
                          for field, names in aliases.items()}
    best = None
    for row_idx, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        cells = [normalize_header(c) for c in row]
        mapping: dict[str, int] = {}
        used: set[int] = set()
        # Поля разбираются в порядке объявления, и колонка достаётся первому
        # подходящему: одна колонка «Сумма» не должна стать сразу и выручкой,
        # и выплатой. Внутри поля важен порядок синонимов: у WB есть и
        # «Предмет», и «Название», и наименованием должно стать «Название»,
        # хоть оно и правее.
        for field, names in normalized_aliases.items():
            for name in names:
                col_idx = next((i for i, cell in enumerate(cells)
                                if cell == name and i not in used), None)
                if col_idx is not None:
                    mapping[field] = col_idx
                    used.add(col_idx)
                    break
        if not all(f in mapping for f in required):
            continue
        if not all(any(f in mapping for f in group) for group in required_any):
            continue
        if len(mapping) < min_fields:
            continue
        if best is None or len(mapping) > best[2]:
            best = (row_idx, mapping, len(mapping))
    return best


def header_preview(rows: list[list]) -> list[str]:
    """Самая «заполненная» из первых строк — показать оператору, какие колонки
    мы увидели, когда отчёт не опознан."""
    candidates = [[str(c).strip() for c in row if c not in (None, "")]
                  for row in rows[:HEADER_SCAN_ROWS]]
    return max(candidates, key=len, default=[])

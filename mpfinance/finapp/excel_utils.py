import io
from datetime import datetime, date
from urllib.parse import quote

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from fastapi.responses import StreamingResponse

YES_NO = ["Да", "Нет"]

# По скольким первым строкам прикидываем ширину колонок. Раньше мерили по ВСЕМ:
# на выгрузке каталога это 152 тысячи строк на каждую из семи колонок, то есть
# миллион вызовов len(str(...)) ради числа, которое всё равно упирается в
# потолок в 60 символов. Пятисот строк хватает, чтобы колонка не оказалась
# шириной в заголовок.
WIDTH_SAMPLE_ROWS = 500


def build_xlsx_bytes(headers: list[str], rows: list[list],
                     choices: dict[str, list[str]] | None = None) -> bytes:
    """Сам файл, без обвязки HTTP.

    Вынесено из `build_xlsx_response` ради скриптов, которые собирают файл на
    диск (`scripts/restore_offsets_from_backup.py`). Второй писатель .xlsx
    рядом с этим однажды разошёлся бы с ним — заголовками, ширинами,
    выпадающими списками, — и файл из скрипта перестал бы читаться импортом, а
    узналось бы это в день, когда им пользуются.

    Лист пишется ПОТОКОМ (`write_only`), а не собирается целиком в памяти.
    Обычный режим openpyxl держит на каждую ячейку отдельный объект: замер на
    боевом объёме дал 50 000 строк — 36,5 с и 344 МБ, 152 235 строк — 112 с и
    829 МБ. Выгрузка остатков на дату не ограничена ничем, то есть это второе
    число и есть рабочее: две минуты веб-служба занята одним запросом и держит
    под него почти гигабайт. Потоком те же объёмы — 4,3 с / 50 МБ и
    13,1 с / 103 МБ.
    """
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Данные")

    # Выпадающие списки объявляются ДО первой строки: в потоковом режиме лист
    # после записи уже не переписать, а диапазон известен заранее — он ровно по
    # числу выгруженных строк. На пустом файле проверять нечего, а «на весь
    # столбец» Excel тянет тяжелее.
    if choices and rows:
        for header, options in choices.items():
            if header not in headers:
                continue
            letter = get_column_letter(headers.index(header) + 1)
            rule = DataValidation(
                type="list", formula1='"' + ",".join(options) + '"',
                allow_blank=True, showErrorMessage=True,
                errorTitle="Так нельзя",
                error="Выберите значение из списка: " + ", ".join(options) + ".",
            )
            ws.data_validations.dataValidation.append(rule)
            rule.add(f"{letter}2:{letter}{len(rows) + 1}")

    bold = Font(bold=True)
    header_cells = []
    for header in headers:
        cell = WriteOnlyCell(ws, value=header)
        cell.font = bold
        header_cells.append(cell)
    ws.append(header_cells)

    # Ширина колонок считается по первым WIDTH_SAMPLE_ROWS строкам — в том же
    # проходе, что и запись: второй проход по выгрузке означал бы держать её в
    # памяти целиком ради числа, ограниченного шестьюдесятью символами.
    widths = [len(str(h)) for h in headers]
    for index, row in enumerate(rows):
        if index < WIDTH_SAMPLE_ROWS:
            for col_idx, value in enumerate(row):
                if col_idx < len(widths) and value is not None:
                    length = len(str(value))
                    if length > widths[col_idx]:
                        widths[col_idx] = length
        ws.append(row)

    for col_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = min(width + 3, 60)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_xlsx_response(headers: list[str], rows: list[list], filename: str,
                        choices: dict[str, list[str]] | None = None) -> StreamingResponse:
    """Собирает .xlsx в памяти и отдаёт как файл на скачивание.

    `choices` — колонки, значение в которых выбирается из списка, а не пишется
    руками: {заголовок: варианты}. Excel рисует в таких ячейках выпадающий
    список и не принимает ничего другого.

    Смысл не в удобстве. Импорт понимает «Да/Нет», а всё остальное молча читает
    как «Нет» (`parse_bool_ru`): «да» с опечаткой, «+», «1» латиницей, пустая
    ячейка после вычищенного фильтра — и файл ВЫКЛЮЧАЕТ то, что оператор
    собирался включить, не сказав об этом ни слова.
    """
    buffer = io.BytesIO(build_xlsx_bytes(headers, rows, choices))

    # Content-Disposition должен быть ASCII (latin-1) — кириллическое имя файла
    # кодируем по RFC 5987 (filename*=UTF-8''...), с ASCII-фолбэком для
    # совсем старых клиентов на всякий случай.
    ascii_fallback = "export.xlsx"
    encoded_filename = quote(filename)
    disposition = f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded_filename}'

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


class ExcelReadError(Exception):
    """Файл не читается как .xlsx. Отдельный тип, чтобы роутеры импорта могли
    показать оператору человеческую причину.

    Раньше любая проблема с самим файлом (не тот формат, обрезанная загрузка,
    .xls или .csv, переименованные в .xlsx) вылетала наружу как BadZipFile и
    превращалась в 500: все три импорта аккуратно собирают ошибки по строкам, но
    падали на открытии файла, ещё до первой строки."""


# Потолок на размер загружаемого файла. Не «защита от злоумышленника» — вход под
# паролем, — а защита от промаха: веб-служба читает файл целиком в память и
# распаковывает его, и .xlsx в сотню мегабайт (или .zip, переименованный в .xlsx)
# кладёт её вместе с приёмом заказов и рассылкой. Сорок мегабайт — это заведомо
# больше, чем весит выгрузка всего каталога на 152 тысячи строк.
MAX_UPLOAD_BYTES = 40 * 1024 * 1024


def read_upload(file, limit: int = MAX_UPLOAD_BYTES) -> bytes:
    """Читает загруженный файл, не давая ему съесть память веб-службы."""
    data = file.read(limit + 1)
    if len(data) > limit:
        raise ExcelReadError(
            f"Файл больше {limit // (1024 * 1024)} МБ — столько не весит даже "
            "выгрузка всего каталога. Проверьте, что выбран нужный файл.")
    return data


# Предел по СТРОКАМ, а не только по мегабайтам, и это не вторая перестраховка, а
# единственная работающая. Стоимость чтения — в строках: замер 21.09 показал, что
# 50 000 строк весят 2,5 МБ и читаются 38 с, а боевой каталог на 152 235 строк —
# 7,7 МБ и 123 с. Оба файла проходят потолок в 40 МБ с огромным запасом, то есть
# он не защищает ни от чего: веб-служба, которая в это же время принимает заказы
# с площадок и отдаёт страницы, занята одним запросом ДВЕ МИНУТЫ — и это ещё до
# обработки строк, где на каждую идут запросы в базу.
#
# Двадцать тысяч — тот же порядок, что `BULK_LIMIT` у массовой правки: столько
# читается около пятнадцати секунд. Настройка каталога пачками в это укладывается,
# а файл на весь каталог означает, что оператор выгрузил всё и залил обратно
# целиком — этого делать не надо, и сказать об этом лучше сразу.
MAX_IMPORT_ROWS = 20_000


def read_xlsx_rows(file_bytes: bytes, max_rows: int = MAX_IMPORT_ROWS) -> list[dict]:
    """Читает .xlsx, первая строка — заголовки. Возвращает список словарей
    {заголовок: значение}, пустые строки в конце файла пропускаются.

    Бросает ExcelReadError, если файл не удалось прочитать как .xlsx."""
    if not file_bytes:
        raise ExcelReadError("Файл пустой — выберите .xlsx, выгруженный этой же страницей.")

    try:
        wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active

        rows_iter = ws.iter_rows(values_only=True)
        try:
            headers = next(rows_iter)
        except StopIteration:
            return []

        headers = [str(h).strip() if h is not None else "" for h in headers]

        result = []
        for raw_row in rows_iter:
            if raw_row is None or all(v is None for v in raw_row):
                continue
            row_dict = {}
            for idx, header in enumerate(headers):
                value = raw_row[idx] if idx < len(raw_row) else None
                row_dict[header] = value
            result.append(row_dict)
            if len(result) > max_rows:
                # Отказ, а не обрезка: молча применить первые двадцать тысяч
                # строк и промолчать про остальные — худшее из поведений. Файл
                # правят целиком и считают применённым целиком.
                raise ExcelReadError(
                    f"В файле больше {max_rows} строк. Столько за один раз не "
                    f"обрабатываем: веб-служба в это же время принимает заказы с "
                    f"площадок, а чтение файла на весь каталог занимает у неё до "
                    f"двух минут. Разбейте файл на части или сузьте выгрузку "
                    f"фильтром на странице.")

        return result
    except ExcelReadError:
        raise
    except Exception as e:
        raise ExcelReadError(
            "Файл не читается как .xlsx "
            f"({type(e).__name__}). Проверьте, что это именно .xlsx (не .xls и не .csv, "
            "переименованные в .xlsx) и что он докачался целиком."
        ) from e


def parse_bool_ru(value) -> bool:
    """Разбирает 'Да'/'Нет'/True/False/1/0 из ячейки Excel в bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in ("да", "yes", "true", "1", "истина")


def format_dt(value) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime("%d.%m.%Y %H:%M")
    return ""

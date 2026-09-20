import io
from datetime import datetime, date
from urllib.parse import quote

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from fastapi.responses import StreamingResponse

YES_NO = ["Да", "Нет"]


def build_xlsx_response(headers: list[str], rows: list[list], filename: str,
                        choices: dict[str, list[str]] | None = None) -> StreamingResponse:
    """Собирает .xlsx в памяти и отдаёт как файл на скачивание.

    `choices` — колонки, значение в которых выбирается из списка, а не пишется
    руками: {заголовок: варианты}. Excel рисует в таких ячейках выпадающий
    список и не принимает ничего другого.

    Смысл не в удобстве. Импорт понимает «Да/Нет», а всё остальное молча читает
    как «Нет» (`parse_bool_ru`): «да» с опечаткой, «+», «1» латиницей, пустая
    ячейка после вычищенного фильтра — и файл ВЫКЛЮЧАЕТ то, что оператор
    собирался включить, не сказав об этом ни слова. Проверка на стороне Excel
    ловит это там, где человек ещё видит свою строку.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Данные"

    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for row in rows:
        ws.append(row)

    # Выпадающие списки. Диапазон — ровно по выгруженным строкам: на пустом
    # файле проверять нечего, а «на весь столбец» Excel тянет тяжелее.
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
            # Правило добавляется в лист ДО назначения диапазона: openpyxl
            # связывает его с листом именно в этот момент, и обратный порядок
            # молча даёт файл без проверок.
            ws.add_data_validation(rule)
            rule.add(f"{letter}2:{letter}{len(rows) + 1}")

    # автоширина колонок — грубая эвристика по длине содержимого
    for col_idx, header in enumerate(headers, start=1):
        letter = ws.cell(row=1, column=col_idx).column_letter
        max_len = len(str(header))
        for row in rows:
            value = row[col_idx - 1] if col_idx - 1 < len(row) else ""
            max_len = max(max_len, len(str(value)) if value is not None else 0)
        ws.column_dimensions[letter].width = min(max_len + 3, 60)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

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


def read_xlsx_rows(file_bytes: bytes) -> list[dict]:
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

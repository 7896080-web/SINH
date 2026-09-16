import io
from datetime import datetime, date
from urllib.parse import quote

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from fastapi.responses import StreamingResponse


def build_xlsx_response(headers: list[str], rows: list[list], filename: str) -> StreamingResponse:
    """Собирает .xlsx в памяти и отдаёт как файл на скачивание."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Данные"

    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for row in rows:
        ws.append(row)

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


def read_xlsx_rows(file_bytes: bytes) -> list[dict]:
    """Читает .xlsx, первая строка — заголовки. Возвращает список словарей
    {заголовок: значение}, пустые строки в конце файла пропускаются."""
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

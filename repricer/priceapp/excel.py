"""Excel: выгрузка потоком и чтение с понятной причиной отказа (как у sync_admin)."""
import io
from urllib.parse import quote

from fastapi.responses import StreamingResponse
from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font

# Выгрузка кабинета целиком бывает больше 20 тыс. строк, и её же должно быть можно
# залить обратно (аудит 08.10). Обработчики форм идут в пуле потоков, так что
# долгое чтение файла больше не замораживает интерфейс.
MAX_IMPORT_ROWS = 60000


class ExcelReadError(Exception):
    pass


def xlsx_bytes(headers: list[str], rows: list[list]) -> bytes:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Данные")
    from openpyxl.utils import get_column_letter
    for i, h in enumerate(headers, start=1):
        width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:500]
                                     if i - 1 < len(r) and r[i - 1] is not None])
        ws.column_dimensions[get_column_letter(i)].width = min(width + 3, 60)
    bold = []
    for h in headers:
        c = WriteOnlyCell(ws, value=h)
        c.font = Font(bold=True)
        bold.append(c)
    ws.append(bold)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx_response(headers: list[str], rows: list[list], filename: str) -> StreamingResponse:
    disposition = f"attachment; filename=\"export.xlsx\"; filename*=UTF-8''{quote(filename)}"
    response = StreamingResponse(io.BytesIO(xlsx_bytes(headers, rows)),
                                 media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": disposition})
    # Знак индикатору загрузки (static/busy.js): файл отдан, бегущую строку можно гасить.
    response.set_cookie("download_done", "1", max_age=60, path="/", samesite="lax")
    return response


def read_xlsx_rows(data: bytes) -> list[dict]:
    if not data:
        raise ExcelReadError("Файл пустой — выберите .xlsx, выгруженный этой же страницей.")
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        it = wb.active.iter_rows(values_only=True)
        try:
            headers = [str(h).strip() if h is not None else "" for h in next(it)]
        except StopIteration:
            return []
        out = []
        for raw in it:
            if raw is None or all(v is None for v in raw):
                continue
            out.append({h: (raw[i] if i < len(raw) else None) for i, h in enumerate(headers)})
            if len(out) > MAX_IMPORT_ROWS:
                raise ExcelReadError(f"В файле больше {MAX_IMPORT_ROWS} строк — разбейте его.")
        return out
    except ExcelReadError:
        raise
    except Exception as e:
        raise ExcelReadError(f"Файл не читается как .xlsx ({type(e).__name__}).") from e

"""Предел импорта — в СТРОКАХ, потому что стоимость в строках.

Потолок в сорок мегабайт не защищает ни от чего: замер 21.09 на боевом масштабе
показал, что 50 000 строк весят 2,5 МБ и читаются 38 с, а весь каталог — 152 235
строк — это 7,7 МБ и 123 с. Оба файла проходят мегабайтный потолок с огромным
запасом, а веб-служба, которая в это же время принимает заказы с площадок и
отдаёт страницы, занята одним запросом две минуты. И это ещё до обработки строк,
где на каждую идут запросы в базу.
"""
import io

import pytest
from openpyxl import Workbook

from app.excel_utils import MAX_IMPORT_ROWS, ExcelReadError, read_xlsx_rows


def _file(rows):
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Данные")
    ws.append(["ID_1С", "Резерв"])
    for i in range(rows):
        ws.append([f"u{i}", 0])
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def test_a_normal_file_is_read(tmp_path):
    rows = read_xlsx_rows(_file(50))
    assert len(rows) == 50


def test_a_file_at_the_limit_is_still_read():
    """Граница включительно: оператор, выгрузивший ровно предел, не должен
    получить отказ."""
    rows = read_xlsx_rows(_file(MAX_IMPORT_ROWS), max_rows=MAX_IMPORT_ROWS)
    assert len(rows) == MAX_IMPORT_ROWS


def test_too_many_rows_are_refused_not_truncated():
    """Отказ, а не обрезка. Молча применить часть файла и промолчать про
    остальное — худшее из поведений: файл правят целиком и считают применённым
    целиком, а расхождение всплывёт продажами по несогласованному остатку."""
    with pytest.raises(ExcelReadError) as e:
        read_xlsx_rows(_file(12), max_rows=10)
    assert "10" in str(e.value)
    assert "Разбейте файл" in str(e.value)


def test_the_message_says_what_to_do():
    """Сообщение без выхода превращает предел в тупик: оператор видит отказ и не
    знает, что дальше."""
    with pytest.raises(ExcelReadError) as e:
        read_xlsx_rows(_file(5), max_rows=3)
    text = str(e.value)
    assert "фильтр" in text.lower() or "части" in text.lower()

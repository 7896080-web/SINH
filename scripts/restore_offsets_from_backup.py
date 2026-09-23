"""Вернуть пороги трансляции, схлопнутые массовой кнопкой, — по копии базы.

Запуск (только чтение, НИЧЕГО не меняет):
    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\restore_offsets_from_backup.py ^
        C:\\sync_admin\\backups\\sync_admin-20260923-143142.db

Зачем. «Записать остаток ЦС на дату» ставит факт равным учёту, то есть
объявляет «расхождения нет», и порог честно становится равным брони. Сразу по
всему отбору и молча. Прежний факт при этом затёрт и по текущей базе не
восстановим — а вот в суточной копии он цел.

Порог восстанавливаем ИМЕННО ПОРОГОМ, а не фактом, и это важно: между копией и
сегодняшним днём дату расчёта могли сдвинуть (так и было 23.09 — с 07.08 на
06.07), и факт «на дату» из копии относился бы к другой дате. Порог же описывает
ПОСТОЯННОЕ расхождение учёта со складом, он от даты не зависит — а факт под него
подберёт сам импорт (`offset_base.apply_offset`), уже под сегодняшнюю дату.

Берём только строки, где порог УПАЛ: это направление оверселла — наружу уходит
больше, чем есть. Выросший порог занижает остаток и спешки не требует; такие
строки перечисляются отдельно, но в файл не идут.

Скрипт ничего не применяет. Он собирает .xlsx для штатного импорта на странице
«Товары» — решение по каждой строке остаётся за человеком: часть схлопываний
могла быть намеренной (провели настоящий пересчёт склада, расхождения больше
нет).
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backup import database_path            # noqa: E402
from app.excel_utils import build_xlsx_bytes      # noqa: E402

FIELDS = ("uid_1c", "article", "size", "color", "name", "stock_on_hand", "reserve",
          "broadcast_offset", "offset_base_date", "offset_base_stock", "fact_at_date")


def read_products(path: str) -> dict:
    """Снимок нужных полей по всем товарам. Только чтение, режим ro."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(f"SELECT {', '.join(FIELDS)} FROM products").fetchall()
        return {r["uid_1c"]: dict(r) for r in rows}
    finally:
        con.close()


def outgoing(row: dict, offset) -> int:
    """Сколько уйдёт на площадки при таком пороге — ступени 3–5 лестницы.

    Формула повторяет `transmit.sku_quantity` для режима порога: он считается от
    ТЕКУЩЕГО остатка, а не от того, что был на момент постановки в очередь."""
    stock = row.get("stock_on_hand") or 0
    if offset is not None:
        return max(0, stock - offset)
    return max(0, stock - (row.get("reserve") or 0))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup", help="путь к копии базы (файл .db из backups)")
    parser.add_argument("--out", default="", help="куда положить .xlsx (по умолчанию рядом с копией)")
    parser.add_argument("--limit-print", type=int, default=20)
    args = parser.parse_args()

    if not os.path.exists(args.backup):
        print(f"копия не найдена: {args.backup}")
        return 1

    current_path = database_path()
    if current_path is None or not current_path.exists():
        print("не удалось определить путь к боевой базе из DATABASE_URL")
        return 1
    if os.path.abspath(str(current_path)) == os.path.abspath(args.backup):
        print("указана сама боевая база, а не копия")
        return 1

    was = read_products(args.backup)
    now = read_products(str(current_path))
    print(f"копия:  {args.backup} — товаров {len(was)}")
    print(f"боевая: {current_path} — товаров {len(now)}")

    dropped, raised, impossible = [], [], []
    for uid, new in now.items():
        old = was.get(uid)
        if old is None:
            continue                       # товар завели после копии
        old_offset, new_offset = old["broadcast_offset"], new["broadcast_offset"]
        if old_offset is None or old_offset == new_offset:
            continue
        if new_offset is not None and new_offset > old_offset:
            raised.append((uid, new, old_offset, new_offset))
            continue
        # Порог упал — наружу уходит больше. Проверяем, что восстановить можно:
        # импорт подбирает под порог факт, и отрицательным склад быть не может.
        base = new["offset_base_stock"]
        if base is not None:
            fact = base - old_offset + (new["reserve"] or 0)
            if fact < 0:
                impossible.append((uid, new, old_offset, fact))
                continue
        dropped.append((uid, new, old_offset, new_offset))

    print("-" * 70)
    print(f"порог УПАЛ (наружу уходит больше, чем есть): {len(dropped)}")
    print(f"порог вырос (остаток занижен, не срочно):    {len(raised)}")
    print(f"вернуть нельзя — склад вышел бы отрицательным: {len(impossible)}")

    for uid, row, old_offset, new_offset in dropped[:args.limit_print]:
        print(f"  {row['article']} {row['size'] or ''} {row['color'] or ''}: "
              f"порог {old_offset} → {new_offset}, "
              f"уходит {outgoing(row, new_offset)} → станет {outgoing(row, old_offset)}")
    if len(dropped) > args.limit_print:
        print(f"  … и ещё {len(dropped) - args.limit_print}")
    for uid, row, old_offset, fact in impossible:
        print(f"  РАЗБЕРИТЕ РУКАМИ {row['article']}: порог {old_offset} на дату "
              f"{row['offset_base_date']} дал бы факт {fact}")

    if not dropped:
        print("восстанавливать нечего")
        return 0

    total_now = sum(outgoing(r, n) for _, r, _, n in dropped)
    total_back = sum(outgoing(r, o) for _, r, o, _ in dropped)
    print("-" * 70)
    print(f"сейчас на площадки уходит суммарно {total_now} шт, "
          f"после восстановления будет {total_back} шт "
          f"(лишних в продаже: {total_now - total_back})")

    headers = ["ID_1С", "Порог трансляции", "Артикул", "Размер", "Цвет",
               "Наименование", "Порог сейчас", "Уходит сейчас", "Станет уходить"]
    data = [[uid, old_offset, row["article"], row["size"], row["color"], row["name"],
             new_offset, outgoing(row, new_offset), outgoing(row, old_offset)]
            for uid, row, old_offset, new_offset in dropped]
    # Импорт читает из этого файла ровно две колонки — «ID_1С» и «Порог
    # трансляции»; остальные он не разбирает вовсе, они для глаз.
    out = args.out or os.path.join(os.path.dirname(args.backup), "восстановить_порог.xlsx")
    with open(out, "wb") as fh:
        fh.write(build_xlsx_bytes(headers, data))
    print(f"файл для импорта: {out}")
    print("Проверьте его глазами и залейте на странице «Товары» → «Импорт из Excel».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Вернуть пороги трансляции по копии базы — файлом для штатного импорта.

Запуск (только чтение, НИЧЕГО не меняет):
    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\restore_offsets_from_backup.py ^
        C:\\sync_admin\\backups\\sync_admin-20260922-165926.db --broadcasting --exact

Зачем. «Записать остаток ЦС на дату» ставит факт равным учёту, то есть объявляет
«расхождения нет», и порог схлопывается до брони. Сразу по всему отбору и молча.
Прежний факт при этом затёрт и по текущей базе не восстановим — а в суточной
копии он цел.

Порог восстанавливаем ИМЕННО ПОРОГОМ, а не фактом, и это важно: между копией и
сегодняшним днём дату расчёта могли сдвинуть (так и было 23.09 — и не раз), и
факт «на дату» из копии относился бы к другой дате. Порог же описывает
ПОСТОЯННОЕ расхождение учёта со складом и от даты не зависит — импорт запишет
его как расхождение (`offset_base.apply_offset`: `расхождение = порог - бронь`).

Порогом, а не колонкой «Расхождение», намеренно: сверяем мы то, что УХОДИТ НА
ПЛОЩАДКИ, а бронь между копией и сегодня могли изменить — тогда прежнее
расхождение дало бы уже другой порог. Восстанавливаем ровно отправляемое число.

Два ключа отбора:

  --broadcasting  только товары с ВКЛЮЧЁННОЙ трансляцией. Именно их остаток
                  уходит на площадки прямо сейчас, то есть только по ним
                  неверный порог оборачивается оверселлом. Выключенные можно
                  спокойно разобрать потом.
  --exact         ТОЧНОЕ восстановление: все расхождения порога, а не только
                  упавшие. Без него берутся только упавшие — направление
                  оверселла; с ним ещё и выросшие, где наружу уходит меньше,
                  чем есть.

Скрипт ничего не применяет. Он собирает .xlsx для штатного импорта на странице
«Товары» — решение по каждой строке остаётся за человеком: часть правок могла
быть намеренной (провели настоящий пересчёт склада, расхождения больше нет).
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backup import database_path            # noqa: E402
from app.excel_utils import build_xlsx_bytes     # noqa: E402

FIELDS = ("uid_1c", "article", "size", "color", "name", "stock_on_hand", "reserve",
          "broadcast_enabled", "broadcast_offset", "offset_base_date",
          "offset_base_stock", "fact_at_date")


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
    """Сколько уйдёт на площадки при таком пороге — ступени 4–6 лестницы.

    Формула повторяет `transmit.sku_quantity` для режима порога: он считается от
    ТЕКУЩЕГО остатка, а не от того, что был на момент постановки в очередь.
    Выключатели (ступени 0–3) здесь не при чём: они дают ноль независимо от
    порога, и вопрос «какой порог вернуть» от них не зависит.
    """
    stock = row.get("stock_on_hand") or 0
    if offset is not None:
        return max(0, stock - offset)
    return max(0, stock - (row.get("reserve") or 0))


def plan_restore(was: dict, now: dict, only_broadcasting: bool = False,
                 exact: bool = False) -> dict:
    """Разложить расхождения порога по корзинам. Ничего не меняет.

    Отдельная функция, а не кусок `main`, потому что именно здесь и живёт вся
    цена ошибки: лишняя строка в файле вернёт порог там, где человек изменил его
    намеренно, а недостающая оставит оверселл. Проверяется тестами.

    Корзины:
      dropped   — порог УПАЛ: наружу уходит больше, чем есть. Это оверселл.
      raised    — порог вырос ОТНОСИТЕЛЬНО КОПИИ, то есть возврат его ПОНИЗИТ
                  и отправка вырастет. Сегодня наружу уходит меньше — это
                  безопасная сторона, а возврат ведёт В СТОРОНУ ОВЕРСЕЛЛА.
                  Особенно когда в копии порог ОТРИЦАТЕЛЬНЫЙ: он значит «на
                  складе больше, чем числится в 1С», и отправка тогда превышает
                  остаток ЦС. 23.09 на бою таких нашлось 25, и один вернул бы
                  227 штук при учёте 14. При `--exact` они в файл идут — «точно»
                  значит «как в копии», — но молча этого делать нельзя.
      manual    — в копии порога НЕ БЫЛО, а сейчас есть. Импортом такое не снять:
                  снять порог у строки с датой файлом нельзя, это кнопка
                  «Сбросить порог». Молча выбросить нельзя тем более — человек
                  просил точного восстановления и должен узнать, что по этим
                  строкам оно неполное.
      impossible— вернуть нельзя: подобранный факт вышел бы отрицательным, то
                  есть прежний порог на сегодняшнюю дату невозможен.
    """
    dropped, raised, manual, impossible = [], [], [], []
    for uid, new in now.items():
        if only_broadcasting and not new.get("broadcast_enabled"):
            continue
        old = was.get(uid)
        if old is None:
            continue                       # товар завели после копии
        old_offset, new_offset = old["broadcast_offset"], new["broadcast_offset"]
        if old_offset == new_offset:
            continue
        if old_offset is None:
            manual.append((uid, new, old_offset, new_offset))
            continue
        # Импорт подбирает под порог факт, а склад отрицательным не бывает.
        base = new["offset_base_stock"]
        if base is not None and base - old_offset + (new["reserve"] or 0) < 0:
            impossible.append((uid, new, old_offset, new_offset))
            continue
        (dropped if new_offset is None or old_offset > new_offset else raised).append(
            (uid, new, old_offset, new_offset))

    return {"dropped": dropped, "raised": raised, "manual": manual,
            "impossible": impossible,
            "to_file": dropped + raised if exact else dropped}


HEADERS = ["ID_1С", "Порог трансляции", "Артикул", "Размер", "Цвет", "Наименование",
           "Порог сейчас", "Уходит сейчас", "Станет уходить"]


def to_rows(items: list) -> list[list]:
    """Строки файла. Импорт читает ОТСЮДА ровно две колонки — «ID_1С» и «Порог
    трансляции»; остальные он не разбирает вовсе, они для глаз."""
    return [[uid, old_offset, row["article"], row["size"], row["color"], row["name"],
             new_offset, outgoing(row, new_offset), outgoing(row, old_offset)]
            for uid, row, old_offset, new_offset in items]


def direction_note(now_total: int, back_total: int) -> str:
    """Что файл СДЕЛАЕТ с отправкой — словами и с направлением.

    Отдельная функция ради теста, и это не формальность: первая версия печатала
    предупреждение прямо в `main`, а проверяла его сканированием исходника —
    то есть мутация «обернуть в `if False`» не ловилась ничем, строка оставалась
    в файле, а человек её больше не видел. Ровно тот дефект, что мы весь день
    чиним: у сигнала должен быть читатель, а у проверки — поведение.

    Направление важнее числа. Скрипт заканчивается словами «залейте на
    странице», поэтому нейтральное «после восстановления будет 435» читается как
    отчёт об успехе — а это предложение увеличить отправку на 288 штук.
    """
    if back_total > now_total:
        return (f"ВНИМАНИЕ: файл УВЕЛИЧИТ отправку на {back_total - now_total} шт. "
                f"Это направление оверселла — площадки начнут продавать больше, "
                f"чем есть. Заливайте, только если уверены, что прежние пороги верны.")
    if back_total < now_total:
        return (f"файл СНИЗИТ отправку на {now_total - back_total} шт — столько "
                f"сейчас лишнего в продаже")
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup", help="путь к копии базы (файл .db из backups)")
    parser.add_argument("--broadcasting", action="store_true",
                        help="только товары с включённой трансляцией")
    parser.add_argument("--exact", action="store_true",
                        help="все расхождения порога, а не только упавшие")
    parser.add_argument("--out", default="", help="куда положить .xlsx")
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

    was, now = read_products(args.backup), read_products(str(current_path))
    print(f"копия:  {args.backup} — товаров {len(was)}")
    print(f"боевая: {current_path} — товаров {len(now)}")
    print(f"отбор:  {'только с включённой трансляцией' if args.broadcasting else 'все товары'}"
          f", {'ТОЧНОЕ восстановление (все расхождения)' if args.exact else 'только упавшие пороги'}")

    plan = plan_restore(was, now, args.broadcasting, args.exact)
    print("-" * 70)
    print(f"порог УПАЛ (наружу уходит больше, чем есть): {len(plan['dropped'])}")
    negative = [r for r in plan["raised"] if (r[2] or 0) < 0]
    print(f"порог вырос — возврат ПОВЫСИТ отправку:       {len(plan['raised'])}"
          f"{'' if args.exact else ' — в файл НЕ идут, добавьте --exact'}")
    if negative:
        # Отрицательный порог значит «на складе больше, чем числится в 1С», и
        # отправка тогда ПРЕВЫШАЕТ остаток ЦС. Вернуть его — прямой путь к
        # оверселлу, поэтому такие строки называем отдельно и числом.
        print(f"   из них с ОТРИЦАТЕЛЬНЫМ порогом в копии: {len(negative)} — "
              f"возврат заставит отправлять БОЛЬШЕ, чем числится в 1С")
    print(f"в копии порога не было — снять можно только кнопкой «Сбросить порог»: "
          f"{len(plan['manual'])}")
    if plan["manual"]:
        # Назвать корзину мало — надо сказать СЛЕДСТВИЕ. «Порога не было» и
        # «порог 0» дают наружу одно и то же, ПОКА бронь нулевая: без порога
        # лестница считает `остаток − бронь`, с порогом 0 — весь остаток. При
        # ненулевой брони появление нуля молча ПОДНЯЛО отправку ровно на бронь,
        # и это направление оверселла. 23.09 таких строк нашлось 223, и вопрос
        # «а сколько из них с бронью» решал, беда это или нет.
        raised_by_zero = [r for r in plan["manual"]
                          if outgoing(r[1], r[3]) > outgoing(r[1], r[2])]
        units = sum(outgoing(r[1], r[3]) - outgoing(r[1], r[2]) for r in raised_by_zero)
        if raised_by_zero:
            print(f"   из них ПОДНЯЛИ отправку (бронь перестала вычитаться): "
                  f"{len(raised_by_zero)} строк, +{units} шт — направление оверселла")
        else:
            print(f"   отправку не изменили: бронь нулевая, «нет порога» и "
                  f"«порог 0» дают одно и то же")
    print(f"вернуть нельзя — склад вышел бы отрицательным: {len(plan['impossible'])}")

    for uid, row, old_offset, new_offset in plan["to_file"][:args.limit_print]:
        print(f"  {row['article']} {row['size'] or ''} {row['color'] or ''}: "
              f"порог {old_offset} -> {new_offset}, "
              f"уходит {outgoing(row, new_offset)} -> станет {outgoing(row, old_offset)}")
    if len(plan["to_file"]) > args.limit_print:
        print(f"  … и ещё {len(plan['to_file']) - args.limit_print}")

    # Эти две корзины перечисляем ПОИМЁННО и целиком: файл их не закрывает, и
    # молчание о них означало бы «восстановлено всё», что неправда.
    for uid, row, old_offset, new_offset in plan["manual"]:
        print(f"  РУКАМИ {row['article']} {row['size'] or ''} {row['color'] or ''}: "
              f"порога в копии не было, сейчас {new_offset} — «Сбросить порог»")
    for uid, row, old_offset, new_offset in plan["impossible"]:
        print(f"  РАЗБЕРИТЕ {row['article']} {row['size'] or ''}: порог {old_offset} "
              f"на дату {row['offset_base_date']} дал бы отрицательный склад")

    if not plan["to_file"]:
        print("восстанавливать нечего")
        return 0

    now_total = sum(outgoing(r, n) for _, r, _, n in plan["to_file"])
    back_total = sum(outgoing(r, o) for _, r, o, _ in plan["to_file"])
    print("-" * 70)
    print(f"сейчас на площадки уходит суммарно {now_total} шт, "
          f"после восстановления будет {back_total} шт")
    note = direction_note(now_total, back_total)
    if note:
        print(note)

    out = args.out or os.path.join(os.path.dirname(args.backup), "восстановить_порог.xlsx")
    with open(out, "wb") as fh:
        fh.write(build_xlsx_bytes(HEADERS, to_rows(plan["to_file"])))
    print(f"файл для импорта: {out}")
    print("Проверьте его глазами и залейте на странице «Товары» -> «Импорт из Excel».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

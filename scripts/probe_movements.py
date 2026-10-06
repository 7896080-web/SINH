"""Все движения 1С по размерному ряду одного артикула — только чтение.

Запуск:
    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_movements.py "39681 SELIANIK"
    ... probe_movements.py 39681 M             # только размер M
    ... probe_movements.py 39681 M "MAVI MELANJ"   # размер И цвет
    ... probe_movements.py 39681 "MAVI MELANJ"     # весь ряд одного цвета
    ... probe_movements.py 2000932153711       # по баркоду (найдёт весь ряд)

**Порядок уточнений не значит ничего**: про каждое скрипт спрашивает сам ряд —
размер это или цвет. Раньше цвет стоял третьим, а «весь ряд одного цвета»
требовал пустого второго аргумента, и 03.10 на бою это обернулось отказом
«размера «MAVI MELANJ» среди них НЕТ»: **PowerShell не передаёт нативной
программе пустые строковые аргументы вовсе**, так что цвет встал на место
размера, а отказ соврал о причине. В bash тот же вызов работал — ровно тот
класс «зелёно здесь, красно там», которым открывается `CLAUDE.md`. Плейсхолдер
в интерфейсе скрипта на этом сервере не живёт, поэтому его тут и нет.

**Артикул один не только на размеры, но и на ЦВЕТА.** 03.10 на бою по
«39681 SELIANIK» вышло 45 строк: девять цветов на пять размеров. Человек при
этом называет товар ЦВЕТОМ («Свитшот MAVI MELANJ»), потому что так он выглядит
на витрине, — и получал простыню, в которой нужные пять строк искал глазами.
Цвет сузить обязательно, и пустой размер вторым аргументом это позволяет: ряд
одного цвета — такой же законный вопрос, как один размер всех цветов.

Чем отличается от `probe_offset.py`. Тот отвечает на вопрос «откуда взялся
порог у ОДНОЙ строки» и при многоразмерном артикуле намеренно ОТКАЗЫВАЕТ: числа
по чужому размеру выглядят точно так же, как по нужному. Здесь вопрос другой —
«что происходило с товаром по ВСЕМУ ряду», — и ответ обязан быть сразу по всем
размерам: единица уезжает и возвращается по конкретному баркоду, а человек
держит в руках артикул и видит на витрине ряд.

И главное: тут печатаются САМИ ДВИЖЕНИЯ — задания 1С (`FtpTask`), то есть
документы «Перемещение товаров», возвраты и списания. Их не показывает ни одна
страница по товару: «Диагностика» знает только зависшие, отчёт — только сводку,
а `probe_offset` печатает заказы и очередь рассылки, то есть ПОВОД и СЛЕДСТВИЕ,
но не сам документ. Вопрос «почему на этом размере столько» без списка движений
не имеет ответа: остаток — это их сумма.

Ничего не меняет и не коммитит.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                            # noqa: E402
from app.models import (Barcode, DispatchQueueItem, FtpTask,      # noqa: E402
                        FtpTaskStatus, ProcessedOrder, Product,
                        ReconciliationLog, SyncSetting)
from app.transmit import offset_from_base, sku_quantity           # noqa: E402
from app.workers.reconciliation import _in_flight_adjustment      # noqa: E402

# Консоль боевого сервера пишет в cp1251, и один символ, которого в ней нет,
# роняет ВЕСЬ вывод посреди строки — с трассировкой вместо ответа на вопрос,
# ради которого скрипт и запускали. Свои строки мы держим в пределах cp1251
# (закрыто тестом), но сюда печатаются и ЧУЖИЕ данные: названия товаров,
# артикулы, текст ошибки 1С, имена складов. «?» вместо символа несравнимо
# лучше, чем оборванный вывод.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):  # перенаправленный вывод, старый Python
    pass

# Человеческие имена команд. Берём их СЛОВАМИ, а не как есть: `CANCEL_MOVEMENT`
# и `SCRAP_RETURN` рядом в одном списке читаются как «что-то отменили» и «что-то
# списали», тогда как для остатка это противоположные вещи — первое его
# возвращает на ЦС, второе убирает совсем.
COMMAND_LABELS = {
    "CREATE_MOVEMENT": "продажа   ЦС -> площадка",
    "CANCEL_MOVEMENT": "отмена    площадка -> ЦС",
    "CONFIRM_MOVEMENT": "отгрузка  подтверждение",
    "RETURN_TO_STOCK": "возврат   площадка -> ЦС",
    "SCRAP_RETURN": "утиль     площадка -> ЦС -> списание",
}

# Знак команды для остатка ЦС: на сколько единиц она его меняет, когда 1С её
# провела. Нужен ровно для итоговой строки по размеру, и держать его СПИСКОМ
# нельзя молча: новая команда без знака посчиталась бы нулём, то есть итог
# соврал бы, не сказав об этом. Поэтому незнакомая команда печатается отдельно.
COMMAND_SIGN = {
    "CREATE_MOVEMENT": -1,
    "CANCEL_MOVEMENT": +1,
    "RETURN_TO_STOCK": +1,
    "SCRAP_RETURN": 0,       # вернулась на ЦС и тут же списана: итог нулевой
    "CONFIRM_MOVEMENT": 0,   # движения по ЦС нет вовсе, это смена склада площадки
}

# «В пути» — статусы, которые сверка считает незавершёнными. Спрашиваем их у
# `_in_flight_adjustment`, а не повторяем список: разойдись два счёта, скрипт
# показывал бы «в пути 0» там, где сверка держит единицу, и вопрос «куда девался
# остаток» остался бы без ответа.
OPEN_STATUSES = (FtpTaskStatus.pending, FtpTaskStatus.sent,
                 FtpTaskStatus.timeout, FtpTaskStatus.failed)


def _size_key(product: Product):
    """Порядок размеров: числовой, где он числовой, иначе по алфавиту.

    Ряд печатается сверху вниз, и человек сверяет его с витриной глазами.
    Строковая сортировка даёт 44, 46, 48, 50, 52, 54, 56 верно, но «10, 12, 8»
    неверно, а на буквенных рядах (S, M, L) не помогает никак — поэтому числа
    идут числами, остальное как есть.
    """
    raw = (product.size or "").strip()
    try:
        return (0, float(raw.replace(",", ".")), "")
    except ValueError:
        return (1, 0.0, raw.upper())


def _classify_filters(rows, filters: list[str]) -> tuple[str, str, str]:
    """Про каждое уточнение спрашиваем РЯД: это размер или цвет.

    Возвращает `(размер, цвет, ошибка)`. Выбор делает не позиция аргумента, а
    то, что в ряду действительно есть: размеры сверяются точно, цвет — по куску
    и без регистра (человек называет его так, как видит на витрине, а записан
    он как придётся).

    Неизвестное значение — ОТКАЗ, называющий И размеры, И цвета: сказать про
    него «такого размера нет», не посмотрев в цвета, значит соврать о причине —
    именно так 03.10 выглядел потерянный PowerShell-ом пустой аргумент.
    """
    sizes = {(p.size or "").strip().upper() for p in rows}
    colors = [(p.color or "").strip().upper() for p in rows]
    size, color = "", ""
    for raw in filters:
        value = raw.strip().upper()
        if not value:
            continue
        if value in sizes:
            if size and size != value:
                return "", "", (f"указаны два размера — «{size}» и «{value}»; "
                                f"оставьте один")
            size = value
        elif any(value in c for c in colors):
            if color and color != value:
                return "", "", (f"указаны два цвета — «{color}» и «{value}»; "
                                f"оставьте один")
            color = value
        else:
            return "", "", (
                f"«{raw}» — это ни размер, ни цвет этого артикула.\n"
                f"  размеры: " + ", ".join(sorted(sizes)) + "\n"
                f"  цвета:   " + ", ".join(sorted(set(colors))))
    return size, color, ""


def _find_rows(db, needle: str, filters: list[str]):
    """Строки ряда: по ID_1С, баркоду, артикулу целиком и по куску артикула.

    Баркод обязателен, и не для удобства: артикул человек видит на площадке, а
    он может не совпасть с нашим (пробелы, регистр, другой разделитель цвета) —
    и тогда «не найдено» при том, что строка есть. Найдя товар по баркоду, ряд
    всё равно разворачиваем по его артикулу: спросили про одну единицу, а
    отвечать надо про ряд.
    """
    product = db.query(Product).filter(Product.uid_1c == needle).first()
    if product is None:
        link = db.query(Barcode).filter(Barcode.barcode == needle).first()
        if link is not None:
            product = db.query(Product).filter(
                Product.uid_1c == link.uid_1c).first()
            if product is None:
                # Баркод есть, товара нет: привязка висит в пустоту. Само по
                # себе находка — заказ по такому баркоду разнести не на что.
                print(f"баркод {needle} привязан к {link.uid_1c}, "
                      f"но товара с таким ID_1С в номенклатуре НЕТ")
                return []

    if product is not None and product.article:
        rows = db.query(Product).filter(Product.article == product.article).all()
    elif product is not None:
        rows = [product]
    else:
        rows = db.query(Product).filter(Product.article == needle).all()
        if not rows:
            rows = db.query(Product).filter(
                Product.article.ilike(f"%{needle}%")).all()

    # Кусок артикула попадает и в ЧУЖИЕ артикулы, и это не теория: 06.10 на бою
    # «2617» нашло не ту куртку, а свитшот «2101-22» (размеры L/M/XL/XXL,
    # цвет СИНИЙ). Отказ при этом звучал «52 — ни размер, ни цвет», то есть
    # ВРАЛ О ПРИЧИНЕ: человек шёл проверять размеры, тогда как найден посторонний
    # товар. Молчаливый исход хуже: не уточни человек размер, скрипт напечатал бы
    # числа ЧУЖОГО артикула под шапкой с его именем — ответ, выглядящий ответом.
    found = sorted({(p.article or "").strip() for p in rows})
    if len(found) > 1:
        print(f"«{needle}» — это кусок сразу {len(found)} артикулов. "
              f"Назовите один целиком:")
        for article in found[:20]:
            count = sum(1 for p in rows if (p.article or "").strip() == article)
            print(f"  {article}   строк: {count}")
        if len(found) > 20:
            print(f"  ... и ещё {len(found) - 20}")
        return []

    if filters:
        wanted_size, wanted_color, error = _classify_filters(rows, filters)
        if error:
            # Артикул НАЗЫВАЕМ: по куску мог найтись не тот товар, и тогда
            # вопрос не в размере, а в имени. Без него список «размеры: L, M»
            # читается как утверждение о ТВОЁМ товаре.
            article = (rows[0].article or "-") if rows else "-"
            print(f"нашлось строк: {len(rows)} по артикулу «{article}», но "
                  + error)
            return []
        if wanted_size:
            rows = [p for p in rows
                    if (p.size or "").strip().upper() == wanted_size]
        if wanted_color:
            rows = [p for p in rows
                    if wanted_color in (p.color or "").strip().upper()]

    return sorted(rows, key=_size_key)


def _print_row(db, product: Product) -> dict:
    """Одна строка ряда: числа, движения, заказы, сверка. Возвращает итоги."""
    codes = [b.barcode for b in db.query(Barcode).filter(
        Barcode.uid_1c == product.uid_1c).order_by(Barcode.id).all()]
    marks = [m.account_id for m in db.query(SyncSetting).filter(
        SyncSetting.uid_1c == product.uid_1c,
        SyncSetting.enabled.is_(True)).all()]

    print()
    print("-" * 78)
    print(f"РАЗМЕР {product.size or '-':8} цвет {product.color or '-':22}"
          f" ID_1С {product.uid_1c}")
    print("-" * 78)
    gap = product.stock_discrepancy
    print(f"  остаток ЦС {product.stock_on_hand:>5}   бронь {product.reserve:>4}"
          f"   порог {str(product.broadcast_offset):>5}"
          f"   расхождение {('не измеряли' if gap is None else gap)}")
    print(f"  порог по формуле {offset_from_base(product)}"
          f"   уходит на площадки {sku_quantity(product)}"
          f"   в пути {_in_flight_adjustment(db, product.uid_1c)}")
    print(f"  трансляция {'ВКЛ' if product.broadcast_enabled else 'выкл'}"
          f"   кабинеты {marks or 'нет'}"
          f"   актуализирован {product.recalc_done_at or 'нет'}")
    print(f"  баркоды: {', '.join(codes) if codes else 'НЕТ — движений не найти'}")

    totals = {"stock": product.stock_on_hand, "open": 0, "open_qty": 0,
              "by_command": defaultdict(int), "delta": 0, "unknown": set()}

    # Движения живут на БАРКОДЕ (`FtpTask.barcode`), не на товаре.
    tasks = []
    if codes:
        tasks = db.query(FtpTask).filter(
            FtpTask.barcode.in_(codes),
        ).order_by(FtpTask.id).all()

    print()
    print(f"  ДВИЖЕНИЯ 1С — всего {len(tasks)}:")
    for t in tasks:
        label = COMMAND_LABELS.get(t.command, t.command)
        sign = COMMAND_SIGN.get(t.command)
        qty = t.quantity or 0
        totals["by_command"][t.command] += qty
        if sign is None:
            totals["unknown"].add(t.command)
        if t.status in OPEN_STATUSES:
            totals["open"] += 1
            totals["open_qty"] += qty
        elif t.status is FtpTaskStatus.done and sign:
            totals["delta"] += sign * qty
        # Номер документа 1С — то, по чему строку находят в самой 1С, и он же
        # единственное доказательство, что документ есть. У отказа на его месте
        # текст ошибки, и обрезать его нельзя до неузнаваемости: по нему решают,
        # создавать документ руками или нет.
        detail = (t.result_detail or "").strip()
        print(f"    {t.created_at}  {label:34} {qty:>3} шт"
              f"  {t.status.value:11} {t.result_status or '-':6}"
              f" {detail[:60] or '-'}")
        route = (f"склад {t.warehouse_from} -> {t.warehouse_to}   "
                 if (t.warehouse_from or t.warehouse_to) else "")
        print(f"        {route}"
              f"заказ {t.order_id}   каб.{t.account_id or '-'}"
              f"   док. {t.movement_date or 'тек. дата'}"
              f"   баркод {t.barcode}"
              + (f"   файл {t.batch_filename}" if t.batch_filename else "")
              + ("   ТЕСТОВОЕ" if t.is_test else ""))
    if not tasks:
        print("    пусто — в 1С по этой строке не уезжало НИ ОДНОГО документа")

    orders = db.query(ProcessedOrder).filter(
        ProcessedOrder.uid_1c == product.uid_1c,
    ).order_by(ProcessedOrder.id).all()
    print()
    print(f"  ЗАКАЗЫ ПЛОЩАДОК — всего {len(orders)}:")
    for o in orders:
        # Отмена печатается заметно: по ней остаток вернулся, и именно её ищут,
        # когда в 1С нашёлся обратный документ.
        mark = "  <= ОТМЕНА" if o.status.value == "cancelled" else ""
        print(f"    {o.processed_at}  каб.{o.account_id}"
              f"  заказ {o.order_id:<26} {o.quantity:>3} шт"
              f"  {o.status.value}{mark}")
    if not orders:
        print("    пусто — заказов по этой строке система не проводила")

    queue = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.uid_1c == product.uid_1c,
    ).order_by(DispatchQueueItem.id.desc()).limit(10).all()
    print()
    print("  ЧТО УХОДИЛО НА ПЛОЩАДКИ (последние 10):")
    for q in reversed(queue):
        print(f"    {q.created_at}  каб.{q.account_id}"
              f"  остаток {q.quantity}  ушло {q.sent_quantity}"
              f"  {q.status.value if q.status else '':9} {q.reason}"
              + (f"  площадка держит {q.verified_quantity}"
                 if q.verified_at is not None else ""))
    if not queue:
        print("    пусто")

    last = db.query(ReconciliationLog).filter(
        ReconciliationLog.uid_1c == product.uid_1c,
    ).order_by(ReconciliationLog.id.desc()).first()
    print()
    if last is None:
        print("  СВЕРКА С 1С: ни разу (строки нет в снимке или сверка не доходила)")
    else:
        print(f"  СВЕРКА С 1С {last.checked_at}: у нас {last.python_stock}"
              f" + в пути {last.in_flight} = ждали {last.expected_1c},"
              f" 1С показала {last.actual_1c}, разница {last.delta}"
              f" ({last.classification.value})")
    return totals


def main() -> int:
    if len(sys.argv) < 2:
        print("укажите артикул, ID_1С или баркод; далее размер и/или цвет")
        print('пример: probe_movements.py 39681 M "MAVI MELANJ"')
        print('        probe_movements.py 39681 "MAVI MELANJ"')
        return 1
    needle = sys.argv[1].strip()
    # Уточнения — СПИСКОМ, и порядок в нём не значит ничего: размер это или
    # цвет, решает сам ряд. Пустые отбрасываем здесь же — в bash «""» доедет
    # пустой строкой, в PowerShell не доедет вовсе, и ни то ни другое не должно
    # менять смысл вызова.
    filters = [a for a in (arg.strip() for arg in sys.argv[2:]) if a]

    db = SessionLocal()
    try:
        rows = _find_rows(db, needle, filters)
        if not rows:
            if not filters:
                print(f"не найдено ни по ID_1С, ни по баркоду, ни по артикулу: "
                      f"{needle}")
            return 1

        article = rows[0].article or "-"
        print("=" * 78)
        print(f"АРТИКУЛ: {article}")
        print(f"Название: {rows[0].name or '-'}")
        # Размеры и цвета печатаются БЕЗ ПОВТОРОВ. По 45 строкам список
        # выходил как «L, L, L, L, L, L, L, L, L, M, M, M…» — он не говорил ни
        # сколько размеров, ни сколько цветов, то есть занимал три строки
        # экрана и не отвечал ни на один вопрос.
        sizes = sorted({(p.size or "-") for p in rows},
                       key=lambda s: _size_key(Product(size=s)))
        colors = sorted({(p.color or "-") for p in rows})
        print(f"Строк 1С: {len(rows)}"
              f"   размеров {len(sizes)}: " + ", ".join(sizes))
        print(f"           цветов {len(colors)}: " + ", ".join(colors))
        print("-" * 78)
        # Про пояс говорим ПРЯМО, и это не вежливость. Всё время в базе UTC, а
        # документы в 1С стоят по местному: у Москвы UTC+3, и человек, сверяющий
        # список с журналом 1С, иначе ищет движение на три часа не там — а не
        # найдя, решает, что его нет.
        print("Время везде UTC. Боевой сервер и 1С живут по Москве (UTC+3):")
        print("прибавьте 3 часа, чтобы сойтись с датами документов в 1С.")
        print("Движения ищутся ПО БАРКОДАМ строки. Перевязка баркода меняет этот")
        print("список в обе стороны: движение, созданное когда баркод вёл к другому")
        print("товару, попадёт сюда, а по отвязанному баркоду — исчезнет.")

        total = {"stock": 0, "open": 0, "open_qty": 0, "delta": 0}
        by_command: dict[str, int] = defaultdict(int)
        unknown: set[str] = set()
        for product in rows:
            totals = _print_row(db, product)
            total["stock"] += totals["stock"]
            total["open"] += totals["open"]
            total["open_qty"] += totals["open_qty"]
            total["delta"] += totals["delta"]
            for command, qty in totals["by_command"].items():
                by_command[command] += qty
            unknown |= totals["unknown"]

        print()
        print("=" * 78)
        print("ИТОГО ПО РЯДУ")
        print("=" * 78)
        print(f"  остаток ЦС по всем размерам: {total['stock']}")
        print(f"  незакрытых заданий 1С («в пути»): {total['open']}"
              f" на {total['open_qty']} шт")
        print(f"  проведено движений на остаток ЦС: {total['delta']:+d} шт")
        for command in sorted(by_command):
            print(f"    {COMMAND_LABELS.get(command, command):34}"
                  f" {by_command[command]:>5} шт (во всех статусах)")
        if not by_command:
            print("    движений нет вовсе")
        if unknown:
            # Молчать нельзя: незнакомая команда посчиталась бы нулём, и итог
            # соврал бы, не сказав об этом.
            print("  ВНИМАНИЕ: команды без известного знака для остатка — "
                  + ", ".join(sorted(unknown))
                  + "; в строку «проведено движений» они НЕ вошли")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

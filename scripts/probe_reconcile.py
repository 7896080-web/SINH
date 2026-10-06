"""Почему остаток КАЧАЕТСЯ: разбор сверки по слагаемым. Только чтение.

Запуск:
    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_reconcile.py "2617 C24-2317CQ"
    ... probe_reconcile.py 2617 52 "BRICK RED"    # размер и/или цвет
    ... probe_reconcile.py 2000932153711          # по баркоду (найдёт весь ряд)
    ... probe_reconcile.py 2617 52 --hours 48     # глубже суток

Повод. 06.10 на странице «Крупные расхождения со складом 1С за сутки» по одним
и тем же позициям стояли ПАРЫ строк: в 12:33 остаток 8 -> 16 (+8), в 13:33
16 -> 8 (-8). И так час за часом, по десятку позиций сразу. Само по себе это не
говорит ни о чём: страница показывает только ИТОГ (было/стало/разница), а сверка
считает его из трёх слагаемых, и качание даёт любое из них.

    expected_1c = наш остаток + «в пути»
    delta       = остаток 1С - expected_1c
    новый наш   = остаток 1С - «в пути»

Отсюда ровно два подозреваемых, и лечатся они по-разному:

  * качается `actual_1c` — РАЗНОЕ ОТДАЁТ 1С. Либо два источника спорят (часовой
    снимок и минутная дельта), либо баркод привязан к чужому товару и сверка
    берёт МАКСИМУМ по баркодам товара, то есть ЧУЖОЙ остаток (`uid_to_actual` в
    `reconciliation.run_reconciliation`). Чинится мэппингом, и до починки на
    площадку уходит число соседнего размера.
  * качается `in_flight` — наши задания 1С то считаются «в пути», то нет.
    Открытое задание (`pending`/`sent`/`timeout`/`failed`) живёт в этом счёте
    вечно, а закрытое попадает в него, пока `completed_at >= snapshot_at`, то
    есть пока снимок старше закрытия. Чинится разбором зависшего задания.

Скрипт печатает САМИ слагаемые по часам и говорит, которое из них качается:
«сверка сломалась» — не диагноз, а по итоговой разнице эти два случая
неразличимы, и человек идёт чинить не то.

Ничего не меняет и не коммитит.
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                            # noqa: E402
from app.models import (Barcode, FtpTask, Product,                # noqa: E402
                        ReconciliationLog)
from app.timeutils import now_utc                                 # noqa: E402
from scripts.probe_movements import _classify_filters, _size_key  # noqa: E402

# Консоль боевого сервера пишет в cp1251, и один символ, которого в ней нет,
# роняет ВЕСЬ вывод посреди строки. Сюда печатаются ЧУЖИЕ данные — названия
# товаров, артикулы, имена складов из заданий, — и «?» вместо символа
# несравнимо лучше, чем оборванный ответ на вопрос, ради которого запускали.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):  # перенаправленный вывод, старый Python
    pass

DEFAULT_HOURS = 24
# Сколько раз подряд знак расхождения должен смениться, чтобы это считалось
# качанием. Два подряд (+ - +) бывают и при обычной торговле; три — уже узор.
MIN_SWINGS = 3


def _sign_swings(deltas: list[int]) -> int:
    """Сколько раз знак расхождения сменился на противоположный.

    Качание — это когда остаток ВОЗВРАЩАЕТСЯ: час уехал вверх, час вниз на
    столько же. Считать при этом сами ЗНАЧЕНИЯ нельзя, и это главное про эту
    функцию: качание на бою идёт НА ФОНЕ ПРОДАЖ. Пара 8 <-> 16 после одной
    продажи становится 7 <-> 15, и признак «всего два разных числа» промолчал бы
    ровно там, где качание есть. Знак же чередуется при любом фоне.
    """
    signs = [1 if d > 0 else -1 for d in deltas if d != 0]
    return sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])


def _steady(values: list[int]) -> bool:
    """Слагаемое стояло на месте всё время разбора."""
    return len(set(values)) == 1


def _verdict(rows: list[ReconciliationLog]) -> list[str]:
    """Что именно качается. Пустой список — качания нет.

    Сначала отвечаем «качается ли вообще» — по чередованию ЗНАКА расхождения, а
    не по значениям. И только потом «что именно»: слагаемое, которое всё это
    время стояло на месте, виноватым быть не может.
    """
    deltas = [r.delta for r in rows]
    if _sign_swings(deltas) < MIN_SWINGS:
        return []

    actual = [r.actual_1c for r in rows]
    flight = [r.in_flight for r in rows]
    out = []

    if not _steady(actual):
        values = sorted(set(actual))
        shown = " <-> ".join(str(v) for v in values[:6])
        out.append(
            f"КАЧАЕТСЯ ОСТАТОК 1С: {shown}\n"
            "  Это НЕ наши задания: одна и та же выгрузка приносит то одно "
            "число, то другое.\n"
            "  Два объяснения, и оба видны ниже. (1) У строки чужой баркод: "
            "сверка берёт\n"
            "  МАКСИМУМ по баркодам товара, то есть остаток соседнего размера, "
            "и он же\n"
            "  уходит на площадку. (2) Часовой снимок и минутная дельта спорят "
            "между собой:\n"
            "  дельта приходит по ОДНОМУ баркоду, снимок несёт все, и максимум "
            "берётся\n"
            "  только во втором. Смотрите баркоды строки и ряд целиком.")

    if not _steady(flight):
        values = sorted(set(flight))
        shown = " <-> ".join(str(v) for v in values[:6])
        out.append(
            f"КАЧАЕТСЯ «В ПУТИ»: {shown}\n"
            "  Качаются НАШИ задания: то считаются открытыми, то нет.\n"
            "  Открытое задание (pending/sent/timeout/failed) считается «в "
            "пути» вечно, а\n"
            "  закрытое — пока снимок старше его закрытия "
            "(`completed_at >= snapshot_at`).\n"
            "  Смотрите список заданий ниже: зависшее разбирают на "
            "«Диагностике».")

    if not out:
        out.append(
            "ЗНАК РАСХОЖДЕНИЯ ЧЕРЕДУЕТСЯ, а оба слагаемых стоят на месте.\n"
            "  Значит ходит НАШ остаток, и двигает его не сверка: смотрите "
            "заказы,\n"
            "  возвраты и правки руками по этому товару (`probe_movements.py`).")
    return out


def _print_barcodes(db, product: Product) -> None:
    """Баркоды строки — и почему их число решает, какой остаток к ней приедет.

    Один баркод ведёт ровно на один товар: `barcodes.barcode` уникален, так что
    «баркод привязан к двоим» в базе невозможно, и искать это бесполезно. Чужим
    баркод оказывается иначе — он лежит у НЕ СВОЕЙ строки ряда, и снаружи это
    видно только перекосом: у одного размера три баркода, у соседнего ни одного.

    Цена перекоса в сверке прямая. Несколько штрихкодов одного SKU держат ОДИН
    физический остаток, и 1С отдаёт по ним одно число; разные числа означают
    чужой баркод, а `run_reconciliation` берёт по товару МАКСИМУМ — то есть на
    площадку уезжает остаток соседнего размера. Сама сверка это замечает
    (`stats["barcode_conflicts"]`), но говорит об этом ТОЛЬКО строкой WARNING в
    логе воркера, поэтому здесь и напечатано, где её искать.
    """
    links = db.query(Barcode).filter(Barcode.uid_1c == product.uid_1c).all()
    print(f"    баркоды ({len(links)}):",
          ", ".join(b.barcode for b in links) or "нет")
    if len(links) > 1:
        print("      у строки НЕСКОЛЬКО баркодов: 1С обязана отдавать по ним")
        print("      одно число, а разные означают чужой баркод — сверка берёт")
        print("      МАКСИМУМ, то есть остаток соседнего размера. Сверьте ряд")
        print("      ниже и проверьте строку в логе воркера:")
        print('        Select-String "РАЗНЫЕ остатки по разным баркодам" '
              'C:\\sync_admin\\logs\\worker.err.log -Encoding UTF8')


def _print_tasks(db, product: Product, since) -> None:
    """Задания 1С по баркодам строки — это и есть слагаемые «в пути»."""
    barcodes = [b.barcode for b in
                db.query(Barcode).filter(Barcode.uid_1c == product.uid_1c).all()]
    if not barcodes:
        return
    tasks = (db.query(FtpTask)
             .filter(FtpTask.barcode.in_(barcodes), FtpTask.is_test.is_(False))
             .order_by(FtpTask.id.desc()).limit(15).all())
    if not tasks:
        print("    заданий 1С по этим баркодам нет")
        return
    print("    задания 1С (последние 15):")
    for t in reversed(tasks):
        done = t.completed_at.strftime("%d.%m %H:%M") if t.completed_at else "-"
        print(f"      {t.id:>7}  {t.command:<16} {t.status.value:<8} "
              f"кол-во {t.quantity or 0:>4}  создано "
              f"{t.created_at.strftime('%d.%m %H:%M') if t.created_at else '-'}"
              f"  закрыто {done}")


def _print_product(db, product: Product, hours: int) -> None:
    since = now_utc() - timedelta(hours=hours)
    rows = (db.query(ReconciliationLog)
            .filter(ReconciliationLog.uid_1c == product.uid_1c,
                    ReconciliationLog.checked_at >= since)
            .order_by(ReconciliationLog.checked_at).all())

    print()
    print("-" * 78)
    print(f"  разм. {product.size or '-':<6} {product.color or '-':<14} "
          f"наш остаток сейчас: {product.stock_on_hand}"
          f"   трансляция: {'ВКЛ' if product.broadcast_enabled else 'выкл'}")
    print("-" * 78)

    if not rows:
        print(f"    сверка по этой строке за {hours} ч не проходила ни разу")
    else:
        print("    время (UTC)        наш   +в пути   ждали   1С дала   разница")
        for r in rows:
            mark = "" if r.delta == 0 else "  <-- расхождение"
            print(f"    {r.checked_at.strftime('%d.%m %H:%M:%S')}  "
                  f"{r.python_stock:>7} {r.in_flight:>+8} {r.expected_1c:>8}"
                  f" {r.actual_1c:>9} {r.delta:>+9}{mark}")
        print(f"    строк сверки: {len(rows)}, из них с расхождением: "
              f"{sum(1 for r in rows if r.delta != 0)}")

        for line in _verdict(rows):
            print()
            print("    " + line.replace("\n", "\n    "))

    print()
    _print_barcodes(db, product)
    _print_tasks(db, product, since)


def main() -> int:
    args = [a for a in (arg.strip() for arg in sys.argv[1:]) if a]
    hours = DEFAULT_HOURS
    if "--hours" in args:
        i = args.index("--hours")
        if i + 1 >= len(args) or not args[i + 1].isdigit():
            print("--hours требует числа часов, например: --hours 48")
            return 1
        hours = int(args[i + 1])
        del args[i:i + 2]

    if not args:
        print("укажите артикул, ID_1С или баркод; далее размер и/или цвет")
        print('пример: probe_reconcile.py 2617 52 "BRICK RED"')
        print('        probe_reconcile.py "2617 C24-2317CQ" --hours 48')
        return 1

    needle, filters = args[0], args[1:]

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.uid_1c == needle).first()
        if product is None:
            link = db.query(Barcode).filter(Barcode.barcode == needle).first()
            if link is not None:
                product = db.query(Product).filter(
                    Product.uid_1c == link.uid_1c).first()

        if product is not None and product.article:
            rows = db.query(Product).filter(
                Product.article == product.article).all()
        elif product is not None:
            rows = [product]
        else:
            rows = db.query(Product).filter(Product.article == needle).all()
            if not rows:
                rows = db.query(Product).filter(
                    Product.article.ilike(f"%{needle}%")).all()

        if not rows:
            print(f"не найдено ни по ID_1С, ни по баркоду, ни по артикулу: "
                  f"{needle}")
            return 1

        if filters:
            # Уточнения СПИСКОМ, и порядок в них не значит ничего: размер это
            # или цвет, решает сам ряд. Правило общее с `probe_movements`, и
            # функция та же НАМЕРЕННО: повтори её здесь, она однажды разошлась
            # бы с соседкой, и один и тот же вызов на двух скриптах отвечал бы
            # по-разному.
            size, color, error = _classify_filters(rows, filters)
            if error:
                print(f"нашлось строк: {len(rows)}, но " + error)
                return 1
            if size:
                rows = [p for p in rows
                        if (p.size or "").strip().upper() == size]
            if color:
                rows = [p for p in rows
                        if color in (p.color or "").strip().upper()]

        rows = sorted(rows, key=_size_key)

        print("=" * 78)
        print(f"АРТИКУЛ: {rows[0].article or '-'}")
        print(f"Название: {rows[0].name or '-'}")
        print(f"Строк: {len(rows)}   глубина разбора: {hours} ч")
        print("Время везде UTC. Боевой сервер и 1С живут по Москве (UTC+3):")
        print("прибавьте 3 часа, чтобы сойтись с датами документов в 1С.")
        print("=" * 78)
        print("Как читается строка сверки:")
        print("  ждали = наш остаток + в пути;  разница = 1С дала - ждали;")
        print("  после сверки наш остаток становится «1С дала - в пути».")

        for product in rows:
            _print_product(db, product, hours)

        # Ряд целиком, одной таблицей. Перекос баркодов (у одного размера три,
        # у соседнего ни одного) по отдельной строке не виден вовсе: там три
        # баркода выглядят нормально. А это ровно тот случай, когда сверка
        # берёт по товару максимум и привозит остаток чужого размера.
        print()
        print("=" * 78)
        print("РЯД ЦЕЛИКОМ — сколько баркодов у каждой строки")
        print("=" * 78)
        for p in sorted(db.query(Product).filter(
                Product.article == (rows[0].article or "")).all(), key=_size_key):
            codes = [b.barcode for b in
                     db.query(Barcode).filter(Barcode.uid_1c == p.uid_1c).all()]
            mark = "  <-- баркодов нет вовсе" if not codes else ""
            print(f"  разм. {p.size or '-':<6} {p.color or '-':<14} "
                  f"остаток {p.stock_on_hand:>5}   баркодов {len(codes)}{mark}")
            if codes:
                print(f"      {', '.join(codes)}")

        print()
        print("=" * 78)
        print("Скрипт только читает. Остаток не трогает, в 1С ничего не шлёт.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Быстрые отмены по кабинету: их ли это поток и откуда — только чтение.

Запуск (окно по умолчанию — четырнадцать суток: отмены редки, и за двое
суток поток от единичного случая не отличить):

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_cancels.py КАРАМАН
    ... probe_cancels.py КАРАМАН 30      # за тридцать суток
    ... probe_cancels.py все             # по всем кабинетам сразу

Зачем отдельный скрипт, когда есть `probe_order.py`. Тот отвечает про ОДИН
заказ: «кто заказал эту отмену». А главный вопрос другой — единичный это случай
или поток, — и по одной строке он не решается никогда: одна отмена выглядит
одинаково и когда её сделал человек в кабинете, и когда её делает автоматика
сорок раз в день.

**Быстрая отмена — та, что случилась в пределах `FAST` от приёма заказа.**
Человек, глядящий на список сборочных заданий, за двадцать секунд новый заказ
не найдёт и не отменит. Значит поток таких — это автоматика, и вопрос сужается
до «чья».

Два подозреваемых, и различает их ОДНО поле — наш собственный ноль.

  * **Отменяем мы сами, не зная того.** Приём заказа списывает единицу; если она
    была последней, остаток становится нулём и рассылка отправляет ноль на тот
    же кабинет. Площадка видит, что товара у продавца нет, и отменяет ещё не
    собранное задание. Отмена возвращает единицу, следующая рассылка везёт
    единицу обратно, приходит новый заказ — и круг замыкается. Признак: ноль на
    этот кабинет по этому товару ушёл МЕЖДУ приёмом и отменой.
  * **Отменяет кто-то снаружи** — вторая система с ключами от кабинета или сам
    WB (просрочка сборки, отказ по правилам). Признак: нуля в окне нет вовсе.

Поэтому в выводе рядом с каждой быстрой отменой стоит наша отправка нуля, если
она была, а в конце — разбор по большинству. И обязательно ДАТА ПОСЛЕДНЕЙ
быстрой отмены: «прекратилось после такого-то числа» и «случилось сегодня» —
это два разных дела, а выглядят в сводке одинаково.

Дни считаются МЕСТНЫЕ: у Москвы UTC+3, и всё, что после 21:00 UTC, относится к
следующему числу. Сводка по дням, посчитанная в UTC, разложила бы вечерние
отмены во вчера — и «прекратилось такого-то» указало бы не на тот день.

Ничего не меняет и не коммитит.
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                               # noqa: E402
from app.models import (DispatchQueueItem, DispatchStatus,          # noqa: E402
                        OrderProcessStatus, PlatformAccount,
                        ProcessedOrder, Product)
from app.timeutils import local_date_of, now_utc                    # noqa: E402

# Консоль боевого сервера пишет в cp1251, а сюда печатаются ЧУЖИЕ данные:
# названия товаров и артикулы из 1С. Один символ, которого в кодировке нет,
# уронил бы ВЕСЬ вывод посреди строки — человек получил бы трассировку вместо
# ответа. «?» вместо символа несравнимо лучше.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):  # перенаправленный вывод, старый Python
    pass

# Граница «быстрой» отмены. Десять минут, а не минута: человек в кабинете
# теоретически успевает и за минуту, а вот поток отмен в пределах десяти минут
# от приёма — это уже не человек, и сузив окно, мы потеряли бы ровно те строки,
# ради которых скрипт написан.
FAST = timedelta(minutes=10)

DEFAULT_DAYS = 14
# Сколько быстрых отмен печатать поимённо. Остальные считаются числом: список на
# двести строк не читают, а сводка по дням под ним нужна целиком.
SHOW = 25

# Меньше этого числа — вывода о ПРИЧИНЕ не делаем вовсе.
#
# Разбор идёт по большинству («у скольких из них был наш ноль»), а на двух
# строках большинство — это одна строка. Первый же боевой прогон это и показал:
# 1869 принятых заказов за тридцать суток, быстрых отмен ДВЕ, у одной ноль был —
# и скрипт уверенно объявил причиной нас. Уверенность там была ложная: два
# случая на две тысячи заказов это не поток, а совпадение, и причина у каждого
# может быть своя. Хуже того, такой вердикт отправляет человека чинить запас по
# товарам, с которыми всё в порядке.
#
# Молчание тут честнее: скрипт говорит, что материала мало, и показывает обе
# строки поимённо — дальше их разбирают по одной через `probe_order.py`.
MIN_FOR_VERDICT = 5


def _hhmmss(value) -> str:
    return value.strftime("%H:%M:%S") if value else "-"


def _gap(seconds: float) -> str:
    if seconds < 60:
        return f"через {int(seconds)} с"
    if seconds < 3600:
        return f"через {int(seconds // 60)} мин"
    return f"через {seconds / 3600:.1f} ч"


def _product_name(products, uid) -> str:
    p = products.get(uid)
    if p is None:
        return uid or "товар не определён"
    parts = [p.article or "", p.size or "", p.color or ""]
    return " / ".join(x for x in parts if x) or (uid or "")


def main() -> int:
    if len(sys.argv) < 2:
        print("Укажите кабинет (можно куском имени) или «все», например:")
        print("  probe_cancels.py КАРАМАН")
        print("  probe_cancels.py все 30")
        return 2
    wanted = sys.argv[1].strip()
    try:
        days = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DAYS
    except ValueError:
        print(f"Второй аргумент - число суток, а не {sys.argv[2]!r}")
        return 2

    db = SessionLocal()
    try:
        accounts = db.query(PlatformAccount).all()
        if wanted.lower() in ("все", "all", "*"):
            chosen = accounts
        else:
            chosen = [a for a in accounts if wanted.lower() in a.name.lower()]
        if not chosen:
            print(f"Кабинет по куску имени {wanted!r} не нашёлся. Есть такие:")
            for a in accounts:
                print(f"  {a.name} ({a.platform.value})")
            return 1

        since = now_utc() - timedelta(days=days)
        ids = [a.id for a in chosen]
        names = ", ".join(f"{a.name} ({a.platform.value})" for a in chosen)
        print(f"=== ОТМЕНЫ: {names}, {days} суток ===")
        print(f"«Быстрая» — отмена в пределах {int(FAST.total_seconds() // 60)} "
              f"минут от приёма заказа.")
        print()

        orders = (db.query(ProcessedOrder)
                  .filter(ProcessedOrder.account_id.in_(ids),
                          ProcessedOrder.processed_at >= since)
                  .order_by(ProcessedOrder.processed_at).all())
        cancelled = [o for o in orders
                     if o.status == OrderProcessStatus.cancelled and o.cancelled_at]
        fast = [o for o in cancelled if o.cancelled_at - o.processed_at <= FAST]

        # Сводка по дням — МЕСТНЫМ. Считай мы их в UTC, вечерние отмены легли бы
        # во вчера, и «прекратилось такого-то числа» указало бы не на тот день.
        by_day: dict = {}
        for o in orders:
            day = local_date_of(o.processed_at)
            row = by_day.setdefault(day, [0, 0, 0])
            row[0] += 1
        for o in cancelled:
            by_day.setdefault(local_date_of(o.processed_at), [0, 0, 0])[1] += 1
        for o in fast:
            by_day.setdefault(local_date_of(o.processed_at), [0, 0, 0])[2] += 1

        print("-- по дням (местное число) --")
        print("  дата         принято  отменено  из них быстрых")
        for day in sorted(by_day):
            got, canc, quick = by_day[day]
            print(f"  {day.strftime('%d.%m.%Y')}  {got:>7}  {canc:>8}  {quick:>14}")
        if not by_day:
            print("  заказов за это окно нет вовсе")
        print()

        # Нули, ушедшие на те же пары. Берём ОДНИМ запросом на весь набор, а не
        # подзапросом на строку: на боевом масштабе второе означало бы чтение
        # очереди на каждую отмену.
        zeros: dict = {}
        if fast:
            pairs = {(o.uid_1c, o.account_id) for o in fast if o.uid_1c}
            uids = {u for u, _ in pairs}
            rows = (db.query(DispatchQueueItem)
                    .filter(DispatchQueueItem.uid_1c.in_(uids),
                            DispatchQueueItem.account_id.in_(ids),
                            DispatchQueueItem.status == DispatchStatus.sent,
                            DispatchQueueItem.sent_quantity == 0,
                            DispatchQueueItem.sent_at >= since).all())
            for r in rows:
                zeros.setdefault((r.uid_1c, r.account_id), []).append(r.sent_at)

        products = {}
        if fast:
            uids = [o.uid_1c for o in fast if o.uid_1c]
            if uids:
                products = {p.uid_1c: p for p in
                            db.query(Product).filter(Product.uid_1c.in_(uids)).all()}

        print(f"-- быстрые отмены: {len(fast)} из {len(cancelled)} отменённых --")
        with_zero = 0
        for o in fast[:SHOW]:
            gap = (o.cancelled_at - o.processed_at).total_seconds()
            print(f"  {o.order_id}  {_product_name(products, o.uid_1c)}")
            print(f"    принят {_hhmmss(o.processed_at)}, "
                  f"отменён {_hhmmss(o.cancelled_at)} ({_gap(gap)}), "
                  f"{local_date_of(o.processed_at).strftime('%d.%m.%Y')}")
            # Ноль ИМЕННО В ОКНЕ: отправка до приёма про эту отмену не говорит
            # ничего, а после отмены — это уже её следствие, а не причина.
            sent = [t for t in zeros.get((o.uid_1c, o.account_id), [])
                    if o.processed_at <= t <= o.cancelled_at]
            if sent:
                with_zero += 1
                first = min(sent)
                print(f"    НАШ НОЛЬ ушёл {_hhmmss(first)} — за "
                      f"{int((o.cancelled_at - first).total_seconds())} с до отмены")
        if len(fast) > SHOW:
            print(f"  ... и ещё {len(fast) - SHOW}")
        if not fast:
            print("  нет ни одной")
        print()

        print("-- откуда --")
        if not fast:
            print("  Быстрых отмен нет. Отмены, если они были, растянуты по")
            print("  времени - это обычная работа человека в кабинете.")
            return 0

        last_day = max(local_date_of(o.processed_at) for o in fast)
        today = local_date_of(now_utc())
        # Масштаб называем ЧИСЛОМ и рядом с выводом. По сводке его надо
        # складывать глазами по дням, а именно он решает, поток это или
        # совпадение, — то есть без него любой вывод ниже читается крупнее, чем
        # он есть.
        print(f"  Быстрых отмен {len(fast)} при {len(orders)} принятых заказах "
              f"за окно.")
        if len(fast) < MIN_FOR_VERDICT:
            print(f"  Этого МАЛО для вывода о причине: разбор идёт по большинству,")
            print(f"  а на {len(fast)} строках большинство ничего не значит. Поток")
            print("  автоматики выглядел бы десятками в день.")
            print(f"  Наш ноль в окне был у {with_zero} из {len(fast)}.")
            print("  Разбирайте их по одной: probe_order.py <номер заказа>.")
        elif with_zero * 2 >= len(fast):
            print(f"  У {with_zero} быстрых отмен из {len(fast)} НАШ НОЛЬ ушёл на")
            print("  кабинет между приёмом и отменой. Значит отменяет площадка, и")
            print("  повод даём мы: приём заказа списал последнюю единицу, рассылка")
            print("  увезла ноль, площадка увидела, что товара нет, и сняла ещё не")
            print("  собранное задание. Круг замыкается сам: отмена вернула единицу,")
            print("  следующая рассылка везёт её обратно, приходит новый заказ.")
            print("  Чинится не в кабинете, а порогом и запасом по этим товарам.")
        else:
            print(f"  Нашего нуля в окне нет у {len(fast) - with_zero} быстрых отмен")
            print(f"  из {len(fast)}. Значит отменяет кто-то СНАРУЖИ: вторая система")
            print("  с ключами от кабинета либо сам WB (просрочка сборки, отказ по")
            print("  правилам). Смотреть надо историю сборочного задания в кабинете.")
        print()
        print(f"  Последняя быстрая отмена: {last_day.strftime('%d.%m.%Y')}"
              + (" - ЭТО СЕГОДНЯ, продолжается." if last_day == today
                 else f" - после этого числа не повторялось (сегодня "
                      f"{today.strftime('%d.%m.%Y')})."))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

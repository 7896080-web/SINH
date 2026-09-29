"""Откуда взялась отмена (или создание) по заказу — только чтение.

Запуск:

    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_order.py 5583015069

Повод конкретный. В 1С нашёлся документ «Перемещение товаров» с комментарием
`sync REVERSE sync order_id=... wb` — то есть 1С отработала НАШЕ задание
`CANCEL_MOVEMENT`, вернув единицу со склада площадки на ЦС. Со стороны 1С
видно только это, и вопрос «а отмену-то кто заказал» оттуда ответа не имеет:
документ одинаков и когда заказ отменил продавец в кабинете, и когда кто-то
нажал кнопку у нас.

Ответ лежит в НАШЕЙ базе, но в трёх разных местах, и по одному месту вывод
получается неверный:

  * `ProcessedOrder` — когда заказ приняли и когда закрыли отменой;
  * `FtpTask` — что именно уехало в 1С, когда, каким файлом и что она ответила
    (а `is_test` отвечает на главный вопрос безопасности: боевое задание или
    симуляция со страницы «Тестирование»);
  * `AuditLog` — нажимал ли человек «Симулировать отмену» по этому номеру.

Отсюда и правило вывода в конце: отмену принёс ОПРОС площадки тогда и только
тогда, когда боевое задание есть, а следа человека нет. Для WB это вдобавок
означает `supplierStatus=cancel` — отмену ПРОДАВЦА (сборочное задание отменено
в кабинете), потому что клиентские отмены мы не реверсим вовсе: товар к тому
времени отгружен, и возврат оформляется в 1С отдельно.

Номера заказов у разных площадок совпадают, поэтому строки печатаются с
кабинетом и площадкой, а не схлопываются в одну.

Ничего не меняет и не коммитит.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import (AuditLog, FtpTask, PlatformAccount,  # noqa: E402
                        Product, ProcessedOrder, SyncAnomaly)
from app.database import SessionLocal                        # noqa: E402

# Консоль боевого сервера пишет в cp1251, а сюда печатаются ЧУЖИЕ данные:
# названия товаров из 1С и `result_detail` — текст ответа 1С. Один символ,
# которого в кодировке нет, уронил бы ВЕСЬ вывод посреди строки, то есть
# человек получил бы трассировку вместо ответа. «?» вместо символа несравнимо
# лучше; свои строки держим в пределах cp1251, это закрыто тестом.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:                                   # noqa: BLE001
    pass

# Команды, по которым 1С делает документ. Задание с любой из них — след того,
# что движение в 1С заказали МЫ, а не человек руками в самой 1С.
CANCEL = "CANCEL_MOVEMENT"

# Действия журнала, которыми человек мог завести отмену со страницы
# «Тестирование». Симуляция обязана оставлять след, и вопрос «это не мы ли
# нажали» без него отвечался бы догадкой.
HUMAN_ACTIONS = ("test_simulate_cancel", "test_simulate_order",
                 "test_simulate_confirm", "test_cleanup")


def _dt(value) -> str:
    return value.strftime("%d.%m.%Y %H:%M:%S") if value else "-"


def _gap(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} с"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин {int(seconds % 60)} с"
    return f"{seconds / 3600:.1f} ч"


def _product(db, uid: str | None) -> str:
    if not uid:
        return "товар не определён"
    p = db.query(Product).filter(Product.uid_1c == uid).first()
    if p is None:
        return f"{uid} (в номенклатуре нет)"
    parts = [p.article or "", p.size or "", p.color or "", p.name or ""]
    return " / ".join(x for x in parts if x) or uid


def main() -> int:
    if len(sys.argv) < 2:
        print("Укажите номер заказа площадки, например:")
        print("  probe_order.py 5583015069")
        return 2
    order_id = sys.argv[1].strip()

    db = SessionLocal()
    try:
        accounts = {a.id: a for a in db.query(PlatformAccount).all()}

        print(f"=== ЗАКАЗ {order_id} ===")
        print()

        orders = (db.query(ProcessedOrder)
                  .filter(ProcessedOrder.order_id == order_id)
                  .order_by(ProcessedOrder.id).all())
        print("-- что знает наша база о заказе --")
        if not orders:
            # Это не мелочь: без строки заказа опрос отмен его не увидел бы
            # вовсе, значит и задание пришло не оттуда.
            print("  записей нет: заказ этим номером мы не принимали")
        for o in orders:
            acc = accounts.get(o.account_id)
            print(f"  кабинет #{o.account_id} "
                  f"{acc.name if acc else '?'} ({acc.platform.value if acc else '?'})")
            print(f"    товар: {_product(db, o.uid_1c)}")
            print(f"    количество: {o.quantity}, статус: {o.status.value}")
            print(f"    принят:  {_dt(o.processed_at)}")
            print(f"    отменён: {_dt(o.cancelled_at)}")
            # Разрыв между приёмом и отменой печатаем ЧИСЛОМ, а не оставляем
            # человеку вычитать одно время из другого. Двадцать секунд и два
            # часа — это разные дела: за двадцать секунд человек в кабинете
            # новый заказ не найдёт, значит отменяла автоматика, и вопрос сразу
            # сужается до «чья». Глазами эта разница не бросается: оба времени
            # отличаются только секундами в одной строке.
            if o.cancelled_at and o.processed_at:
                gap = (o.cancelled_at - o.processed_at).total_seconds()
                print(f"    отменён через {_gap(gap)} после приёма")
        print()

        tasks = (db.query(FtpTask).filter(FtpTask.order_id == order_id)
                 .order_by(FtpTask.id).all())
        print("-- задания в 1С по этому номеру --")
        if not tasks:
            print("  заданий нет: движение в 1С по этому номеру мы не заказывали")
        for t in tasks:
            acc = accounts.get(t.account_id) if t.account_id else None
            mark = "  ТРЕНИРОВОЧНОЕ (в 1С не уезжает)" if t.is_test else ""
            print(f"  #{t.id} {t.command} [{t.status.value}]{mark}")
            print(f"    склад: {t.warehouse_from or '-'} -> {t.warehouse_to or '-'}, "
                  f"кол-во {t.quantity}")
            # Площадку берём у кабинета, если своя пуста: своё поле задания
            # заполняется только у возвратов (у них кабинета нет вовсе), а у
            # заказа площадка известна через кабинет. Печатать тут «-» значило
            # бы говорить «неизвестно» о том, что известно.
            platform = (t.platform.value if t.platform
                        else (acc.platform.value if acc else "-"))
            print(f"    кабинет: {acc.name if acc else '-'}, площадка: {platform}")
            print(f"    заведено: {_dt(t.created_at)}")
            print(f"    отправлено: {_dt(t.sent_at)}  файл: {t.batch_filename or '-'}")
            print(f"    ответ 1С: {t.result_status or '-'} "
                  f"{(t.result_detail or '')[:120]}")
            print(f"    закрыто: {_dt(t.completed_at)}, повторов: {t.repost_count}")
        print()

        # Журнал ищем ПОДСТРОКОЙ по номеру: страница тестирования кладёт его в
        # текст записи, а не отдельным полем.
        entries = (db.query(AuditLog)
                   .filter(AuditLog.details.contains(order_id))
                   .order_by(AuditLog.id).all())
        print("-- журнал действий (кто и что нажимал по этому номеру) --")
        if not entries:
            print("  записей нет: руками через админку этот заказ не трогали")
        for e in entries:
            print(f"  {_dt(e.created_at)}  {e.actor}  {e.action}")
            print(f"    {(e.details or '')[:160]}")
        print()

        anomalies = (db.query(SyncAnomaly)
                     .filter(SyncAnomaly.order_id == order_id)
                     .order_by(SyncAnomaly.id).all())
        if anomalies:
            print("-- аномалии --")
            for a in anomalies:
                print(f"  {_dt(a.detected_at)}  {a.reason.value} [{a.status.value}]"
                      f"{'  ТЕСТ' if a.is_test else ''}")
                print(f"    товар: {_product(db, a.uid_1c)}")
            print()

        # --- вывод, а не только выкладка чисел ---
        print("-- откуда отмена --")
        cancels = [t for t in tasks if t.command == CANCEL]
        live = [t for t in cancels if not t.is_test]
        human = [e for e in entries if e.action in HUMAN_ACTIONS]
        if not cancels:
            print("  задания на отмену у нас НЕТ. Документ реверса в 1С есть, а")
            print("  заказывали его не мы: обработку запускали в самой 1С руками.")
        elif human:
            who = ", ".join(sorted({e.actor for e in human}))
            print(f"  отмену завёл человек через админку: {who}")
            print("  (см. журнал выше; тренировочные задания в 1С не уезжают)")
        elif live:
            t = live[0]
            print(f"  отмену принёс ОПРОС площадки: задание #{t.id} заведено "
                  f"{_dt(t.created_at)},")
            print("  следа человека в журнале нет.")
            print("  Для WB это значит supplierStatus=cancel, то есть сборочное")
            print("  задание отменено НА СТОРОНЕ ПРОДАВЦА, в кабинете WB.")
            print("  Клиентские отмены мы не реверсим вовсе - товар к тому")
            print("  времени отгружен, и возврат оформляется в 1С отдельно.")
            print("  Значит смотреть надо в кабинет: кто отменил сборочное")
            print("  задание, или это сделал сам WB (просрочка сборки, отказ).")
        else:
            print("  задание на отмену есть, но ТРЕНИРОВОЧНОЕ: в 1С оно не")
            print("  уезжает вовсе. Документ в 1С пришёл не отсюда.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

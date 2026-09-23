"""Что уходит на площадки ПРЯМО СЕЙЧАС по товарам с порогом — только чтение.

Запуск:
    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\transmit_report.py

Отвечает на три РАЗНЫХ вопроса, которые легко спутать, а разница между ними и
есть предмет разбора:

  «уйдёт»    — что посчитает лестница (`transmit.explain`) в момент отправки.
               Это будущее: столько уедет при следующем событии рассылки.
  «ушло»     — что реально уехало последней отправкой (`sent_quantity` очереди).
               Это прошлое: именно оно сейчас лежит у площадки, если её никто не
               перебивал.
  «площадка» — что она ответила на вопрос сверки (`verified_quantity`). У Ozon и
               Kit читать остатки мы не умеем, там всегда пусто — и это НЕ то же
               самое, что «площадка держит ноль».

Расхождение «уйдёт» ≠ «ушло» значит, что число в базе поправили, а наружу оно
ещё не поехало: рассылка СОБЫТИЙНАЯ и сама к нему не вернётся. Расхождение
«ушло» ≠ «площадка» значит, что наше число там перетёрли.

Лестницу НЕ ПОВТОРЯЕМ, а зовём ту же, что и рассылка: повтори её тут — и отчёт
однажды покажет не то, что произойдёт.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func                              # noqa: E402
from sqlalchemy.orm import joinedload                    # noqa: E402

from app.database import SessionLocal                    # noqa: E402
from app.excel_utils import build_xlsx_bytes             # noqa: E402
from app.models import (DispatchQueueItem, PlatformAccount,  # noqa: E402
                        Product)
from app.transmit import explain                         # noqa: E402

HEADERS = ["ID_1С", "Артикул", "Размер", "Цвет", "Наименование", "Остаток ЦС",
           "Бронь", "Порог", "Трансляция", "Кабинет", "Уйдёт", "Причина нуля",
           "Ушло последней отправкой", "Когда", "Статус", "Площадка держит",
           "Проверено"]


def last_rows(db, uids: list[str]) -> dict:
    """Последняя запись очереди по каждой паре товар+кабинет.

    Порциями по тем же соображениям, что и везде: `IN (...)` — по параметру на
    элемент, а у SQLite их число ограничено.
    """
    latest = {}
    for start in range(0, len(uids), 500):
        chunk = uids[start:start + 500]
        ids = db.query(func.max(DispatchQueueItem.id)).filter(
            DispatchQueueItem.uid_1c.in_(chunk)).group_by(
            DispatchQueueItem.uid_1c, DispatchQueueItem.account_id).all()
        flat = [i for (i,) in ids if i is not None]
        for sub in range(0, len(flat), 500):
            for item in db.query(DispatchQueueItem).filter(
                    DispatchQueueItem.id.in_(flat[sub:sub + 500])).all():
                latest[(item.uid_1c, item.account_id)] = item
    return latest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="все транслируемые товары, а не только с порогом")
    parser.add_argument("--out", default=r"C:\sync_admin\что_уходит.xlsx")
    parser.add_argument("--limit", type=int, default=20000)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        accounts = {a.id: a for a in db.query(PlatformAccount).all()}
        query = db.query(Product).options(joinedload(Product.sync_settings))
        if not args.all:
            query = query.filter(Product.broadcast_offset.isnot(None))
        total = query.order_by(None).enable_eagerloads(False).count()
        products = query.order_by(Product.article).limit(args.limit).all()
        if total > len(products):
            # Молча обрезанный отчёт хуже отсутствующего: по нему сделают вывод
            # обо ВСЁМ каталоге.
            print(f"ВНИМАНИЕ: под отбор попало {total}, показано {len(products)} — "
                  f"поднимите --limit, иначе выводы будут о части каталога")
        print(f"товаров {'с порогом' if not args.all else 'всего'}: {len(products)}")

        latest = last_rows(db, [p.uid_1c for p in products])

        rows, reasons = [], Counter()
        pairs = will_go = went = 0
        # Суммы «уйдёт» и «ушло» по ВСЕМ парам сравнивать нельзя, и это не
        # придирка. В `ушло` не входят пары, куда не отправляли ни разу (их
        # просто нет в очереди), зато входят пары с выключенной трансляцией —
        # там `уйдёт` ноль, а `ушло` хранит прошлое число. Итоговая разница
        # поэтому смешивает три разных обстоятельства и ни об одном не говорит
        # правды. Считаем их порознь.
        never_sent = agrees = 0
        over = under = 0            # пар
        over_units = under_units = 0
        for product in products:
            settings = {s.account_id: s for s in product.sync_settings}
            for account_id, setting in sorted(settings.items()):
                account = accounts.get(account_id)
                if account is None or not setting.enabled:
                    continue          # неотмеченный кабинет — решение оператора
                t = explain(product, setting, account)
                item = latest.get((product.uid_1c, account_id))
                pairs += 1
                will_go += t.quantity
                sent = item.sent_quantity if item is not None else None
                if sent is None:
                    never_sent += 1
                else:
                    went += sent
                    if sent == t.quantity:
                        agrees += 1
                    elif sent > t.quantity:
                        over += 1
                        over_units += sent - t.quantity
                    else:
                        under += 1
                        under_units += t.quantity - sent
                if t.blocked:
                    reasons[t.reason] += 1
                rows.append([
                    product.uid_1c, product.article, product.size, product.color,
                    product.name, product.stock_on_hand, product.reserve,
                    product.broadcast_offset if product.broadcast_offset is not None else "",
                    "Да" if product.broadcast_enabled else "Нет",
                    f"{account.name} ({account.platform.value.upper()})",
                    t.quantity, t.reason,
                    item.sent_quantity if item is not None and item.sent_quantity is not None else "",
                    item.created_at.strftime("%Y-%m-%d %H:%M") if item is not None else "",
                    item.status.value if item is not None else "",
                    item.verified_quantity if item is not None and item.verified_quantity is not None else "",
                    item.verified_at.strftime("%Y-%m-%d %H:%M")
                    if item is not None and item.verified_at is not None else "",
                ])

        print("-" * 70)
        print(f"пар товар+кабинет (кабинет отмечен): {pairs}")
        print(f"по лестнице УЙДЁТ сейчас: {will_go} шт")
        print(f"реально УШЛО последними отправками: {went} шт "
              f"(по {pairs - never_sent} парам; ещё {never_sent} не отправляли ни разу)")
        print("-" * 70)
        print(f"сходится (уйдёт = ушло): {agrees} пар")
        # Это и есть оверселл: площадка держит БОЛЬШЕ, чем мы собираемся ей
        # отдать, и до следующего события рассылки так и будет продавать.
        print(f"площадка держит БОЛЬШЕ, чем уйдёт: {over} пар, {over_units} шт "
              f"— именно столько сейчас лишнего в продаже")
        print(f"площадка держит меньше, чем уйдёт: {under} пар, {under_units} шт "
              f"— недопродажа, не срочно")
        if reasons:
            print("-" * 70)
            print("почему уходит ноль:")
            for reason, count in reasons.most_common():
                print(f"  {count:>6}  {reason}")

        with open(args.out, "wb") as fh:
            fh.write(build_xlsx_bytes(HEADERS, rows))
        print("-" * 70)
        print(f"подробно по строкам: {args.out}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

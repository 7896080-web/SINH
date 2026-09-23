"""Что происходило с порогом у одного товара — только чтение.

Запуск:
    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_offset.py 2000932153711

Ищет по ID_1С, БАРКОДУ, артикулу целиком и по куску артикула — в таком
порядке. Баркод обязателен: артикул оператор видит на площадке, а он может не
совпасть с тем, что записано у нас (пробелы, регистр, другой разделитель
цвета), и тогда строка «не найдена» при том, что она есть.

Ничего не меняет и не коммитит. Нужен, когда порог «сам» съехал: по трём
числам на экране этого не понять — надо видеть, В КАКОМ ПОРЯДКЕ их правили и
кто. Журнал действий это помнит, а строка на странице показывает только итог.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                      # noqa: E402
from app.models import (AuditLog, Barcode, DispatchQueueItem,  # noqa: E402
                        Product, StockDateSnapshot, StockDiscrepancyLog,
                        SyncSetting)
from app.transmit import offset_from_base                  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("укажите артикул или ID_1С")
        return 1
    needle = sys.argv[1].strip()

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.uid_1c == needle).first()
        if product is None:
            link = db.query(Barcode).filter(Barcode.barcode == needle).first()
            if link is not None:
                product = db.query(Product).filter(
                    Product.uid_1c == link.uid_1c).first()
                if product is None:
                    # Баркод есть, а товара нет — привязка висит в пустоту.
                    # Само по себе находка: заказ по такому баркоду разнести
                    # не на что.
                    print(f"баркод {needle} привязан к {link.uid_1c}, "
                          f"но товара с таким ID_1С в номенклатуре НЕТ")
                    return 1
        if product is None:
            product = db.query(Product).filter(Product.article == needle).first()
        if product is None:
            product = db.query(Product).filter(
                Product.article.ilike(f"%{needle}%")).first()
        if product is None:
            print(f"не найден ни по ID_1С, ни по баркоду, ни по артикулу: {needle}")
            return 1

        print("=" * 70)
        print(f"ID_1С:            {product.uid_1c}")
        print(f"Артикул:          {product.article}")
        print(f"Название:         {product.name}")
        print("-" * 70)
        print(f"Остаток ЦС:       {product.stock_on_hand}")
        print(f"Бронь:            {product.reserve}")
        print(f"Порог:            {product.broadcast_offset}")
        print(f"Ручной остаток:   {product.transmit_override}")
        print("-" * 70)
        print(f"Дата расчёта:     {product.offset_base_date}")
        print(f"Учёт 1С на дату:  {product.offset_base_stock}")
        print(f"Факт на дату:     {product.fact_at_date}")
        print(f"Расхождение:      {product.stock_discrepancy}")
        print(f"Порог по формуле: {offset_from_base(product)}"
              "  (расхождение + бронь)")
        print("-" * 70)
        print(f"Актуализирован:   {product.recalc_done_at}")
        print(f"Покрытые кабинеты:{product.recalc_account_ids!r}")
        print(f"Трансляция:       {product.broadcast_enabled}")
        print(f"Просьба включить: {product.broadcast_requested_at}")

        if product.offset_base_date is not None:
            snap = db.query(StockDateSnapshot).filter(
                StockDateSnapshot.snapshot_date == product.offset_base_date,
            ).order_by(StockDateSnapshot.id.desc()).first()
            print("-" * 70)
            if snap is None:
                print(f"Срез 1С на {product.offset_base_date}: ЗАЯВКИ НЕТ")
            else:
                print(f"Срез 1С на {product.offset_base_date}: {snap.status} "
                      f"(строк {snap.rows_count}, заявка {snap.created_at})")

        codes = [b.barcode for b in db.query(Barcode).filter(
            Barcode.uid_1c == product.uid_1c).order_by(Barcode.id).all()]
        print("-" * 70)
        print(f"Баркоды ({len(codes)}): {', '.join(codes) if codes else 'нет'}")

        marks = db.query(SyncSetting).filter(
            SyncSetting.uid_1c == product.uid_1c, SyncSetting.enabled.is_(True)).all()
        print("-" * 70)
        print(f"Отмечено кабинетов: {[m.account_id for m in marks]}")

        print("=" * 70)
        print("ИСТОРИЯ РАСХОЖДЕНИЯ (время UTC):")
        # Отдельно от журнала действий намеренно: массовые пути (кнопки отбора,
        # импорт Excel) в журнал построчно не пишут вовсе, и по конкретной строке
        # там следа нет — а расхождение уходит в порог и дальше на площадки.
        gaps = db.query(StockDiscrepancyLog).filter(
            StockDiscrepancyLog.uid_1c == product.uid_1c,
        ).order_by(StockDiscrepancyLog.id).all()
        for g in gaps:
            print(f"  {g.created_at}  {str(g.old_value):>6} -> {str(g.new_value):>6}  "
                  f"{g.source.value:9} {g.username or '-':12} "
                  f"дата {g.base_date} учёт {g.base_stock} факт {g.fact}"
                  + (f"  {g.note}" if g.note else ""))
        if not gaps:
            print("  пусто — расхождение не измеряли ни разу")

        print("=" * 70)
        print("ЖУРНАЛ ПО ЭТОЙ СТРОКЕ (последние 40, время UTC):")
        rows = db.query(AuditLog).filter(
            AuditLog.details.contains(product.uid_1c),
        ).order_by(AuditLog.id.desc()).limit(40).all()
        for r in reversed(rows):
            print(f"  {r.created_at}  {r.actor:12} {r.action:28} {r.details}")
        if not rows:
            print("  пусто — строку правили только массово или файлом")

        # Очередь рассылки — единственный след ПОРОГА во времени.
        #
        # Сам порог нигде не версионируется: страница показывает итог, а журнал
        # действий записывает только правку руками. Зато `sent_quantity` — это
        # «сколько ушло на площадку НА САМОМ ДЕЛЕ», посчитанное всей лестницей.
        # Зная остаток на тот момент (`quantity` — он пишется при постановке),
        # порог считается обратно: порог = остаток − ушло. Приблизительно —
        # сверху могли сработать порог кабинета и пауза, — но для вопроса «съехал
        # ли порог и когда» этого хватает.
        print("-" * 70)
        print("ОЧЕРЕДЬ РАССЫЛКИ (последние 25; «порог≈» = остаток − ушло):")
        queue = db.query(DispatchQueueItem).filter(
            DispatchQueueItem.uid_1c == product.uid_1c,
        ).order_by(DispatchQueueItem.id.desc()).limit(25).all()
        for q in reversed(queue):
            guess = ("—" if q.sent_quantity is None
                     else str((q.quantity or 0) - q.sent_quantity))
            print(f"  {q.created_at}  каб.{q.account_id}  остаток {q.quantity}"
                  f"  ушло {q.sent_quantity}  порог≈{guess}"
                  f"  {q.status.value if q.status else ''}  {q.reason}")
        if not queue:
            print("  пусто")

        print("-" * 70)
        print("МАССОВЫЕ ДЕЙСТВИЯ И РАСЧЁТЫ (последние 30):")
        bulk = db.query(AuditLog).filter(
            AuditLog.action.in_(["products_bulk", "products_bulk_import_excel",
                                 "recalc_started"]),
        ).order_by(AuditLog.id.desc()).limit(30).all()
        for r in reversed(bulk):
            print(f"  {r.created_at}  {r.actor:12} {r.action:28} {r.details}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

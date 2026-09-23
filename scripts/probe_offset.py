"""Что происходило с порогом у одного товара — только чтение.

Запуск:
    C:\\sync_admin\\.venv\\Scripts\\python.exe C:\\sync_admin\\scripts\\probe_offset.py "27643 LACIVERT/RED L"

Ничего не меняет и не коммитит. Нужен, когда порог «сам» съехал: по трём
числам на экране этого не понять — надо видеть, В КАКОМ ПОРЯДКЕ их правили и
кто. Журнал действий это помнит, а строка на странице показывает только итог.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                      # noqa: E402
from app.models import (AuditLog, Product, StockDateSnapshot,  # noqa: E402
                        SyncSetting)
from app.transmit import offset_from_base                  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("укажите артикул или ID_1С")
        return 1
    needle = sys.argv[1].strip()

    db = SessionLocal()
    try:
        product = (db.query(Product).filter(Product.uid_1c == needle).first()
                   or db.query(Product).filter(Product.article == needle).first()
                   or db.query(Product).filter(Product.article.ilike(f"%{needle}%")).first())
        if product is None:
            print(f"не найден: {needle}")
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
        print(f"Порог к удержанию:{product.offset_pinned}")
        print(f"Порог по формуле: {offset_from_base(product)}")
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

        marks = db.query(SyncSetting).filter(
            SyncSetting.uid_1c == product.uid_1c, SyncSetting.enabled.is_(True)).all()
        print("-" * 70)
        print(f"Отмечено кабинетов: {[m.account_id for m in marks]}")

        print("=" * 70)
        print("ЖУРНАЛ ПО ЭТОЙ СТРОКЕ (последние 40, время UTC):")
        rows = db.query(AuditLog).filter(
            AuditLog.details.contains(product.uid_1c),
        ).order_by(AuditLog.id.desc()).limit(40).all()
        for r in reversed(rows):
            print(f"  {r.created_at}  {r.actor:12} {r.action:28} {r.details}")
        if not rows:
            print("  пусто — строку правили только массово или файлом")

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

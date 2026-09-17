"""Порог трансляции от даты: остаток ЦС на дату и его подстановка в товары.

Оператор задаёт дату, система спрашивает у 1С остаток ЦС на неё, оператор
вписывает, сколько лежало на складе на самом деле, — и из трёх чисел выводится
порог (формула в `app/transmit.py`).

Узкое место здесь одно: **1С отвечает не сразу.** Обработка запускается своим
расписанием, на боевом это до десяти минут. Значит момент «оператор задал дату»
и момент «стало известно, сколько было» разнесены во времени, и между ними
товар живёт с пустым `offset_base_stock`. Подставить остаток и пересчитать порог
обязан тот, кто принимает ответ 1С, — иначе оператор задал бы дату, ответ
пришёл бы, и ничего не произошло до следующего касания строки руками.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session, joinedload

from app.models import Product, StockDateRow, StockDateSnapshot, StockDateStatus
from app.transmit import enqueue_full_resend, recompute_offset


def done_snapshot(db: Session, snapshot_date: date) -> StockDateSnapshot | None:
    """Последний ЗАКРЫТЫЙ снимок на эту дату.

    Именно последний: если выгрузку на одно и то же число заказывали дважды,
    свежая версия вернее — между ними в 1С могли провести документы задним
    числом, и старый ответ уже не описывает эту дату.
    """
    return db.query(StockDateSnapshot).filter(
        StockDateSnapshot.snapshot_date == snapshot_date,
        StockDateSnapshot.status == StockDateStatus.done,
    ).order_by(StockDateSnapshot.id.desc()).first()


def stock_at_date(db: Session, uid_1c: str, snapshot_date: date) -> int | None:
    """Сколько 1С показала по этому товару на дату.

    `None` — готового снимка нет: выгрузку либо не заказывали, либо ещё ждём.
    `0` — снимок есть, а товара в нём нет: на эту дату его на складе не было.
    Разница принципиальная: в первом случае порог считать не из чего, во втором
    он честно равен брони.
    """
    snapshot = done_snapshot(db, snapshot_date)
    if snapshot is None:
        return None
    row = db.query(StockDateRow).filter(
        StockDateRow.snapshot_id == snapshot.id,
        StockDateRow.uid_1c == uid_1c,
    ).order_by(StockDateRow.id.desc()).first()
    # Последняя строка, а не сумма: так же разбирает товар с повторами часовая
    # выгрузка (`job_reconciliation`), и расходиться этим двум путям нельзя.
    return row.quantity if row is not None else 0


def set_base_date(db: Session, product: Product, snapshot_date: date | None) -> bool:
    """Задать дату расчёта товару. True — порог изменился.

    Снимок на дату уже есть — подставляем остаток сразу. Нет — оставляем пустым,
    товар попадёт в ожидание, и `fill_waiting_products` доделает за нас, когда
    придёт ответ 1С.

    Пустая дата снимает расчёт целиком: уходит и факт, потому что он привязан
    именно к этой дате, а порог остаётся последним посчитанным. Стирать порог
    здесь нельзя — это молча вернуло бы на площадки полный остаток.
    """
    product.offset_base_date = snapshot_date
    if snapshot_date is None:
        product.offset_base_stock = None
        product.fact_at_date = None
        return False
    product.offset_base_stock = stock_at_date(db, product.uid_1c, snapshot_date)
    return recompute_offset(product)


def fill_waiting_products(db: Session, snapshot: StockDateSnapshot) -> dict:
    """1С ответила — подставить остаток всем, кто ждал этой даты, и пересчитать.

    Берём только товары с пустым `offset_base_stock`: у кого он уже заполнен,
    тот получил своё число раньше, и переписывать его свежим снимком значило бы
    менять порог под оператором без его ведома.

    Изменившийся порог ставится в очередь рассылки — иначе новое число осталось
    бы только на экране, а на площадках висело бы старое.
    """
    stats = {"filled": 0, "offsets_changed": 0, "queued": 0}

    waiting = db.query(Product).options(joinedload(Product.sync_settings)).filter(
        Product.offset_base_date == snapshot.snapshot_date,
        Product.offset_base_stock.is_(None),
    ).all()
    if not waiting:
        return stats

    # Одним запросом, а не по товару: в снимке бывают все 152 тысячи позиций,
    # и отдельный SELECT на каждую превратил бы приём файла в часовую работу.
    by_uid: dict[str, int] = {}
    for uid, quantity in db.query(StockDateRow.uid_1c, StockDateRow.quantity).filter(
            StockDateRow.snapshot_id == snapshot.id).all():
        by_uid[uid] = quantity        # повтор — последняя строка, как и в stock_at_date

    for product in waiting:
        product.offset_base_stock = by_uid.get(product.uid_1c, 0)
        stats["filled"] += 1
        if recompute_offset(product):
            stats["offsets_changed"] += 1
            for setting in product.sync_settings:
                if setting.enabled:
                    enqueue_full_resend(db, product.uid_1c, setting.account_id,
                                        reason="offset_base_filled")
                    stats["queued"] += 1

    return stats

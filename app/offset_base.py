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
from app.broadcast_gate import apply_pending_broadcast
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


def open_request(db: Session, snapshot_date: date) -> StockDateSnapshot | None:
    """Незакрытая заявка на эту дату.

    Вторую заводить нельзя: 1С называет файл ответа по дате, один ответ закрыл
    бы только одну заявку, а вторая висела бы до таймаута и выглядела бы как
    сбой канала.
    """
    return db.query(StockDateSnapshot).filter(
        StockDateSnapshot.snapshot_date == snapshot_date,
        StockDateSnapshot.status.in_([StockDateStatus.pending, StockDateStatus.sent]),
    ).order_by(StockDateSnapshot.id.asc()).first()


def ensure_snapshot_requested(db: Session, snapshot_date: date, username: str) -> bool:
    """Заказать у 1С выгрузку на дату, если её ещё не заказывали. True — заказали.

    Без этого связка получалась дырявой: оператор задаёт дату на странице
    товаров, а выгрузку на неё должен не забыть попросить руками на другой
    странице. Забудет — строка навсегда останется в «ждём выгрузку 1С», и
    виноватым будет выглядеть расчёт.
    """
    if done_snapshot(db, snapshot_date) is not None:
        return False                       # ответ уже есть, спрашивать нечего
    if open_request(db, snapshot_date) is not None:
        return False                       # уже спросили, ждём
    db.add(StockDateSnapshot(snapshot_date=snapshot_date, requested_by=username))
    return True


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


def stock_lookup(db: Session, snapshot_date: date):
    """Готовая функция «uid → остаток на дату», со снимком, прочитанным ОДИН раз.

    Для массовых путей: простановка даты сотне отмеченных строк и импорт Excel,
    где строк бывает до пятидесяти тысяч. `stock_at_date` ходит в базу дважды на
    каждый товар — на таком объёме это сто тысяч запросов и минуты ожидания.

    Возвращает функцию, отдающую `None`, если готового снимка на дату нет вовсе.
    """
    snapshot = done_snapshot(db, snapshot_date)
    if snapshot is None:
        return lambda uid: None
    by_uid: dict[str, int] = {}
    for uid, quantity in db.query(StockDateRow.uid_1c, StockDateRow.quantity).filter(
            StockDateRow.snapshot_id == snapshot.id).all():
        by_uid[uid] = quantity        # повтор — последняя строка, как и в stock_at_date
    return lambda uid: by_uid.get(uid, 0)


def set_base_date(db: Session, product: Product, snapshot_date: date | None,
                  lookup=None) -> bool:
    """Задать дату расчёта товару. True — порог изменился.

    Снимок на дату уже есть — подставляем остаток сразу. Нет — оставляем пустым,
    товар попадёт в ожидание, и `fill_waiting_products` доделает за нас, когда
    придёт ответ 1С.

    **Факт при смене даты стирается.** Он всегда «факт НА ДАТУ»: пересчитанное
    07.08 количество ничего не говорит о складе на 01.09. Оставить его значило бы
    посчитать порог по данным с другого числа и отправить на площадки заведомо
    неверный остаток — причём молча. Пусть лучше порог временно сведётся к брони
    (это поведение сегодняшнего автоматического режима), а оператор впишет факт
    заново.

    Пустая дата снимает расчёт целиком. Порог при этом остаётся последним
    посчитанным: стирать его здесь нельзя — это молча вернуло бы на площадки
    полный остаток.

    `lookup` — готовая функция поиска остатка (см. `stock_lookup`) для массовых
    путей; без неё каждый товар идёт в базу сам.
    """
    changed_day = snapshot_date != product.offset_base_date
    product.offset_base_date = snapshot_date
    if changed_day:
        product.fact_at_date = None
    if snapshot_date is None:
        product.offset_base_stock = None
        product.fact_at_date = None
        return False
    product.offset_base_stock = (lookup(product.uid_1c) if lookup is not None
                                 else stock_at_date(db, product.uid_1c, snapshot_date))
    return recompute_offset(product)


def fill_waiting_products(db: Session, snapshot: StockDateSnapshot) -> dict:
    """1С ответила — подставить остаток всем, кто ждал этой даты, и пересчитать.

    Берём только товары с пустым `offset_base_stock`: у кого он уже заполнен,
    тот получил своё число раньше, и переписывать его свежим снимком значило бы
    менять порог под оператором без его ведома.

    Изменившийся порог ставится в очередь рассылки — иначе новое число осталось
    бы только на экране, а на площадках висело бы старое.
    """
    stats = {"filled": 0, "offsets_changed": 0, "queued": 0, "broadcast_on": 0}

    # ОБЯЗАТЕЛЬНЫЙ flush. Приложение работает с `autoflush=False`
    # (`app/database.py`), а вызывают нас сразу после того, как строки ответа
    # добавлены в сессию и снимку проставлен статус `done`, — но ещё НЕ
    # отправлены в базу. Без flush запрос ниже не увидит ни строк, ни статуса:
    # снимок «не найден», остаток на дату у всех выходит нулём, и порог у всего
    # каталога считается по несуществующим данным. Поймано только живым прогоном:
    # в тестах сессия фикстуры флашится сама и дефект не проявлялся.
    db.flush()

    # Строки берём ПО ЭТОМУ снимку, а не ищем свежий по дате: мы его уже держим
    # в руках, лишний поиск только добавляет способ ошибиться.
    by_uid: dict[str, int] = {}
    for uid, quantity in db.query(StockDateRow.uid_1c, StockDateRow.quantity).filter(
            StockDateRow.snapshot_id == snapshot.id).all():
        by_uid[uid] = quantity        # повтор — последняя строка, как в stock_at_date

    # ПАЧКАМИ, а не всё сразу. Оператор вправе проставить дату всему каталогу —
    # это 152 тысячи товаров, и каждый тянет за собой свои настройки кабинетов.
    # Загрузить их одним списком значило бы держать под миллион объектов в памяти
    # воркера, который в это же время обслуживает опрос заказов и рассылку.
    CHUNK = 1000
    while True:
        waiting = db.query(Product).options(joinedload(Product.sync_settings)).filter(
            Product.offset_base_date == snapshot.snapshot_date,
            Product.offset_base_stock.is_(None),
        ).limit(CHUNK).all()
        # Смещения нет намеренно: каждый обработанный товар получает непустой
        # offset_base_stock и из выборки выпадает сам. Со смещением пачка
        # «перепрыгивала» бы через необработанные строки.
        if not waiting:
            break

        for product in waiting:
            # Товара нет в ответе — значит на эту дату его на складе не было.
            product.offset_base_stock = by_uid.get(product.uid_1c, 0)
            stats["filled"] += 1
            offset_changed = recompute_offset(product)
            if offset_changed:
                stats["offsets_changed"] += 1
            # Второе место, где могут открыться ворота трансляции. Порядок
            # прихода не определён: расчёт по товару мог закончиться РАНЬШЕ, чем
            # 1С ответила на заявку о дате, и тогда в тот момент строка стояла
            # «ждём 1С» и включиться не могла. Спроси мы только в расчёте —
            # просьба из файла осталась бы висеть навсегда.
            turned_on = apply_pending_broadcast(product)
            if turned_on:
                stats["broadcast_on"] += 1
            if offset_changed or turned_on:
                for setting in product.sync_settings:
                    if setting.enabled:
                        enqueue_full_resend(db, product.uid_1c, setting.account_id,
                                            reason="offset_base_filled")
                        stats["queued"] += 1
        db.commit()

    return stats

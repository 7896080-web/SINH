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

from app.models import (DiscrepancySource, Product, StockDateRow, StockDateSnapshot,
                        StockDateStatus, StockDiscrepancyLog)
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


def set_discrepancy(db: Session, product: Product, value: int | None, *,
                    source: DiscrepancySource, username: str = "",
                    note: str = "") -> bool:
    """Записать расхождение учёта со складом и запомнить это в истории.

    True — значение изменилось. Единственный, кто пишет `stock_discrepancy`:
    число уходит в порог, а порог — на площадки, и появиться оно должно только
    вместе со строкой истории. Иначе через месяц «почему здесь 11» останется без
    ответа — ровно тот случай, из-за которого 23.09 подобранный факт приняли за
    измерение и «поправили».

    Значение не изменилось — истории не пишем: правка брони, повторный импорт
    того же файла и второе нажатие кнопки идут пачками, и запись «было 11, стало
    11» на каждую из них утопила бы настоящие правки.
    """
    if product.stock_discrepancy == value:
        return False
    db.add(StockDiscrepancyLog(
        uid_1c=product.uid_1c,
        old_value=product.stock_discrepancy,
        new_value=value,
        source=source,
        username=(username or None),
        # Контекст измерения — чтобы число можно было перепроверить: из какого
        # учёта и какого пересчёта оно вышло.
        base_date=product.offset_base_date,
        base_stock=product.offset_base_stock,
        fact=product.fact_at_date,
        note=(note or None),
    ))
    product.stock_discrepancy = value
    return True


def apply_fact(db: Session, product: Product, fact: int | None, *,
               username: str = "") -> bool:
    """Вписать факт на дату и, ЕСЛИ ОН ОТЛИЧАЕТСЯ ОТ УЧЁТА, записать расхождение.

    True — расхождение изменилось.

    Факт — это акт измерения склада: «на такое-то число там лежало столько-то».
    Само по себе оно не является тем, что уходит в порог; в порог уходит
    РАЗНИЦА с учётом 1С, и она-то и хранится на товаре.

    Условие «отличается от учёта» — главное здесь, и оно прямо из требования
    оператора. Факт, РАВНЫЙ учёту, значит «мне нечего возразить цифре 1С», а не
    «я пересчитал склад и он сошёлся». Разница между ними видна ровно в одном
    месте, и оно же самое дорогое: строка, у которой расхождение уже измерено, а
    дату сдвинули назад, стоит с пустым фактом на новую дату. Человек, не зная
    склада на то число, вписывает учётное — и прежним поведением это стирало бы
    расхождение, схлопывая порог до брони и отправляя наружу завышенный остаток.
    Сразу по всему отбору, если правка массовая. Поэтому такой ввод расхождения
    НЕ ТРОГАЕТ.

    Сказать «склад сошёлся с учётом» по-прежнему можно — но явно, поставив
    расхождение 0 (`set_discrepancy`, колонка «Расхождение» в файле и в строке).
    Ноль — утверждение, и делается оно вслух.
    """
    product.fact_at_date = fact
    if fact is None or product.offset_base_stock is None:
        # Считать не из чего: либо факт сняли, либо 1С ещё не ответила на дату.
        # Расхождение при этом не трогаем — оно свойство ТОВАРА, а не этой даты.
        return False
    measured = product.offset_base_stock - fact
    if measured == 0:
        return False
    return set_discrepancy(db, product, measured, source=DiscrepancySource.fact,
                           username=username)


def apply_offset(db: Session, product: Product, value: int, *,
                 username: str = "") -> bool:
    """Задать порог НАПРЯМУЮ. True — порог изменился.

    Порог — производная величина: `порог = расхождение + бронь`. Записать его
    мимо расхождения было бы миной замедленного действия — первая же правка
    брони пересчитала бы порог по формуле и вернула прежний, молча и через
    неделю. Поэтому здесь считается ОБРАТНАЯ величина и пишется она:

        расхождение = порог − бронь

    У строки без даты расчёта порог всегда был просто числом, и здесь ничего не
    меняется: расхождение там не измеряли, формуле неоткуда взяться.

    Отказа у этой функции больше нет. Прежняя версия подбирала под порог ФАКТ и
    отвергала правку, при которой факт вышел бы отрицательным. Подбирать больше
    нечего: расхождение хранится само, а отрицательным оно бывает на законных
    основаниях — «на складе больше, чем знает 1С» (на бою таких товаров 53).
    """
    if product.offset_base_date is None:
        changed = product.broadcast_offset != value
        product.broadcast_offset = value
        product.transmit_override = None
        return changed
    set_discrepancy(db, product, value - (product.reserve or 0),
                    source=DiscrepancySource.offset, username=username)
    return recompute_offset(product)


def clear_offset(db: Session, product: Product, *, username: str = "") -> None:
    """«Сбросить порог»: снять порог, расхождение и все исходные числа.

    Расхождение снимается ОБЯЗАТЕЛЬНО, и это не уборка ради опрятности. Порог
    выводится из него, значит оставь мы расхождение — ближайший пересчёт (правка
    брони, ответ 1С, любое касание строки) вернул бы порог на место. Кнопка
    выглядела бы сломанной, а на площадки продолжало бы уходить прежнее число.

    Это единственный путь, снимающий расхождение целиком (в NULL — «не
    измеряли»). Сказать «склад сошёлся» — другое действие: расхождение 0.
    """
    product.broadcast_offset = None
    set_discrepancy(db, product, None, source=DiscrepancySource.reset,
                    username=username, note="сброс порога")
    # Снимаем и исходные три величины. Иначе «сброшен» было бы неправдой: дата
    # осталась бы на месте, и первая же правка брони вернула бы порог обратно —
    # оператор решил бы, что кнопка не работает.
    product.offset_base_date = None
    product.offset_base_stock = None
    product.fact_at_date = None


def offset_is_established(product: Product | None) -> bool:
    """Расхождение измерено — то есть за порогом стоит работа человека.

    Спрашивает об этом одна кнопка, «Записать остаток ЦС на дату»: она ставит
    факт равным учёту, и по строке с измеренным расхождением это ничего не
    объявляет (`apply_fact` такой ввод намеренно пропускает), зато оставляет в
    ячейке число, противоречащее расхождению рядом. Молчаливое противоречие в
    строке — ровно то, из-за чего 23.09 подобранный факт «поправили» на учётный.

    Ноль расхождением не считается: «склад сошёлся с учётом» — это и есть то,
    что кнопка утверждает, спорить тут не с чем.

    Хвост условия — про строки, настроенные ДО появления колонки расхождения: у
    них порог стоит, а расхождение NULL, и схлопывать его кнопкой нельзя ровно
    так же. Порог, равный броне, работой человека НЕ считается (формула даёт его
    сама), иначе кнопка перестала бы работать почти везде.
    """
    if product is None:
        return False
    if product.stock_discrepancy:
        return True
    if product.stock_discrepancy == 0 or product.broadcast_offset is None:
        return False
    return (product.fact_at_date is not None
            or product.broadcast_offset != (product.reserve or 0))


def set_base_date(db: Session, product: Product, snapshot_date: date | None,
                  lookup=None, keep_offset: bool = True) -> bool:
    """Задать дату расчёта товару. True — порог изменился.

    Снимок на дату уже есть — подставляем остаток сразу. Нет — оставляем пустым,
    товар попадёт в ожидание, и `fill_waiting_products` доделает за нас, когда
    придёт ответ 1С.

    **Факт при смене даты стирается.** Он всегда «факт НА ДАТУ»: пересчитанное
    07.08 количество ничего не говорит о складе на 01.09. Оставить его значило бы
    показывать измерение с другого числа как измерение на это.

    **А расхождение — НЕТ.** Оно свойство товара, а не даты: учёт врёт на одну и
    ту же величину, пока склад не пересчитали заново. Поэтому порог смену даты
    переживает сам собой, и подбирать под него факт больше не нужно — раньше это
    делал `pin_offset`, и подобранное им число оператор видел как измерение,
    которого не делал. 23.09 он его и «поправил» на учётное, схлопнув пороги у
    62 товаров. Параметр `keep_offset` остался ради вызывающих и ни на что не
    влияет: удерживать нечего, ничего и не теряется.

    Пустая дата снимает расчёт целиком. Порог при этом остаётся последним
    посчитанным: стирать его здесь нельзя — это молча вернуло бы на площадки
    полный остаток. Снять осознанно — «Сбросить порог» (`clear_offset`).

    `lookup` — готовая функция поиска остатка (см. `stock_lookup`) для массовых
    путей; без неё каждый товар идёт в базу сам.
    """
    changed_day = snapshot_date != product.offset_base_date
    product.offset_base_date = snapshot_date
    if changed_day:
        product.fact_at_date = None
        # Отметку «актуализирован» снимаем вместе с датой — расчёт проводил
        # заказы ОТ ПРЕЖНЕЙ даты, и к новому периоду его вывод не относится.
        # Без этого строка возвращалась в «актуализирован», как только человек
        # вводил новый факт: проверка факта в `calc_status` стоит раньше
        # проверки расчёта, поэтому смена даты выглядела как «нужен факт», а не
        # «нужен расчёт». Сдвиг даты НАЗАД при этом опаснее всего: заказы за
        # добавившийся кусок периода не проведены, остаток ЦС завышен ровно на
        # них, и включённая трансляция отправит наружу больше, чем есть.
        # Пустая СТРОКА, а не NULL, — как при переподвязке баркода: покрытие
        # аннулировано, но отслеживать его мы продолжаем, иначе ступень 2
        # лестницы перестанет срабатывать вовсе (см. `transmit.coverage_is_tracked`).
        if product.recalc_done_at is not None:
            # Только если расчёт БЫЛ. У товара без расчёта `recalc_account_ids`
            # пуст (NULL), и выставить здесь пустую СТРОКУ значило бы включить
            # ступень 2 лестницы там, где она обязана молчать: «покрытие ведём,
            # покрыт никто» — и на отмеченный кабинет ушёл бы ноль. Для товаров,
            # которым трансляцию включили до появления расчёта, это было бы
            # обнулением живых карточек по всему каталогу. Поймано тестом
            # `test_changed_threshold_goes_into_the_dispatch_queue`.
            product.recalc_done_at = None
            product.recalc_account_ids = ""
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
    # Счётчиков «порог удержан / удержать не вышло» здесь больше нет, и это не
    # потеря сигнала, а исчезновение самого события: до появления хранимого
    # расхождения под сохранённый порог подбирался ФАКТ, и подбор мог выйти
    # отрицательным. Теперь порог держится на расхождении и смену даты переживает
    # сам — подбирать нечего, терять нечего. Вместе со счётчиком убраны его
    # читатели: строка heartbeat у `ftp_receive` и находка `offset_not_kept`.
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
            # Пересчёт зовём ВСЕГДА, даже когда он ничего не изменит (порог
            # держится на хранимом расхождении и смену даты переживает сам).
            # Пропустить его вместе с остатком тела цикла (первый вариант этой
            # правки так и делал) было
            # бы дефектом: ниже открываются ВОРОТА ТРАНСЛЯЦИИ, и отложенная
            # просьба «включить, как только расчёт закончится» у таких строк
            # осталась бы висеть навсегда.
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
                        # Считаем ТОЛЬКО реально поставленные записи. Возвращаемое
                        # значение у `enqueue_full_resend` есть ровно для этого:
                        # гейты (трансляция выключена, кабинет вне расчёта) режут
                        # запись молча, и безусловный счётчик писал в журнал
                        # «в очередь рассылки 2500» при нуле поставленных — тот
                        # самый случай «всё зелено, а наружу не ушло ничего».
                        if enqueue_full_resend(db, product.uid_1c, setting.account_id,
                                               reason="offset_base_filled"):
                            stats["queued"] += 1
        db.commit()

    return stats

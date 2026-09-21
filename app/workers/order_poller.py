import logging
from datetime import datetime, timedelta
from app.timeutils import now_utc

from sqlalchemy.orm import Session

from app.models import (
    Product, SyncSetting, SyncAnomaly, AnomalyReason, ProcessedOrder,
    OrderProcessStatus, DispatchQueueItem, FtpTask, Barcode, PlatformAccount,
)
from app.transmit import covered_accounts, coverage_is_tracked
from app.workers.matching import resolve_barcode
from app.workers.platform_clients.base import PlatformClient, PlatformOrder

logger = logging.getLogger("sync_worker")


# Префикс синтетических заказов со страницы «Тестирование». У ProcessedOrder нет
# флага is_test — симулированный заказ отличается только этим префиксом, и живой
# опрос ОБЯЗАН исключать такие заказы: их идентификаторы не существуют на площадке
# (клиент WB приводит номер к целому и падает, после пяти падений предохранитель
# гасит боевой кабинет).
TEST_ORDER_PREFIX = "TEST-"


# Сколько дней после приёма заказ ещё считается «живым» и опрашивается на
# отмену/подтверждение.
#
# Окно обязано быть конечным. Заказы WB и Ozon НЕ ЗАКРЫВАЮТСЯ НИКОГДА: детект
# подтверждения у этих площадок не реализован (`base.get_confirmed_orders`
# возвращает пустой список), поэтому заказ навсегда остаётся `processed`. Без
# окна список открытых заказов рос бы со скоростью продаж, а его идентификаторы
# каждые две минуты уходят в запрос отмен — то есть в предел площадки на размер
# запроса мы упёрлись бы рано или поздно обязательно.
#
# Тридцать дней с запасом: отмена неподтверждённого заказа приходит в первые дни,
# у месячного отменять уже нечего. Цена ошибки в эту сторону безопасна: пропущенная
# отмена означает, что мы не вернули единицу на склад, то есть остаток занижен —
# недоотправка, а не оверселл.
#
# ОКНО СЧИТАЕТСЯ ОТ МОМЕНТА ПРИЁМА ЗАКАЗА У НАС (`processed_at`), А НЕ ОТ ДАТЫ
# ЗАКАЗА НА ПЛОЩАДКЕ. Это принципиально для старта задним числом: расчёт с
# базовой датой за 90 дней поднимает заказы трёхмесячной давности, но строка
# `ProcessedOrder` заводится СЕЙЧАС, и отмены по ней отслеживаются ещё 30 дней
# от проведения. Реальная дата заказа уходит в `movement_date` — ею датируется
# документ в 1С, и только туда. Если `processed_at` когда-нибудь начнут
# заполнять `order_date`, весь бэкфилл окажется за окном В МОМЕНТ СОЗДАНИЯ и
# отмены по нему перестанут отслеживаться вовсе; тест
# `test_a_backfilled_old_order_is_still_inside_the_window` такую правку роняет.
#
# Идемпотентности расчёта окно не касается вовсе: повторный прогон ищет заказ
# по паре (кабинет, номер) среди ВСЕХ строк без ограничения по возрасту, так что
# сдвиг базовой даты назад не создаёт вторых перемещений.
OPEN_ORDER_WINDOW_DAYS = 30


def _real_open_orders(db: Session, account: PlatformAccount) -> list[ProcessedOrder]:
    """Принятые, но ещё не закрытые заказы кабинета — только НАСТОЯЩИЕ и только
    за последние `OPEN_ORDER_WINDOW_DAYS` дней.

    Их идентификаторы уходят прямо в API площадки, поэтому синтетика исключается,
    а список ограничен по возрасту (см. комментарий у константы)."""
    cutoff = now_utc() - timedelta(days=OPEN_ORDER_WINDOW_DAYS)
    return db.query(ProcessedOrder).filter(
        ProcessedOrder.account_id == account.id,
        ProcessedOrder.status == OrderProcessStatus.processed,
        ProcessedOrder.order_id.notlike(f"{TEST_ORDER_PREFIX}%"),
        ProcessedOrder.processed_at >= cutoff,
    ).all()


def _representative_barcode(db: Session, uid_1c: str) -> str | None:
    """Для FTP-задания в 1С нужен любой настоящий физический баркод товара —
    после того как подтвердилось, что Kit тоже отдаёт реальный баркод
    (поле barcode у Variant, не product_variant_id), все источники
    равноценны, различать их по площадке больше не нужно."""
    row = db.query(Barcode).filter(Barcode.uid_1c == uid_1c).first()
    return row.barcode if row else None


def _enqueue_dispatch_to_others(db: Session, uid_1c: str, source_account_id: int, new_quantity: int,
                                 reason: str, is_test: bool = False):
    """Раздел 7: рассылаем новый остаток на все КАБИНЕТЫ, где включена
    синхронизация, КРОМЕ того самого кабинета, откуда пришло событие — он
    уже сам скорректировал «доступно к продаже» у себя. Другие кабинеты той
    же площадки (например, другой ИП на WB) — это НЕ источник, им тоже шлём:
    все кабинеты продают из одного и того же физического остатка ЦС.

    is_test=True (со страницы тестирования) помечает запись так, что
    dispatch.py заведомо её не отправит — см. комментарий у DispatchQueueItem."""
    # Трансляция товара выключена — не ставим в очередь НИЧЕГО. Иначе рассылка
    # посчитает по нему ноль и отправит этот ноль на площадку: для товара, по
    # которому мы ещё не транслировали, это обнуление живой карточки, а не отзыв
    # остатка. Особенно важно при актуализации задним числом: там заказы
    # проводятся до включения трансляции, и каждый проведённый заказ отправлял бы
    # ноль. Осознанный отзыв идёт отдельной функцией enqueue_withdrawal.
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    if product is not None and not product.broadcast_enabled:
        return []

    settings = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.enabled.is_(True),
    ).all()

    # Кабинеты, которых расчёт не касался, пропускаем по той же причине: по ним
    # рассылка посчитает ноль и отправит его на живую карточку.
    covered = covered_accounts(product)
    gate_by_recalc = coverage_is_tracked(product)

    targets = []
    for setting in settings:
        if setting.account_id == source_account_id:
            continue
        if gate_by_recalc and setting.account_id not in covered:
            continue
        db.add(DispatchQueueItem(
            uid_1c=uid_1c, account_id=setting.account_id, quantity=new_quantity, reason=reason,
            is_test=is_test,
        ))
        targets.append(setting.account_id)
    return targets


def open_test_out(db: Session, uid_1c: str) -> int:
    """Сколько единиц «забрали» симулированные заказы, которые ещё не отменены и
    не очищены. Нужно, чтобы показать в тесте правдоподобный остаток, НЕ трогая
    боевой `stock_on_hand`: симуляция не должна менять то, что уходит на реальные
    площадки (см. process_new_order)."""
    rows = db.query(ProcessedOrder).filter(
        ProcessedOrder.uid_1c == uid_1c,
        ProcessedOrder.order_id.like(f"{TEST_ORDER_PREFIX}%"),
        ProcessedOrder.status.in_([OrderProcessStatus.processed, OrderProcessStatus.confirmed]),
    ).all()
    return sum(r.quantity or 0 for r in rows)


def process_new_order(db: Session, order: PlatformOrder, account: PlatformAccount, warehouse_pending: str,
                       is_test: bool = False, order_date=None, respect_enabled: bool = True) -> dict:
    """Обрабатывает ОДИН заказ — сердце приёма заказов (раздел 4/5
    спецификации). Используется и живым опросом (poll_new_orders, заказ из
    реального API), и страницей тестирования (testing.py, синтетический
    заказ) — один и тот же код гарантирует, что тест проверяет ровно то
    поведение, которое сработает в бою, а не отдельную параллельную ветку.

    is_test=True — единственное, что отличает тестовый прогон: все побочные
    записи (очередь рассылки, задание в 1С, аномалия) помечаются флагом,
    который явно исключает их из реальной отправки на площадку/в 1С
    (dispatch.py, ftp_channel.py) и из общей страницы аномалий.

    Возвращает подробный результат — нужен странице тестирования для показа
    оператору, что именно произошло на каждом шаге."""

    result = {"status": None, "uid_1c": None, "new_stock": None, "dispatched_to": [], "ftp_task_id": None,
              "detail": "", "anomaly_created": False}

    existing = db.query(ProcessedOrder).filter(
        ProcessedOrder.account_id == account.id, ProcessedOrder.order_id == order.order_id,
    ).first()
    if existing is not None:
        result["status"] = "already_processed"
        result["detail"] = "Заказ с таким ID уже обработан ранее (идемпотентность сработала)."
        return result

    uid_1c = resolve_barcode(db, order.barcode, account.id)
    if uid_1c is None:
        result["status"] = "unmatched"
        result["detail"] = f"Баркод «{order.barcode}» не найден в таблице «Баркоды» — записан конфликт сопоставления."
        db.commit()
        return result

    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    if product is None:
        result["status"] = "unmatched"
        result["detail"] = "Баркод сопоставлен, но товар с таким ID_1С не найден."
        return result

    setting = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account.id,
    ).first()
    enabled = bool(setting and setting.enabled)

    # Отбор по включённым товарам (SyncSetting.enabled): живой поллер
    # (respect_enabled=True) трогает ТОЛЬКО включённые SKU — выключенный товар
    # пропускаем полностью и тихо: без движения в 1С, списания остатка, рассылки,
    # аномалии и отметки об обработке. После включения на «Синхронизируемых
    # товарах» поллер сам начнёт вести товар, а прошлые заказы добираются
    # бэкфиллом. Явные действия оператора не гейтим: бэкфилл (respect_enabled=
    # False) проводит выбранный товар, синтетический тест (is_test) прогоняет весь
    # код до включения (реальных движений он и так не создаёт).
    if respect_enabled and not is_test and not enabled:
        result["status"] = "skipped_disabled"
        result["detail"] = "Синхронизация выключена — заказ пропущен (нет в отборе)."
        return result

    if is_test:
        # Симуляция со страницы «Тестирование» НЕ трогает боевые поля товара.
        # Раньше трогала: остаток 100, симуляция заказа на 7 → в базе 93, и
        # следующая РЕАЛЬНАЯ рассылка отправляла на площадку 93 вместо 100.
        # Направление безопасное (недоотправка), но это всё равно значит, что
        # тест управляет боем. Считаем правдоподобное число для показа и для
        # тестовых записей очереди — от него же вычитаем прошлые открытые тесты,
        # чтобы цепочка симуляций выглядела как настоящая последовательность.
        new_stock = product.stock_on_hand - open_test_out(db, uid_1c) - order.quantity
    else:
        # Не клампим в 0: отрицательный остаток легитимен (пересортица) и так же
        # ведёт себя reconciliation (пишет actual_1c из 1С как есть). На площадку
        # всё равно уйдёт max(0, …) — клампит dispatch. Клампить здесь означало бы
        # молча терять величину пересортицы до следующей сверки.
        product.stock_on_hand = product.stock_on_hand - order.quantity
        # Ручной «передаваемый остаток» (override): заказ вычитается ИЗ НЕГО.
        # Оператор задал стартовое число — продажи его уменьшают. В автоматическом
        # сценарии (override не задан) заказ учитывается через сам остаток, здесь
        # трогать нечего.
        #
        # БЕЗ клампа в ноль: иначе приём и отмена перестают быть обратными и цифра
        # дрейфует вверх. Было 2, заказ на 3 → 0 (единица потеряна), отмена вернула
        # бы 3 — товар, которого нет. Минус здесь — это «долг», он честно вычитается
        # из будущих возвратов; на площадку всё равно уходит max(0, …) — клампит
        # transmit.sku_quantity, как и для самого остатка.
        if product.transmit_override is not None:
            product.transmit_override = product.transmit_override - order.quantity
        new_stock = product.stock_on_hand
    result["uid_1c"] = uid_1c
    result["new_stock"] = new_stock

    # Синтетический тест по выключенному товару — оставляем аномалию-предупреждение
    # (оператор видит, что прогоняет выключенный SKU); реальных движений is_test не
    # создаёт. Живой поллер сюда уже не доходит — отсеян гейтом выше.
    if not enabled and is_test:
        db.add(SyncAnomaly(
            uid_1c=uid_1c, account_id=account.id, reason=AnomalyReason.order_on_disabled,
            order_id=order.order_id, is_test=is_test,
        ))
        result["anomaly_created"] = True
        result["detail"] += " Синхронизация для этого кабинета выключена — зафиксирована аномалия."

    db.add(ProcessedOrder(
        account_id=account.id, order_id=order.order_id, uid_1c=uid_1c,
        quantity=order.quantity, status=OrderProcessStatus.processed,
    ))

    result["dispatched_to"] = _enqueue_dispatch_to_others(
        db, uid_1c, account.id, new_stock, reason="order", is_test=is_test,
    )

    barcode_for_1c = _representative_barcode(db, uid_1c)
    if barcode_for_1c:
        task = FtpTask(
            command="CREATE_MOVEMENT", barcode=barcode_for_1c,
            warehouse_from="ЦС Склад", warehouse_to=warehouse_pending,  # scheduler.SOURCE_WAREHOUSE_NAME
            quantity=order.quantity, order_id=order.order_id, account_id=account.id,
            movement_date=order_date,  # старт задним числом (страница тестирования); None = текущая дата
            is_test=is_test,
        )
        db.add(task)
        db.flush()
        result["ftp_task_id"] = task.id

    result["status"] = "processed"
    db.commit()
    return result


def existing_cancel_task(db: Session, order_id: str, account_id: int, is_test: bool = False):
    """Уже созданное задание отмены по этому заказу и кабинету, если оно есть.

    Отмена в 1С НЕ идемпотентна — в отличие от `CREATE_MOVEMENT`, где
    идемпотентность по номеру заказа сделана и проверена на бою 19.09. Поэтому
    гарантируем со своей стороны, что второе задание отмены по одному заказу не
    уедет никогда.

    МЕХАНИЗМ ПОМЕНЯЛСЯ 19.09, ВЫВОД — НЕТ. Раньше `ОтменитьПеремещенияЗаказа`
    находила по шаблону комментария и собственные реверсы (их комментарий
    начинается с `sync REVERSE` и содержит тот же `order_id`): вторая отмена
    отменила бы и само перемещение, и его возврат, то есть товар уехал бы на
    склад площадки вместо возврата на ЦС. Теперь реверсы из выборки исключены
    (`НЕ Комментарий ПОДОБНО "sync REVERSE%"`), но ИСХОДНОЕ перемещение остаётся
    проведённым и находится снова — значит вторая отмена создаст ВТОРОЙ обратный
    документ, и на ЦС вернётся вдвое больше, чем оттуда уезжало. Лишний приход
    товара, которого нет, как и был; починить это в обработке нельзя, не заведя
    там собственный учёт уже отменённых заказов.

    Смотрим задания в ЛЮБОМ статусе, а не только незакрытые:
    - `done` — 1С отмену уже провела, повтор и есть тот самый фантом;
    - `pending`/`sent` — задание ещё в работе, второе просто лишнее;
    - `timeout` — ответа нет, провела 1С отмену или нет, неизвестно; послать
      второе значит рискнуть фантомом ради предположения;
    - `failed` — 1С ответила отказом, нужен разбор человеком, а не повтор вслепую.

    `is_test` разделяет миры: симуляция не видит боевых заданий и наоборот."""
    return db.query(FtpTask).filter(
        FtpTask.command == "CANCEL_MOVEMENT",
        FtpTask.order_id == order_id,
        FtpTask.account_id == account_id,
        FtpTask.is_test.is_(is_test),
    ).order_by(FtpTask.id.asc()).first()


def process_cancellation(db: Session, cancelled_order: PlatformOrder, record: ProcessedOrder,
                          account: PlatformAccount, is_test: bool = False) -> dict:
    """Обрабатывает ОДИН реверс — та же логика переиспользования, что и
    process_new_order выше. record — существующая строка ProcessedOrder,
    которую нужно откатить (или частично откатить для PARTIAL_REFUND)."""

    result = {"status": None, "return_quantity": 0, "new_stock": None, "dispatched_to": [],
              "ftp_task_id": None, "duplicate_cancel": False}

    if cancelled_order.is_partial_refund:
        # ЧАСТИЧНЫЙ отказ мы провести не можем, и делать вид, что можем, опаснее,
        # чем отказаться.
        #
        # Мы вернули бы себе только отказанное количество, а в 1С ушло бы
        # `CANCEL_MOVEMENT|order_id|площадка` — количества в этой команде нет ни
        # поля, и `ОтменитьПеремещенияЗаказа` реверсит исходный документ ЦЕЛИКОМ.
        # Заказ на 3, отказ от 1: у нас +1, в 1С +3. Расхождение в две единицы
        # часовая сверка втянет в остаток как приход, и мы начнём продавать
        # отгруженное. Второй частичный отказ по тому же заказу вдобавок
        # отбрасывался как дубль — остаток не возвращался вовсе, а площадка
        # приносила эту отмену каждые две минуты все тридцать дней окна.
        #
        # Сегодня путь спящий: `is_partial_refund` не выставляет ни один клиент.
        # Поэтому это не потеря возможности, а запертая дверь — чтобы появившийся
        # завтра частичный отказ не поехал по сломанной дороге молча. Открывать
        # её надо вместе с протоколом 1С (количество в команде отмены и реверс на
        # указанное число), а не здесь.
        result["status"] = "unsupported"
        logger.warning(
            "частичный отказ по заказу %s (кабинет %s, отказано %s из %s) не проведён: "
            "команда отмены в 1С отменяет документ целиком, частичный реверс протоколом "
            "не предусмотрен — разберите вручную",
            cancelled_order.order_id, account.id,
            cancelled_order.refused_quantity, record.quantity,
        )
        return result

    return_quantity = record.quantity
    if return_quantity <= 0:
        result["status"] = "skipped"
        return result

    already = existing_cancel_task(db, cancelled_order.order_id, account.id, is_test)
    if already is not None:
        # Отмена по этому заказу уже уходила в 1С — второй раз НИЧЕГО не делаем:
        # ни задания (повтор дал бы фантомный приход, см. existing_cancel_task),
        # ни возврата остатка. Возврат тоже нельзя повторять: он уже сделан
        # первой отменой, а второй прибавил бы товар, которого нет, — то есть
        # защита от фантома в 1С обернулась бы оверселлом у нас.
        result["status"] = "duplicate"
        result["duplicate_cancel"] = True
        result["ftp_task_id"] = already.id
        if record.status != OrderProcessStatus.cancelled:
            # Заказ всё-таки закрываем: иначе опрос будет приносить его каждые
            # две минуты и каждый раз упираться в эту же проверку. Оговорка про
            # частичный отказ отсюда убрана: до этой строки он больше не доходит,
            # его отсекает проверка в начале функции.
            record.status = OrderProcessStatus.cancelled
            record.cancelled_at = now_utc()
            db.commit()
        logger.warning(
            "отмена заказа %s (кабинет %s): задание отмены уже есть (#%s, %s) — "
            "повтор пропущен целиком, иначе в 1С появился бы фантомный приход",
            cancelled_order.order_id, account.id, already.id, already.status.value,
        )
        return result

    product = db.query(Product).filter(Product.uid_1c == record.uid_1c).first()
    if product is None:
        result["status"] = "skipped"
        return result

    if is_test:
        # Как и приём: симуляция не трогает боевые поля. Отменяемая запись сейчас
        # ещё числится открытой, поэтому её вклад прибавляем обратно вручную.
        new_stock = product.stock_on_hand - open_test_out(db, record.uid_1c) + return_quantity
    else:
        product.stock_on_hand += return_quantity
        # Симметрично приёму заказа: если у товара задан ручной override, отмена
        # возвращает вычтенное обратно в него. Тоже без клампа — приём и отмена
        # обязаны быть в точности обратны друг другу.
        if product.transmit_override is not None:
            product.transmit_override = product.transmit_override + return_quantity
        new_stock = product.stock_on_hand
    result["return_quantity"] = return_quantity
    result["new_stock"] = new_stock

    # Частичный отказ сюда не доходит — отсечён в начале функции.
    record.status = OrderProcessStatus.cancelled
    record.cancelled_at = now_utc()
    result["status"] = "reversed"

    result["dispatched_to"] = _enqueue_dispatch_to_others(
        db, record.uid_1c, account.id, new_stock, reason="cancel", is_test=is_test,
    )

    barcode_for_1c = _representative_barcode(db, record.uid_1c)
    if barcode_for_1c:
        task = FtpTask(
            command="CANCEL_MOVEMENT", barcode=barcode_for_1c,
            quantity=return_quantity, order_id=cancelled_order.order_id, account_id=account.id,
            is_test=is_test,
        )
        db.add(task)
        db.flush()
        result["ftp_task_id"] = task.id

    db.commit()
    return result


def process_confirmation(db: Session, record: ProcessedOrder, account: PlatformAccount,
                         warehouse_pending: str, warehouse_sold: str, is_test: bool = False) -> dict:
    """Подтверждение заказа: товар физически уходит покупателю, в 1С это
    перемещение «<Площадка>.Ожидает» → «Склад <Площадка>». Остаток НЕ меняем
    (он уже списан при приёме заказа) и рассылку НЕ делаем — это чисто
    учётное движение внутри 1С. Обрабатываем только заказы в статусе
    processed (не подтверждённые и не отменённые)."""
    result = {"status": None, "ftp_task_id": None}

    if record.status != OrderProcessStatus.processed:
        result["status"] = "skipped"
        return result

    record.status = OrderProcessStatus.confirmed

    # Вариант A: резерв под заказ уже лежит в основном складе площадки
    # (warehouse_pending == warehouse_sold). Товар физически там же, куда его
    # «продают», поэтому при подтверждении отдельного перемещения в 1С НЕ нужно —
    # только фиксируем статус. Движение создаём лишь если склад «Ожидает»
    # отличается от склада продаж (двухшаговая схема, здесь не используется).
    barcode_for_1c = _representative_barcode(db, record.uid_1c)
    if barcode_for_1c and warehouse_pending != warehouse_sold:
        task = FtpTask(
            command="CONFIRM_MOVEMENT", barcode=barcode_for_1c,
            warehouse_from=warehouse_pending, warehouse_to=warehouse_sold,
            quantity=record.quantity, order_id=record.order_id, account_id=account.id,
            is_test=is_test,
        )
        db.add(task)
        db.flush()
        result["ftp_task_id"] = task.id

    result["status"] = "confirmed"
    db.commit()
    return result


def poll_new_orders(db: Session, client: PlatformClient, account: PlatformAccount, warehouse_pending: str) -> dict:
    """Раздел 4/5: опрашивает площадку и прогоняет каждый новый заказ через
    process_new_order, агрегируя статистику."""

    orders = client.get_orders_awaiting_confirmation()
    stats = {"processed": 0, "already_processed": 0, "unmatched": 0, "anomalies": 0,
             "skipped_disabled": 0, "failed": 0, "problems": []}

    status_to_stat = {"processed": "processed", "already_processed": "already_processed",
                      "unmatched": "unmatched", "skipped_disabled": "skipped_disabled"}

    for order in orders:
        # Заказ обрабатывается ПООТДЕЛЬНО, как и в расчёте (`recalc.apply_orders`).
        # Раньше цикл не был защищён ничем: исключение на одной строке — гонка за
        # `database is locked` во время часовой сверки, ошибка целостности,
        # что угодно — обрывало весь цикл, и остальные новые заказы кабинета в
        # этом проходе не проводились вовсе. Без отката сессия вдобавок остаётся
        # сломанной после неудачного commit внутри `process_new_order`, так что
        # следующий заказ падал бы уже на ровном месте.
        try:
            result = process_new_order(db, order, account, warehouse_pending)
        except Exception as e:                      # noqa: BLE001 — причина уходит наверх текстом
            db.rollback()
            stats["failed"] += 1
            stats["problems"].append(f"заказ {order.order_id}: {type(e).__name__}: {e}")
            continue
        stat_key = status_to_stat.get(result["status"])
        if stat_key:
            stats[stat_key] += 1
        if result["anomaly_created"]:
            stats["anomalies"] += 1

    return stats


def poll_cancellations(db: Session, client: PlatformClient, account: PlatformAccount) -> dict:
    """Обратный ход: заказ, который мы уже списали, оказался отменён —
    возвращаем количество и рассылаем заново (раздел 5 спецификации)."""

    # Только НЕподтверждённые заказы (в статусе «Ожидает»): отмена = возврат
    # «<Площадка>.Ожидает» → ЦС. Подтверждённый заказ терминален — продажу и
    # возврат ПОСЛЕ продажи в нашем потоке не обрабатываем (по требованию).
    open_orders = _real_open_orders(db, account)
    order_ids = [o.order_id for o in open_orders]
    orders_by_id = {o.order_id: o for o in open_orders}

    cancelled = client.get_cancelled_orders(order_ids)
    stats = {"reversed": 0, "unsupported": 0, "failed": 0, "problems": []}

    for c in cancelled:
        record = orders_by_id.get(c.order_id)
        if record is None or record.uid_1c is None:
            continue

        # По одной отмене, как и в приёме заказов: пропущенная отмена — это
        # невозвращённая на ЦС единица, и терять из-за неё весь остаток цикла
        # нельзя. Без отката сессия после неудачного commit внутри
        # `process_cancellation` остаётся сломанной.
        try:
            result = process_cancellation(db, c, record, account)
        except Exception as e:                      # noqa: BLE001 — причина уходит наверх текстом
            db.rollback()
            stats["failed"] += 1
            stats["problems"].append(f"отмена {c.order_id}: {type(e).__name__}: {e}")
            continue
        if result["status"] == "reversed":
            stats["reversed"] += 1
        elif result["status"] == "unsupported":
            # Частичный отказ: провести нечем (см. process_cancellation). Считаем
            # и называем — молчать о непроведённой отмене нельзя.
            stats["unsupported"] += 1
            stats["problems"].append(
                f"отмена {c.order_id}: частичный отказ, протоколом 1С не поддержан")

    return stats


def poll_confirmations(db: Session, client: PlatformClient, account: PlatformAccount,
                       warehouse_pending: str, warehouse_sold: str) -> dict:
    """Подтверждённые/отгруженные заказы: те, что мы приняли (processed) и
    которые площадка перевела в статус «подтверждён/отгружен». По каждому —
    перемещение в 1С «<Площадка>.Ожидает» → «Склад <Площадка>»."""
    open_orders = _real_open_orders(db, account)
    order_ids = [o.order_id for o in open_orders]
    orders_by_id = {o.order_id: o for o in open_orders}

    confirmed = client.get_confirmed_orders(order_ids)
    stats = {"confirmed": 0, "failed": 0, "problems": []}

    for c in confirmed:
        record = orders_by_id.get(c.order_id)
        if record is None or record.uid_1c is None:
            continue
        # См. `poll_new_orders`: сбой на одном подтверждении не должен уносить
        # ни остальные подтверждения, ни ОТМЕНЫ — они опрашиваются следующим
        # шагом того же задания, и без этой защиты пропадали вместе с ним.
        try:
            result = process_confirmation(db, record, account, warehouse_pending, warehouse_sold)
        except Exception as e:                      # noqa: BLE001 — причина уходит наверх текстом
            db.rollback()
            stats["failed"] += 1
            stats["problems"].append(f"подтверждение {c.order_id}: {type(e).__name__}: {e}")
            continue
        if result["status"] == "confirmed":
            stats["confirmed"] += 1

    return stats

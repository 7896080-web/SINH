"""Единственный источник правды: сколько штук уходит на площадку и почему.

Раньше одна и та же лестница приоритетов была скопирована в трёх местах
(`workers/dispatch.py`, страница остатков, страница тестирования). Копии
разошлись: интерфейс показывал «передаётся 18», пока рассылка отправляла 0,
потому что в UI не было проверки `broadcast_enabled`. Теперь считает один
модуль, а интерфейс ещё и объясняет оператору причину нуля.

Лестница (сверху вниз, первое сработавшее правило выигрывает):

0. `Product.broadcast_enabled = False`  → 0  (SKU снят с продажи)
1. кабинет не отмечен для товара        → 0  (`SyncSetting.enabled`)
2. расчёт не покрывал этот кабинет      → 0  (`Product.recalc_account_ids`)
3. рассылка на кабинет на паузе         → 0  (`PlatformAccount.dispatch_enabled`)
4. задан порог трансляции               → max(0, остаток ЦС − порог)
5. задан ручной остаток (legacy)        → max(0, ручной остаток)
6. иначе                                → max(0, остаток ЦС − резерв),
   и если это не больше порога кабинета → 0

Шаги 0–3 — «выключатели», 4–6 — «сколько». Порог кабинета применяется только
в автоматическом режиме (шаг 6): и порог трансляции, и ручной остаток заданы
оператором явно, поверх них страховой буфер не навешиваем.

Шаг 2 появился после разбора 18.09: «актуализирован» — свойство пары
товар+кабинет, а не одного товара. Расчёт поднимает заказы только с кабинетов,
отмеченных в его момент, и для кабинета, отмеченного позже, остаток не сверен
ничем. Раньше галочка на таком кабинете отправляла туда полный остаток через
45 секунд.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import DispatchQueueItem, Product


# Как посчитана цифра уровня SKU — для подписи в интерфейсе.
MODE_OFFSET = "offset"      # порог трансляции
MODE_OVERRIDE = "override"  # ручной остаток (устаревший режим)
MODE_AUTO = "auto"          # остаток − резерв


def offset_from_base(product: Product | None) -> int | None:
    """Порог из трёх чисел на дату — или None, если считать ещё не из чего.

        порог = остаток ЦС на дату − (факт на дату − бронь)

    Пример: на 07.08 учёт 1С показал 10, реально на складе нашли 8, из них 2
    держим у себя (бронь). Доступно было 6, значит порог 4 — и дальше на
    площадки всегда уходит «остаток ЦС минус 4», сколько бы раз остаток ни
    изменился. Порог описывает ПОСТОЯННОЕ расхождение, поэтому он и не дрейфует:
    заказы, приходы, списания и пересортица двигают остаток, а не его.

    Факт не введён — берём остаток ЦС на дату, и порог получается равным просто
    брони: расхождения нет, есть только то, что мы держим у себя.

    Товара не было в выгрузке на дату — это `offset_base_stock == 0`, и порог
    снова равен брони. Тогда уходит «остаток ЦС − бронь», то есть ровно столько
    же, сколько в автоматическом режиме: для товара без истории на дату
    поведение не меняется вовсе.

    None означает «дата не задана» или «1С ещё не ответила» — в обоих случаях
    трогать порог нельзя: пересчитать его не из чего, а обнулить значило бы
    тихо отправить на площадки полный остаток.
    """
    if product is None:
        return None
    if product.offset_base_date is None or product.offset_base_stock is None:
        return None
    fact = product.fact_at_date
    if fact is None:
        fact = product.offset_base_stock
    return product.offset_base_stock - (fact - (product.reserve or 0))


def recompute_offset(product: Product | None) -> bool:
    """Пересчитывает порог, если он выводится из даты. True — порог изменён.

    Единственный, кто пишет `broadcast_offset` при заданной дате. Вызывать
    обязательно при смене ЛЮБОГО из трёх чисел, включая бронь: иначе новая бронь
    останется словами, потому что в пороге будет сидеть старая.

    Даты нет — порог не трогаем: там живёт число, введённое руками до этой
    правки, и стирать его молча нельзя.
    """
    value = offset_from_base(product)
    if value is None:
        return False
    changed = product.broadcast_offset != value
    product.broadcast_offset = value
    # Порог заменяет устаревший ручной остаток — иначе они спорили бы за
    # приоритет, а в лестнице порог и так выше.
    product.transmit_override = None
    return changed


def sku_quantity(product: Product | None, raw_stock: int | None = None) -> int:
    """Количество уровня SKU: шаги 3–5 лестницы, без учёта кабинета.

    `raw_stock` — остаток, от которого считать вместо текущего `stock_on_hand`:
    рассылка передаёт сюда значение, попавшее в очередь. Влияет ТОЛЬКО на
    автоматический режим (остаток − резерв). Порог трансляции намеренно считает
    от текущего `stock_on_hand`: он описывает постоянное расхождение учёта 1С с
    реальным складом, и применять его к числу, посчитанному когда-то раньше,
    значило бы транслировать устаревший остаток (закреплено тестом
    `test_offset_uses_current_stock_not_quantity_arg`). Ручной остаток — число
    самого оператора, никакого «от чего считать» там нет.

    Выключатели (шаги 0–2) здесь НЕ применяются: их проверяет `explain`
    и `quantity_for_account`, чтобы интерфейс мог показать «было бы N, но».
    """
    if product is None:
        return max(0, raw_stock or 0)
    stock = product.stock_on_hand if raw_stock is None else raw_stock
    if product.broadcast_offset is not None:
        return max(0, (product.stock_on_hand or 0) - product.broadcast_offset)
    if product.transmit_override is not None:
        return max(0, product.transmit_override)
    return max(0, (stock or 0) - (product.reserve or 0))


def covered_accounts(product: Product | None) -> set[int]:
    """Кабинеты, по которым остаток товара действительно сверен расчётом.

    Пишет сюда `recalc.catch_up_product` — и только те кабинеты, чьи заказы
    удалось прочитать целиком. Пустое множество значит «ни один», в том числе
    для товаров, рассчитанных до появления колонки: считать их покрытыми было бы
    ровно тем допущением, из-за которого остаток уезжал на несверенный кабинет.
    """
    raw = ((product.recalc_account_ids if product is not None else "") or "").strip()
    if not raw:
        return set()
    return {int(p) for p in (x.strip() for x in raw.split(",")) if p.isdigit()}


def sku_mode(product: Product | None) -> str:
    if product is not None and product.broadcast_offset is not None:
        return MODE_OFFSET
    if product is not None and product.transmit_override is not None:
        return MODE_OVERRIDE
    return MODE_AUTO


@dataclass
class Transmit:
    """Результат для пары товар+кабинет: сколько уйдёт и почему именно столько."""

    quantity: int
    blocked: bool          # True — уходит 0 из-за выключателя или порога кабинета
    reason: str            # человеческая причина; пусто, если ничего не мешает
    fix_hint: str = ""     # что сделать оператору, чтобы разблокировать
    # Сколько ушло бы, если снять мешающий ВЫКЛЮЧАТЕЛЬ (трансляция товара, пауза
    # площадки). Ноль с причиной не отвечает на вопрос, ради которого оператор
    # смотрит в эту колонку перед включением: «а что уйдёт, если я нажму Вкл?».
    # Для неотмеченного кабинета прогноза нет — туда не передают по решению
    # оператора, а не из-за выключателя.
    potential: int = 0


def _after_switches(product: Product, setting) -> int:
    """Сколько ушло бы в этот кабинет, будь выключатели сняты.

    Считается по той же лестнице, только без проверки самих выключателей: порог
    кабинета здесь участвует, потому что он останется и после включения.
    """
    base = sku_quantity(product)
    threshold = getattr(setting, "min_threshold", 0) or 0
    if sku_mode(product) == MODE_AUTO and threshold and base <= threshold:
        return 0
    return base


def explain(product: Product | None, setting, account) -> Transmit:
    """Полная лестница для пары товар+кабинет. `setting` — SyncSetting или None,
    `account` — PlatformAccount или None (None = смотрим только уровень SKU)."""
    if product is None:
        return Transmit(0, True, "товар не найден")

    # Неотмеченный кабинет проверяем ПЕРВЫМ: туда не передают по решению
    # оператора, и прогноз «сколько ушло бы» там бессмыслен — включать нечего.
    if account is not None and (setting is None or not setting.enabled):
        return Transmit(0, True, f"кабинет «{account.name}» не отмечен для товара",
                        "галочка в колонке кабинета")

    if not product.broadcast_enabled:
        return Transmit(0, True, "трансляция товара выключена",
                        "колонка «Трансляция» в этой строке",
                        potential=_after_switches(product, setting))

    # Расчёт сверяет остаток по заказам ТОЛЬКО тех кабинетов, что были отмечены
    # в его момент. Для кабинета, отмеченного позже, остаток ничем не подтверждён:
    # его продажи с базовой даты в 1С не проведены, и уйдёт туда завышенное число.
    # Прогноза здесь намеренно нет — правильный ответ не «столько уйдёт», а
    # «сначала пересчёт», и после пересчёта число всё равно станет другим.
    if (account is not None and product.recalc_done_at is not None
            and account.id not in covered_accounts(product)):
        return Transmit(0, True,
                        f"расчёт не покрывал кабинет «{account.name}» — остаток по нему не сверен",
                        "запустить «Расчёт» с этой галочкой")

    if account is not None and not account.dispatch_enabled:
        return Transmit(0, True, f"рассылка на «{account.name}» на паузе",
                        "переключатели площадок вверху страницы",
                        potential=_after_switches(product, setting))

    base = sku_quantity(product)
    mode = sku_mode(product)

    threshold = getattr(setting, "min_threshold", 0) or 0
    if mode == MODE_AUTO and threshold and base <= threshold:
        return Transmit(0, True, f"порог кабинета {threshold}: доступно {base} — не больше порога",
                        "уменьшить порог в колонке кабинета")

    return Transmit(base, False, "")


def quantity_for_account(db: Session, uid_1c: str, account_id: int, raw_stock: int) -> int:
    """То, что реально уходит на площадку. Используется рассылкой в момент отправки.

    Проверяет ВСЮ лестницу, включая выключатели 1–2. Раньше считалось, что
    отметку кабинета и паузу рассылка отсекает раньше («очередь копится только по
    отмеченным кабинетам»), и здесь их не дублировали. Это неверно: запись,
    попавшая в очередь до снятия галочки, переживает снятие — цикл рассылки брал
    её из очереди и отправлял на уже отключённый кабинет полный остаток. Теперь
    интерфейс (`explain`) и рассылка считают ровно одно и то же."""
    from app.models import PlatformAccount, SyncSetting  # локально: избегаем цикла импортов

    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    if product is None:
        return max(0, raw_stock)
    if not product.broadcast_enabled:
        return 0
    setting = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account_id,
    ).first()
    if setting is None or not setting.enabled:
        return 0
    if product.recalc_done_at is not None and account_id not in covered_accounts(product):
        return 0
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is not None and not account.dispatch_enabled:
        return 0

    # Рассылка считает от значения, попавшего в очередь (raw_stock), — оно могло
    # быть посчитано чуть раньше текущего stock_on_hand.
    base = sku_quantity(product, raw_stock=raw_stock)
    mode = sku_mode(product)
    threshold = setting.min_threshold or 0
    if mode == MODE_AUTO and threshold and base <= threshold:
        return 0
    return base


def enqueue_full_resend(db: Session, uid_1c: str, account_id: int, reason: str = "manual_enable"):
    """Разовая доотправка полного текущего остатка (раздел 10 спецификации).
    Кладёт запись в очередь — реальную отправку делает воркер dispatch.py.

    **Товар с выключенной трансляцией в очередь не ставится вовсе.** Раньше
    ставился, а рассылка считала по нему ноль и этот ноль отправляла на площадку.
    Для товара, который ещё ни разу не транслировался, это не «отзыв остатка», а
    обнуление чужой карточки, по которой идут продажи: мы туда ничего не
    отправляли и отзывать нам нечего.

    Разница принципиальная и она в намерении. Осознанный отзыв — это
    `enqueue_withdrawal`, отдельная функция, которую интерфейс зовёт явно, когда
    оператор снимает галочку кабинета или выключает трансляцию. Здесь же путь
    автоматический: доотправка после правки брони, факта, порога. Пока расчёт не
    закончен и трансляция не включена, наружу не должно уходить ничего.
    """
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    if product is not None and not product.broadcast_enabled:
        return
    # Кабинет, которого расчёт не касался, — тот же случай: в очереди по нему
    # выйдет ноль (ступень 2 лестницы), и этот ноль уедет на площадку, обнулив
    # живую карточку. Отправлять туда нечего, пока не прошёл пересчёт.
    if (product is not None and product.recalc_done_at is not None
            and account_id not in covered_accounts(product)):
        return
    quantity = product.stock_on_hand if product else 0
    db.add(DispatchQueueItem(uid_1c=uid_1c, account_id=account_id, quantity=quantity, reason=reason))


def enqueue_withdrawal(db: Session, uid_1c: str, account_id: int, reason: str = "manual_disable"):
    """Отзыв остатка с площадки: ставит в очередь ноль.

    Нужен, когда товар перестаёт передаваться в кабинет (снята галочка). Без
    этого на площадке остаётся последнее отправленное число, она продолжает
    продавать — а заказы по этой паре гейт отбора уже пропускает, то есть ни
    списания у нас, ни документа в 1С не будет. Ровно так же ведёт себя главный
    выключатель товара: он тоже отправляет ноль, а не «забывает» площадку."""
    db.add(DispatchQueueItem(uid_1c=uid_1c, account_id=account_id, quantity=0, reason=reason))

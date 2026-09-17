"""Единственный источник правды: сколько штук уходит на площадку и почему.

Раньше одна и та же лестница приоритетов была скопирована в трёх местах
(`workers/dispatch.py`, страница остатков, страница тестирования). Копии
разошлись: интерфейс показывал «передаётся 18», пока рассылка отправляла 0,
потому что в UI не было проверки `broadcast_enabled`. Теперь считает один
модуль, а интерфейс ещё и объясняет оператору причину нуля.

Лестница (сверху вниз, первое сработавшее правило выигрывает):

0. `Product.broadcast_enabled = False`  → 0  (SKU снят с продажи)
1. кабинет не отмечен для товара        → 0  (`SyncSetting.enabled`)
2. рассылка на кабинет на паузе         → 0  (`PlatformAccount.dispatch_enabled`)
3. задан порог трансляции               → max(0, остаток ЦС − порог)
4. задан ручной остаток (legacy)        → max(0, ручной остаток)
5. иначе                                → max(0, остаток ЦС − резерв),
   и если это не больше порога кабинета → 0

Шаги 0–2 — «выключатели», 3–5 — «сколько». Порог кабинета применяется только
в автоматическом режиме (шаг 5): и порог трансляции, и ручной остаток заданы
оператором явно, поверх них страховой буфер не навешиваем.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import DispatchQueueItem, Product


# Как посчитана цифра уровня SKU — для подписи в интерфейсе.
MODE_OFFSET = "offset"      # порог трансляции
MODE_OVERRIDE = "override"  # ручной остаток (устаревший режим)
MODE_AUTO = "auto"          # остаток − резерв


def sku_quantity(product: Product | None, raw_stock: int | None = None) -> int:
    """Количество уровня SKU: шаги 3–5 лестницы, без учёта кабинета.

    `raw_stock` — остаток, от которого считать, если он не равен текущему
    `stock_on_hand` (рассылка считает от значения, попавшего в очередь).
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

    @property
    def ok(self) -> bool:
        return not self.blocked


def explain(product: Product | None, setting, account) -> Transmit:
    """Полная лестница для пары товар+кабинет. `setting` — SyncSetting или None,
    `account` — PlatformAccount или None (None = смотрим только уровень SKU)."""
    if product is None:
        return Transmit(0, True, "товар не найден")

    if not product.broadcast_enabled:
        return Transmit(0, True, "трансляция товара выключена",
                        "колонка «Трансляция» в этой строке")

    if account is not None:
        if setting is None or not setting.enabled:
            return Transmit(0, True, f"кабинет «{account.name}» не отмечен для товара",
                            "галочка в колонке кабинета")
        if not account.dispatch_enabled:
            return Transmit(0, True, f"рассылка на «{account.name}» на паузе",
                            "переключатели площадок вверху страницы")

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
    Кладёт запись в очередь — реальную отправку делает воркер dispatch.py."""
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
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

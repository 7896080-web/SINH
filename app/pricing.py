"""Репрайсер: расчёт цены товара для кабинета и ограничители.

Цена = себестоимость 1С × (1 + наценка%) + надбавка, округлённая вверх до шага
правила с «красивым» окончанием. Ручная цена товара в кабинете заменяет расчёт.

Ограничители (обязательны, обойти их расчётом нельзя):
  * ПОЛ — цена не ниже себестоимость × (1 + мин. наценка%). Нарушение не
    отправляется вообще: ни подтверждением, ни ручной ценой.
  * ЛИМИТ ШАГА — изменение больше max_change_percent от последней ПРИНЯТОЙ
    площадкой цены блокируется; отправить можно только отдельным
    подтверждением оператора («да, это не ошибка в себестоимости»).
  * Сама цена никогда не уходит без подтверждения: расчёт только предлагает.

Всё, что здесь, — чистые вычисления и запись предложений; сетевых вызовов нет.
"""

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from sqlalchemy.orm import Session

from app.models import (PlatformAccount, PriceChange, PriceChangeStatus, PriceRule, Product,
                        ProductPrice, SyncSetting)

BLOCK_FLOOR = "floor"
BLOCK_MAX_CHANGE = "max_change"

BLOCK_LABELS = {
    BLOCK_FLOOR: "ниже минимальной наценки — не отправляется",
    BLOCK_MAX_CHANGE: "изменение больше лимита — нужно отдельное подтверждение",
}


@dataclass
class PriceDecision:
    new_price: int | None          # None — посчитать нельзя (см. note)
    source: str = "rule"           # rule / manual
    block_reason: str | None = None
    note: str = ""


def _dec(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def round_price(raw: Decimal, step: int, minus: int) -> int:
    """Округление ВВЕРХ до шага, затем «красивое» окончание: шаг 100, минус 1 →
    1201 становится 1299. Вверх — чтобы округление никогда не съедало наценку."""
    step = max(int(step or 1), 1)
    minus = max(int(minus or 0), 0)
    if minus >= step:
        minus = 0                  # окончание длиннее шага бессмысленно — игнорируем
    rounded = int((raw / step).to_integral_value(rounding=ROUND_CEILING)) * step - minus
    if rounded < raw:              # минус увёл ниже расчётной — берём следующий шаг
        rounded += step
    return rounded


def floor_price(cost: Decimal, rule: PriceRule) -> int:
    """Минимально допустимая цена, ₽ (вверх до рубля)."""
    return math.ceil(cost * (1 + _dec(rule.min_margin_percent) / 100))


def rule_price(cost: Decimal, rule: PriceRule) -> int:
    raw = cost * (1 + _dec(rule.markup_percent) / 100) + _dec(rule.fixed_add)
    return round_price(raw, rule.round_step, rule.round_minus)


def change_percent(old: int | None, new: int) -> float | None:
    if not old:
        return None
    return (new - old) * 100.0 / old


def decide_price(cost, rule: PriceRule, manual_price: int | None,
                 last_sent_price: int | None) -> PriceDecision:
    """Какую цену предложить и пропускают ли её ограничители."""
    cost_dec = _dec(cost) if cost is not None else None

    if manual_price is not None:
        decision = PriceDecision(new_price=int(manual_price), source="manual")
    elif cost_dec is None or cost_dec <= 0:
        return PriceDecision(new_price=None, note="нет себестоимости из 1С")
    elif _dec(rule.markup_percent) <= 0:
        # Правило по умолчанию (наценка 0) — это «не настроено», а не «продавать
        # по себестоимости». Расчёт по нему не делаем; ручная цена работает.
        return PriceDecision(new_price=None, note="правило кабинета не настроено (наценка 0%)")
    else:
        decision = PriceDecision(new_price=rule_price(cost_dec, rule))

    if decision.new_price <= 0:
        return PriceDecision(new_price=None, source=decision.source, note="цена должна быть больше нуля")

    # Пол проверяется и для ручной цены. Без себестоимости проверять не от чего —
    # ручная цена тогда допустима, но оператор видит предупреждение.
    if cost_dec is not None and cost_dec > 0:
        floor = floor_price(cost_dec, rule)
        if decision.new_price < floor:
            decision.block_reason = BLOCK_FLOOR
            decision.note = f"минимум {floor} ₽ при себестоимости {cost_dec}"
            return decision
    elif decision.source == "manual":
        decision.note = "себестоимости нет — пол не проверен"

    pct = change_percent(last_sent_price, decision.new_price)
    if pct is not None and abs(pct) > float(_dec(rule.max_change_percent)):
        decision.block_reason = BLOCK_MAX_CHANGE
        decision.note = f"изменение {pct:+.1f}% при лимите {rule.max_change_percent}%"
    return decision


def get_or_create_rule(db: Session, account_id: int) -> PriceRule:
    rule = db.query(PriceRule).filter(PriceRule.account_id == account_id).first()
    if rule is None:
        rule = PriceRule(account_id=account_id, markup_percent=0, fixed_add=0, round_step=1,
                         round_minus=0, min_margin_percent=0, max_change_percent=20)
        db.add(rule)
        db.flush()
    return rule


def _open_changes(db: Session, account_id: int, uid_1c: str | None = None, is_test: bool = False):
    q = db.query(PriceChange).filter(
        PriceChange.account_id == account_id,
        PriceChange.is_test.is_(is_test),
        PriceChange.status.in_([PriceChangeStatus.proposed, PriceChangeStatus.blocked]),
    )
    if uid_1c is not None:
        q = q.filter(PriceChange.uid_1c == uid_1c)
    return q


def recalculate_account(db: Session, account: PlatformAccount, uids: list[str] | None = None,
                        is_test: bool = False) -> dict:
    """Пересчитывает предложения по кабинету.

    Берутся только товары, у которых синхронизация с этим кабинетом включена
    (те же, чьи остатки мы ведём на площадке). Прежние нерешённые предложения
    по кабинету вытесняются новым расчётом (status=rejected с пометкой), чтобы в
    очереди не висели цены от старой себестоимости. Уже ПОДТВЕРЖДЁННЫЕ и ещё не
    отправленные не трогаем — решение оператора расчёт не отменяет.

    Товар, цена которого совпадает с последней принятой, предложения не получает."""
    rule = get_or_create_rule(db, account.id)
    # no_cost — «посчитать нельзя»: нет себестоимости или правило не настроено.
    stats = {"proposed": 0, "blocked": 0, "unchanged": 0, "no_cost": 0}

    q = db.query(Product).join(SyncSetting, SyncSetting.uid_1c == Product.uid_1c).filter(
        SyncSetting.account_id == account.id, SyncSetting.enabled.is_(True),
    )
    if uids is not None:
        q = q.filter(Product.uid_1c.in_(uids))

    superseded = _open_changes(db, account.id, is_test=is_test)
    if uids is not None:
        superseded = superseded.filter(PriceChange.uid_1c.in_(uids))
    for old in superseded.all():
        old.status = PriceChangeStatus.rejected
        old.note = "вытеснено новым расчётом"

    prices = {p.uid_1c: p for p in db.query(ProductPrice).filter(ProductPrice.account_id == account.id)}
    for product in q.all():
        pp = prices.get(product.uid_1c)
        last_sent = pp.last_sent_price if pp else None
        decision = decide_price(product.cost_price, rule, pp.manual_price if pp else None, last_sent)
        if decision.new_price is None:
            stats["no_cost"] += 1
            continue
        if last_sent == decision.new_price:
            stats["unchanged"] += 1
            continue
        db.add(PriceChange(
            uid_1c=product.uid_1c, account_id=account.id, cost_price=product.cost_price,
            old_price=last_sent, new_price=decision.new_price, source=decision.source,
            status=PriceChangeStatus.blocked if decision.block_reason else PriceChangeStatus.proposed,
            block_reason=decision.block_reason, note=decision.note[:255] or None, is_test=is_test,
        ))
        stats["blocked" if decision.block_reason else "proposed"] += 1

    db.commit()
    return stats


def approve(change: PriceChange, actor: str, confirm_large: bool = False) -> str | None:
    """Подтверждение одного предложения. Возвращает текст отказа или None.
    Пол не обходится никогда; лимит шага — только с confirm_large."""
    if change.status not in (PriceChangeStatus.proposed, PriceChangeStatus.blocked):
        return "уже решено"
    if change.block_reason == BLOCK_FLOOR:
        return BLOCK_LABELS[BLOCK_FLOOR]
    if change.block_reason == BLOCK_MAX_CHANGE and not confirm_large:
        return BLOCK_LABELS[BLOCK_MAX_CHANGE]
    from app.timeutils import now_utc
    change.status = PriceChangeStatus.approved
    change.decided_by = actor
    change.decided_at = now_utc()
    return None

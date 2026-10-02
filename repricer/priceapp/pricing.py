"""Расчёт цены и ограничители.

Экономика (задана заказчиком):
    себестоимость, ₽  = себестоимость 1С, $ × курс
    к получению, ₽    = цена × (1 − комиссия%)
    наценка, ₽        = к получению − себестоимость, ₽
    коэффициент       = к получению / себестоимость, ₽   (2 = +100%, 2,5 = +150%, 3 = +200%)

Правило кабинета задаёт желаемый коэффициент, отсюда цена:
    цена = себестоимость ₽ × коэффициент / (1 − комиссия%)
с округлением ВВЕРХ до шага и «красивым» окончанием (шаг 100, минус 1 → 1299).

Ограничители (обойти расчётом нельзя):
  * ПОЛ — коэффициент не ниже `min_markup_coef`. Нарушение не отправляется ни
    подтверждением, ни ручной ценой, и перепроверяется при отправке по ТЕКУЩИМ
    курсу, себестоимости и комиссии.
  * ЛИМИТ ШАГА — изменение больше `max_change_percent` от последней ПРИНЯТОЙ
    площадкой цены — только отдельным подтверждением.
  * Цена никогда не уходит сама: расчёт только предлагает.

Правило — у ПЛОЩАДКИ, общее для всех её кабинетов (`PlatformRule`). Цена может
браться и от другой площадки: расчётная цена базы × коэффициент (Ozon = WB × 1,1).
База считается ПО СВОЕМУ ПРАВИЛУ, без ручных цен: ручная цена живёт в кабинете, а
у базы их бывает несколько (у WB три ИП) — какая из них «та самая», сказать нельзя.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from priceapp import rates
from priceapp.models import (Account, BasePrice, OnecCost, PlatformPrice, PlatformRule, PriceChange,
                             PriceChangeStatus, ProductPrice)
from priceapp.timeutils import now_utc

# `base_platform` правила может указывать не на площадку, а на БАЗОВУЮ цену товара.
BASE = "base"
BASE_LABEL = "базовая цена"

BLOCK_FLOOR = "floor"
BLOCK_MAX_CHANGE = "max_change"

BLOCK_LABELS = {
    BLOCK_FLOOR: "наценка ниже минимальной — не отправляется",
    BLOCK_MAX_CHANGE: "изменение больше лимита — нужно отдельное подтверждение",
}

OPEN = (PriceChangeStatus.proposed.value, PriceChangeStatus.blocked.value)


def _dec(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def round_price(raw: Decimal, step: int, minus: int) -> int:
    """Вверх до шага, затем «красивое» окончание. Вверх — чтобы округление
    никогда не съедало наценку."""
    step = max(int(step or 1), 1)
    minus = max(int(minus or 0), 0)
    if minus >= step:
        minus = 0
    rounded = int((raw / step).to_integral_value(rounding=ROUND_CEILING)) * step - minus
    if rounded < raw:
        rounded += step
    return rounded


def payout(price, commission_percent) -> Decimal:
    """К получению с цены после комиссии площадки."""
    return _dec(price) * (1 - _dec(commission_percent) / 100)


def markup(price, cost_rub, commission_percent) -> tuple[Decimal, Decimal | None]:
    """(наценка ₽, коэффициент = к получению / себестоимость). Коэффициент —
    None, если себестоимости нет."""
    got = payout(price, commission_percent)
    rub = got - _dec(cost_rub)
    coef = (got / _dec(cost_rub)) if cost_rub and _dec(cost_rub) > 0 else None
    return (rub.quantize(Decimal("0.01"), ROUND_HALF_UP),
            coef.quantize(Decimal("0.01"), ROUND_HALF_UP) if coef is not None else None)


def price_for_coef(cost_rub: Decimal, coef, commission_percent) -> Decimal:
    """Цена, при которой к получению = себестоимость × коэффициент."""
    return _dec(cost_rub) * _dec(coef) / (1 - _dec(commission_percent) / 100)


def floor_price(cost_rub: Decimal, rule: PlatformRule, commission_percent) -> int:
    """Минимально допустимая цена, ₽ (вверх до рубля)."""
    return math.ceil(price_for_coef(cost_rub, rule.min_markup_coef, commission_percent))


def _ru(v) -> str:
    """Коэффициент для текста: 2,5 а не 2.500."""
    return f"{_dec(v).normalize():f}".replace(".", ",")


def change_percent(old: int | None, new: int) -> float | None:
    if not old:
        return None
    return (new - old) * 100.0 / old


@dataclass
class Decision:
    new_price: int | None              # None — посчитать нельзя (см. note)
    cost_rub: Decimal | None = None
    markup_rub: Decimal | None = None
    markup_coef: Decimal | None = None
    source: str = "rule"               # rule / manual
    block_reason: str | None = None
    note: str = ""


def rule_price(cost_rub: Decimal | None, rule: PlatformRule | None) -> tuple[int | None, str]:
    """Цена площадки по её СОБСТВЕННОМУ правилу (без базы и ручных цен) — так
    считается базовая площадка для тех, кто берёт цену от неё."""
    if rule is None:
        return None, "нет правила площадки"
    if rule.commission_percent is None:
        return None, "у площадки не задана комиссия"
    c = _dec(rule.commission_percent)
    if c < 0 or c >= 100:
        return None, f"комиссия {c}% вне 0–99,99"
    if cost_rub is None:
        return None, "нет себестоимости из 1С"
    if _dec(rule.markup_coef) <= 1:
        # Правило по умолчанию — это «не настроено», а не «продавать по себестоимости».
        return None, "правило площадки не настроено (коэффициент 1)"
    return round_price(price_for_coef(cost_rub, rule.markup_coef, c), rule.round_step, rule.round_minus), ""


def decide(cost_usd, usd_rub, commission_percent, rule: PlatformRule,
           manual_price: int | None, last_sent_price: int | None,
           base_rule: PlatformRule | None = None, base_price: int | None = None) -> Decision:
    """Какую цену предложить и пропускают ли её ограничители. `base_rule` —
    правило базовой площадки, если цена берётся от неё; `base_price` — базовая
    цена товара, если правило берёт цену от неё (`base_platform == BASE`)."""
    if commission_percent is None:
        return Decision(None, note="у площадки не задана комиссия")
    c = _dec(commission_percent)
    if c < 0 or c >= 100:
        return Decision(None, note=f"комиссия {c}% вне 0–99,99")
    cost_rub = None
    if cost_usd is not None and _dec(cost_usd) > 0:
        if usd_rub is None:
            return Decision(None, note="нет курса доллара")
        cost_rub = (_dec(cost_usd) * _dec(usd_rub)).quantize(Decimal("0.01"))

    if manual_price is not None:
        d = Decision(int(manual_price), cost_rub=cost_rub, source="manual")
    elif cost_rub is None:
        return Decision(None, note="нет себестоимости из 1С")
    elif rule.base_platform:
        if rule.base_platform == BASE:
            base, why = base_price, "у товара не задана базовая цена"
        else:
            base, why = rule_price(cost_rub, base_rule)
        if base is None:
            what = "базовая цена" if rule.base_platform == BASE else "базовая площадка"
            return Decision(None, cost_rub=cost_rub, note=f"{what}: {why}")
        coef = _dec(rule.base_coef or 1)
        d = Decision(round_price(_dec(base) * coef, rule.round_step, rule.round_minus),
                     cost_rub=cost_rub, source="base")
        d.note = f"от базовой {base} ₽ × {_ru(coef)}"
    elif _dec(rule.markup_coef) <= 1:
        return Decision(None, cost_rub=cost_rub, note="правило площадки не настроено (коэффициент 1)")
    else:
        d = Decision(round_price(price_for_coef(cost_rub, rule.markup_coef, c),
                                 rule.round_step, rule.round_minus), cost_rub=cost_rub)

    if d.new_price <= 0:
        return Decision(None, cost_rub=cost_rub, source=d.source, note="цена должна быть больше нуля")
    if cost_rub is not None:
        d.markup_rub, d.markup_coef = markup(d.new_price, cost_rub, c)
        floor = floor_price(cost_rub, rule, c)
        if d.new_price < floor:
            d.block_reason = BLOCK_FLOOR
            d.note = (f"минимум {floor} ₽: коэффициент {_ru(d.markup_coef)} при минимальном "
                      f"{_ru(rule.min_markup_coef)}")
            return d
    elif d.source == "manual":
        d.note = "себестоимости нет — пол не проверен"

    pct = change_percent(last_sent_price, d.new_price)
    if pct is not None and abs(pct) > float(_dec(rule.max_change_percent)):
        d.block_reason = BLOCK_MAX_CHANGE
        d.note = "; ".join(x for x in (d.note, f"изменение {pct:+.1f}% при лимите {rule.max_change_percent}%") if x)
    return d


def get_rule(db: Session, platform: str) -> PlatformRule:
    rule = db.query(PlatformRule).filter(PlatformRule.platform == platform).first()
    if rule is None:
        rule = PlatformRule(platform=platform, markup_coef=1, round_step=1, round_minus=0,
                            min_markup_coef=1, max_change_percent=20)
        db.add(rule)
        db.flush()   # autoflush выключен: второй вызов иначе завёл бы дубль
    return rule


def rules_for(db: Session, platform: str) -> tuple[PlatformRule, PlatformRule | None]:
    """(правило площадки, правило её базы или None)."""
    rule = get_rule(db, platform)
    if not rule.base_platform or rule.base_platform == BASE:
        return rule, None
    return rule, get_rule(db, rule.base_platform)


@dataclass
class Inputs:
    """Всё, что нужно расчёту по площадке, кроме самого товара: правила и цены,
    заданные человеком на уровне товара (базовая) и площадки. Один источник на
    все пути расчёта — разойдись они, страница показывала бы одну цену, а в
    предложение уходила бы другая."""
    rule: PlatformRule
    base_rule: PlatformRule | None
    base_prices: dict
    platform_prices: dict


def load_inputs(db: Session, platform: str, rule: PlatformRule | None = None,
                base_rule: PlatformRule | None = None) -> Inputs:
    if rule is None:
        rule, base_rule = rules_for(db, platform)
    return Inputs(rule, base_rule,
                  {b.item_id: b.price for b in db.query(BasePrice)},
                  {p.item_id: p.price for p in db.query(PlatformPrice).filter(PlatformPrice.platform == platform)})


def decide_for(inp: Inputs, item_id: str, cost_usd, usd_rub, pp: "ProductPrice | None") -> Decision:
    """Решение по товару в кабинете. Ручная цена КАБИНЕТА главнее цены
    ПЛОЩАДКИ, та — правила; пол и лимит шага не обходит ни одна."""
    manual = pp.manual_price if pp and pp.manual_price is not None else None
    from_platform = manual is None and item_id in inp.platform_prices
    if from_platform:
        manual = inp.platform_prices[item_id]
    d = decide(cost_usd, usd_rub, inp.rule.commission_percent, inp.rule, manual,
               pp.last_sent_price if pp else None, inp.base_rule, inp.base_prices.get(item_id))
    if from_platform and d.source == "manual":
        d.source = "platform"
    return d


@dataclass
class Stats:
    proposed: int = 0
    blocked: int = 0
    unchanged: int = 0
    skipped: int = 0
    reasons: dict = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def recalculate_account(db: Session, account: Account, is_test: bool = False) -> Stats:
    """Пересчёт предложений по кабинету (правило — его площадки) — по всем SKU 1С, сопоставленным с его
    каталогом (`mapping.account_items`). Прежние нерешённые предложения
    вытесняются; подтверждённые и ещё не отправленные не трогаются. Цена, равная
    последней принятой, предложения не получает."""
    from priceapp import mapping

    stats = Stats()
    inp = load_inputs(db, account.platform)
    rule = inp.rule
    rate = rates.current(db)
    for old in db.query(PriceChange).filter(PriceChange.account_id == account.id,
                                            PriceChange.is_test.is_(is_test),
                                            PriceChange.status.in_(OPEN)):
        old.status = PriceChangeStatus.rejected.value
        old.note = "вытеснено новым расчётом"

    items = mapping.account_items(db, account.id)
    costs = {c.item_id: c for c in db.query(OnecCost).filter(OnecCost.item_id.in_(list(items)))}
    prices = {p.item_id: p for p in db.query(ProductPrice).filter(ProductPrice.account_id == account.id)}
    for item_id, rows in items.items():
        pp = prices.get(item_id)
        last = pp.last_sent_price if pp else None
        cost = costs.get(item_id)
        d = decide_for(inp, item_id, cost.cost_usd if cost else None, rate.usd_rub if rate else None, pp)
        if d.new_price is None:
            stats.skip(d.note)
            continue
        min_price = max((r.min_price for r in rows if r.min_price), default=None)
        if min_price and d.new_price < min_price:
            d.note = "; ".join(x for x in (d.note, f"ниже минимальной цены площадки {min_price} ₽") if x)
        if last == d.new_price:
            stats.unchanged += 1
            continue
        db.add(PriceChange(
            item_id=item_id, account_id=account.id, barcode=rows[0].barcode,
            cost_usd=cost.cost_usd if cost else None, usd_rub=rate.usd_rub if rate else None,
            cost_rub=d.cost_rub, commission_percent=rule.commission_percent,
            old_price=last, new_price=d.new_price, markup_rub=d.markup_rub,
            markup_coef=d.markup_coef, source=d.source,
            status=PriceChangeStatus.blocked.value if d.block_reason else PriceChangeStatus.proposed.value,
            block_reason=d.block_reason, note=(d.note or None) and d.note[:255], is_test=is_test,
        ))
        if d.block_reason:
            stats.blocked += 1
        else:
            stats.proposed += 1
    db.commit()
    return stats


def approve(change: PriceChange, actor: str, confirm_large: bool = False) -> str | None:
    """Подтверждение одного предложения. Возвращает текст отказа или None.
    Пол не обходится никогда; лимит шага — только с confirm_large."""
    if change.status not in OPEN:
        return "уже решено"
    if change.block_reason == BLOCK_FLOOR:
        return BLOCK_LABELS[BLOCK_FLOOR]
    if change.block_reason == BLOCK_MAX_CHANGE and not confirm_large:
        return BLOCK_LABELS[BLOCK_MAX_CHANGE]
    change.status = PriceChangeStatus.approved.value
    change.decided_by = actor
    change.decided_at = now_utc()
    return None


def propose_price(db: Session, account: Account, item_id: str, price: int, source: str,
                  note: str, actor: str = "") -> PriceChange | None:
    """Предложение с заданной ценой (откат к прежней). Идёт через `decide`, то есть
    через пол и лимит шага, как ручная цена, и само НИЧЕГО не отправляет: дальше
    обычное подтверждение. Прежнее нерешённое по этой паре вытесняется."""
    from priceapp import mapping
    rows = mapping.account_items(db, account.id).get(item_id)
    if not rows:
        return None
    rule, base_rule = rules_for(db, account.platform)
    rate = rates.current(db)
    cost = db.get(OnecCost, item_id)
    pp = db.query(ProductPrice).filter(ProductPrice.item_id == item_id,
                                       ProductPrice.account_id == account.id).first()
    last = pp.last_sent_price if pp else None
    d = decide(cost.cost_usd if cost else None, rate.usd_rub if rate else None, rule.commission_percent,
               rule, price, last, base_rule)
    if d.new_price is None:
        return None
    for old in db.query(PriceChange).filter(PriceChange.account_id == account.id, PriceChange.item_id == item_id,
                                            PriceChange.is_test.is_(False), PriceChange.status.in_(OPEN)):
        old.status = PriceChangeStatus.rejected.value
        old.note = "вытеснено предложением вернуть прежнюю цену"
    ch = PriceChange(item_id=item_id, account_id=account.id, barcode=rows[0].barcode,
                     cost_usd=cost.cost_usd if cost else None, usd_rub=rate.usd_rub if rate else None,
                     cost_rub=d.cost_rub, commission_percent=rule.commission_percent, old_price=last,
                     new_price=d.new_price, markup_rub=d.markup_rub, markup_coef=d.markup_coef, source=source,
                     status=PriceChangeStatus.blocked.value if d.block_reason else PriceChangeStatus.proposed.value,
                     block_reason=d.block_reason, note="; ".join(x for x in (note, d.note) if x)[:255])
    db.add(ch)
    db.flush()
    return ch


@dataclass
class Preview:
    platform: str
    accounts: int = 0
    priced_before: int = 0
    priced_after: int = 0
    changed: int = 0
    up: int = 0
    down: int = 0
    floor: int = 0
    big_step: int = 0
    avg_change: float | None = None


def preview_rule(db: Session, platform: str, values: dict) -> list[Preview]:
    """«Что будет, если» сохранить правило `values` для площадки: по всем её
    кабинетам и по площадкам, которые берут цену от неё. Ничего не пишет:
    новое правило — несохранённый объект вне сессии."""
    from priceapp import mapping
    new = PlatformRule(platform=platform, **values)
    new_base = (get_rule(db, new.base_platform) if new.base_platform and new.base_platform != BASE else None)
    rate = rates.current(db)
    usd = rate.usd_rub if rate else None
    out = []
    affected = [platform] + [r.platform for r in db.query(PlatformRule).filter(PlatformRule.base_platform == platform)]
    for p in affected:
        old_inp = load_inputs(db, p)
        new_inp = (load_inputs(db, p, new, new_base) if p == platform
                   else load_inputs(db, p, old_inp.rule, new))
        pv = Preview(platform=p)
        pcts = []
        for acc in db.query(Account).filter(Account.platform == p, Account.is_active.is_(True)):
            pv.accounts += 1
            items = mapping.account_items(db, acc.id)
            costs = {c.item_id: c for c in db.query(OnecCost).filter(OnecCost.item_id.in_(list(items)))}
            prices = {x.item_id: x for x in db.query(ProductPrice).filter(ProductPrice.account_id == acc.id)}
            for item_id in items:
                cost, pp = costs.get(item_id), prices.get(item_id)
                a = decide_for(old_inp, item_id, cost.cost_usd if cost else None, usd, pp)
                b = decide_for(new_inp, item_id, cost.cost_usd if cost else None, usd, pp)
                pv.priced_before += a.new_price is not None
                pv.priced_after += b.new_price is not None
                if b.new_price is None:
                    continue
                if b.block_reason == BLOCK_FLOOR:
                    pv.floor += 1
                elif b.block_reason == BLOCK_MAX_CHANGE:
                    pv.big_step += 1
                if a.new_price != b.new_price:
                    pv.changed += 1
                    if a.new_price:
                        pv.up += b.new_price > a.new_price
                        pv.down += b.new_price < a.new_price
                        pcts.append((b.new_price - a.new_price) * 100.0 / a.new_price)
        pv.avg_change = sum(pcts) / len(pcts) if pcts else None
        out.append(pv)
    db.expunge(new) if new in db else None
    return out

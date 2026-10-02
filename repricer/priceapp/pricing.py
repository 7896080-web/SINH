"""Расчёт цены и ограничители.

Экономика (задана заказчиком):
    себестоимость, ₽  = себестоимость 1С, $ × курс ЦБ        — это и есть БАЗОВАЯ цена
    цена, ₽           = то, что мы ставим на площадке (вверх до шага, «красивое» окончание)
    платит покупатель = цена × (1 − скидка продавца%)
    к получению, ₽    = платит покупатель × (1 − комиссия%)
    маржинальность    = к получению / себестоимость, ₽   (2 = +100%)
    комиссия, %       = тариф площадки по категории товара (или из правила) + надбавка

Базовая цена руками не правится. Человек задаёт НАЦЕНКУ — коэффициентом от
базовой (цена = базовая × k) или маржинальностью (цена такая, чтобы
маржинальность была m при ТЕКУЩИХ комиссии и скидке: тариф сменился — цена
пересчитается, маржинальность останется). Наценку задают на артикул 1С (все его
размеры сразу) или на категорию площадки, по кабинету или по всей площадке.
Главнее всех — артикул в кабинете, затем артикул на площадке, категория в
кабинете, категория на площадке, умолчание площадки (`target_for`).

Ограничители (обойти расчётом нельзя):
  * ПОЛ — маржинальность не ниже `min_markup_coef`. Нарушение не отправляется ни
    подтверждением, ни ручной ценой, и перепроверяется при отправке по ТЕКУЩИМ
    курсу, себестоимости и комиссии.
  * ЛИМИТ ШАГА — изменение больше `max_change_percent` от последней ПРИНЯТОЙ
    площадкой цены — только отдельным подтверждением.
  * Цена никогда не уходит сама: её передаёт человек.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from priceapp import rates
from priceapp.models import (Account, ArticleCoef, CategoryTarget, OnecBarcode, OnecCost, PlatformRule, PriceChange,
                             PriceChangeStatus, ProductPrice)
from priceapp.timeutils import now_utc

# Откуда взят коэффициент: свой у артикула в кабинете, у артикула на площадке,
# умолчание площадки. И ручная цена кабинета — она главнее любого коэффициента.
SRC_CABINET, SRC_ARTICLE, SRC_DEFAULT, SRC_MANUAL = "cabinet", "article", "rule", "manual"
SRC_CAT_CABINET, SRC_CATEGORY = "cat_cabinet", "category"
SOURCE_LABELS = {SRC_CABINET: "артикул, кабинет", SRC_ARTICLE: "артикул, площадка",
                 SRC_CAT_CABINET: "категория, кабинет", SRC_CATEGORY: "категория, площадка",
                 SRC_DEFAULT: "по умолчанию площадки", SRC_MANUAL: "ручная цена кабинета"}
KIND_COEF, KIND_MARGIN = "coef", "margin"
KIND_LABELS = {KIND_COEF: "коэффициент от базовой", KIND_MARGIN: "маржинальность"}

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


def floor_price(cost_rub: Decimal, rule: PlatformRule, commission_percent, discount=0) -> int:
    """Минимально допустимая цена, ₽ (вверх до рубля): та, при которой
    маржинальность — с учётом скидки продавца — равна полу."""
    return math.ceil(price_for_coef(cost_rub, rule.min_markup_coef, commission_percent)
                     / (1 - _dec(discount)))


def _ru(v) -> str:
    """Коэффициент для текста: 2,5 а не 2.500."""
    return f"{_dec(v).normalize():f}".replace(".", ",")


def change_percent(old: int | None, new: int) -> float | None:
    if not old:
        return None
    return (new - old) * 100.0 / old


def base_price(cost_usd, usd_rub) -> Decimal | None:
    """Базовая цена, ₽ = себестоимость 1С × курс. None — нет одного из двух."""
    if cost_usd is None or _dec(cost_usd) <= 0 or usd_rub is None:
        return None
    return (_dec(cost_usd) * _dec(usd_rub)).quantize(Decimal("0.01"))


@dataclass
class Target:
    kind: str             # KIND_COEF / KIND_MARGIN
    value: Decimal
    source: str


@dataclass
class Facts:
    """Что известно о товаре на площадке: комиссия (тариф + надбавка), скидка
    продавца (доля), текущие цены. Без строк каталога — только правило."""
    commission: Decimal | None
    tariff: Decimal | None = None
    discount: Decimal = Decimal(0)
    current: int | None = None
    sale: int | None = None
    category: str = ""


def item_facts(rule: PlatformRule, rows=None) -> Facts:
    """Комиссия товара: тариф площадки по его категории (наибольший среди строк
    каталога — у карточки бывает несколько баркодов) или комиссия из правила, плюс
    надбавка. Скидка продавца — по строке с наибольшей текущей ценой (её и
    отправляем на карточку)."""
    rows = [r for r in rows or [] if r is not None]
    attr = "tariff_fbo" if (rule.tariff_model or "fbs") == "fbo" else "tariff_fbs"
    tariffs = [_dec(getattr(r, attr)) for r in rows if getattr(r, attr, None) is not None]
    tariff = max(tariffs) if tariffs else None
    base = tariff if tariff is not None else rule.commission_percent
    commission = (_dec(base) + _dec(rule.commission_extra or 0)) if base is not None else None
    priced = [r for r in rows if getattr(r, "current_price", None)]
    cur = max(priced, key=lambda r: r.current_price) if priced else None
    current = cur.current_price if cur else None
    sale = (cur.current_sale_price or current) if cur else None
    disc = (Decimal(1) - Decimal(sale) / Decimal(current)) if current and sale and sale < current else Decimal(0)
    cats = [r.category for r in rows if getattr(r, "category", "")]
    return Facts(commission, tariff, disc, current, sale, cats[0] if cats else "")


@dataclass
class Decision:
    new_price: int | None              # None — посчитать нельзя (см. note)
    cost_rub: Decimal | None = None    # она же базовая цена
    markup_rub: Decimal | None = None
    markup_coef: Decimal | None = None  # маржинальность: к получению / себестоимость (со скидкой)
    coef: Decimal | None = None        # чем задана наценка (значение)
    source: str = SRC_DEFAULT
    block_reason: str | None = None
    note: str = ""
    kind: str = KIND_COEF
    commission: Decimal | None = None
    discount: Decimal = Decimal(0)


def decide(cost_usd, usd_rub, rule: PlatformRule, target: "Target | None",
           manual_price: int | None, last_sent_price: int | None,
           facts: "Facts | None" = None) -> Decision:
    """Какую цену предложить и пропускают ли её ограничители. `facts` — комиссия
    и скидка товара; без них — комиссия из правила и скидки нет."""
    facts = facts or item_facts(rule)
    if facts.commission is None:
        return Decision(None, note="у площадки не задана комиссия")
    c = facts.commission
    if c < 0 or c >= 100:
        return Decision(None, note=f"комиссия {c}% вне 0–99,99")
    disc = facts.discount
    if cost_usd is not None and _dec(cost_usd) > 0 and usd_rub is None:
        return Decision(None, note="нет курса доллара")
    cost_rub = base_price(cost_usd, usd_rub)

    if manual_price is not None:
        d = Decision(int(manual_price), cost_rub=cost_rub, source=SRC_MANUAL)
    elif cost_rub is None:
        return Decision(None, note="нет себестоимости из 1С")
    elif target is None:
        return Decision(None, cost_rub=cost_rub,
                        note="наценка не задана ни у артикула, ни у категории, ни по умолчанию у площадки")
    else:
        if target.kind == KIND_MARGIN:
            raw = cost_rub * target.value / ((1 - c / 100) * (1 - disc))
        else:
            raw = cost_rub * target.value
        d = Decision(round_price(raw, rule.round_step, rule.round_minus), cost_rub=cost_rub,
                     coef=target.value, source=target.source, kind=target.kind)
    d.commission, d.discount = c, disc

    if d.new_price <= 0:
        return Decision(None, cost_rub=cost_rub, source=d.source, note="цена должна быть больше нуля")
    if cost_rub is not None:
        d.markup_rub, d.markup_coef = markup(Decimal(d.new_price) * (1 - disc), cost_rub, c)
        floor = floor_price(cost_rub, rule, c, disc)
        if d.new_price < floor:
            d.block_reason = BLOCK_FLOOR
            d.note = (f"минимум {floor} ₽: маржинальность {_ru(d.markup_coef)} при минимальной "
                      f"{_ru(rule.min_markup_coef)}" + (f" (скидка продавца {_ru((disc * 100).quantize(Decimal('1')))}%)"
                                                       if disc else ""))
            return d
    elif d.source == SRC_MANUAL:
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


def article_map(db: Session) -> dict[str, str]:
    """SKU 1С -> артикул 1С. Коэффициент живёт на артикуле; SKU без артикула
    отвечает сам за себя (ключ — его ID)."""
    out: dict[str, str] = {}
    for item_id, article in db.query(OnecBarcode.item_id, OnecBarcode.article):
        if item_id not in out or (not out[item_id] and article):
            out[item_id] = (article or "").strip()
    return out


def article_of(articles: dict[str, str], item_id: str) -> str:
    return articles.get(item_id) or item_id


@dataclass
class Inputs:
    """Всё, что нужно расчёту по площадке, кроме самого товара. Один источник на
    все пути расчёта — разойдись они, страница показывала бы одну цену, а в
    предложение и на площадку уходила бы другая."""
    rule: PlatformRule
    coefs: dict          # (артикул, account_id | 0) -> (kind, value)
    articles: dict       # item_id -> артикул
    categories: dict = field(default_factory=dict)   # (категория, account_id | 0) -> (kind, value)


def load_inputs(db: Session, platform: str, rule: PlatformRule | None = None,
                articles: dict | None = None) -> Inputs:
    return Inputs(rule if rule is not None else get_rule(db, platform),
                  {(c.article, c.account_id): (c.kind or KIND_COEF, _dec(c.coef))
                   for c in db.query(ArticleCoef).filter(ArticleCoef.platform == platform)},
                  articles if articles is not None else article_map(db),
                  {(c.category, c.account_id): (c.kind or KIND_MARGIN, _dec(c.value))
                   for c in db.query(CategoryTarget).filter(CategoryTarget.platform == platform)})


def target_for(inp: Inputs, item_id: str, account_id: int | None, category: str = "") -> Target | None:
    """Наценка товара: артикул в кабинете, артикул на площадке, категория в
    кабинете, категория на площадке, умолчание площадки. `account_id` None —
    спрашиваем про площадку целиком (кабинетные не учитываются)."""
    art = article_of(inp.articles, item_id)
    steps = []
    if account_id:
        steps.append((inp.coefs, (art, account_id), SRC_CABINET))
    steps.append((inp.coefs, (art, 0), SRC_ARTICLE))
    if category:
        if account_id:
            steps.append((inp.categories, (category, account_id), SRC_CAT_CABINET))
        steps.append((inp.categories, (category, 0), SRC_CATEGORY))
    for table, key, src in steps:
        if key in table:
            kind, value = table[key]
            return Target(kind, value, src)
    if inp.rule.base_coef is not None:
        return Target(KIND_COEF, _dec(inp.rule.base_coef), SRC_DEFAULT)
    return None


def coef_for(inp: Inputs, item_id: str, account_id: int | None, category: str = "") -> tuple[Decimal | None, str]:
    """(значение наценки, откуда) — для показа; вид наценки — у `target_for`."""
    t = target_for(inp, item_id, account_id, category)
    return (t.value, t.source) if t else (None, SRC_DEFAULT)


def decide_for(inp: Inputs, item_id: str, account_id: int | None, cost_usd, usd_rub,
               pp: "ProductPrice | None", rows=None) -> Decision:
    """Решение по товару в кабинете. `rows` — его строки каталога кабинета:
    из них тариф комиссии, скидка продавца и категория. Ручная цена кабинета
    главнее наценки; пол и лимит шага не обходит ни одна."""
    facts = item_facts(inp.rule, rows)
    manual = pp.manual_price if pp and pp.manual_price is not None else None
    target = target_for(inp, item_id, account_id, facts.category)
    # Лимит шага — от последней принятой нами цены, а пока мы ничего не
    # отправляли — от ТЕКУЩЕЙ на площадке: иначе первая же отправка (опечатка в
    # наценке, ×10 в себестоимости 1С) ушла бы без всякой проверки шага.
    last = (pp.last_sent_price if pp and pp.last_sent_price else None) or facts.current
    return decide(cost_usd, usd_rub, inp.rule, target, manual, last, facts)


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
        d = decide_for(inp, item_id, account.id, cost.cost_usd if cost else None,
                       rate.usd_rub if rate else None, pp, rows)
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
            cost_rub=d.cost_rub, commission_percent=d.commission,
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
    rule = get_rule(db, account.platform)
    rate = rates.current(db)
    cost = db.get(OnecCost, item_id)
    pp = db.query(ProductPrice).filter(ProductPrice.item_id == item_id,
                                       ProductPrice.account_id == account.id).first()
    last = pp.last_sent_price if pp else None
    d = decide(cost.cost_usd if cost else None, rate.usd_rub if rate else None, rule, None,
               price, last, item_facts(rule, rows))
    if d.new_price is None:
        return None
    for old in db.query(PriceChange).filter(PriceChange.account_id == account.id, PriceChange.item_id == item_id,
                                            PriceChange.is_test.is_(False), PriceChange.status.in_(OPEN)):
        old.status = PriceChangeStatus.rejected.value
        old.note = "вытеснено предложением вернуть прежнюю цену"
    ch = PriceChange(item_id=item_id, account_id=account.id, barcode=rows[0].barcode,
                     cost_usd=cost.cost_usd if cost else None, usd_rub=rate.usd_rub if rate else None,
                     cost_rub=d.cost_rub, commission_percent=d.commission, old_price=last,
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
    """«Что будет, если» сохранить правило `values` для площадки — по всем её
    кабинетам. Ничего не пишет: новое правило — несохранённый объект вне сессии."""
    from priceapp import mapping
    new = PlatformRule(platform=platform, **values)
    rate = rates.current(db)
    usd = rate.usd_rub if rate else None
    old_inp = load_inputs(db, platform)
    new_inp = Inputs(new, old_inp.coefs, old_inp.articles, old_inp.categories)
    pv = Preview(platform=platform)
    pcts = []
    for acc in db.query(Account).filter(Account.platform == platform, Account.is_active.is_(True)):
        pv.accounts += 1
        items = mapping.account_items(db, acc.id)
        costs = {c.item_id: c for c in db.query(OnecCost).filter(OnecCost.item_id.in_(list(items)))}
        prices = {x.item_id: x for x in db.query(ProductPrice).filter(ProductPrice.account_id == acc.id)}
        for item_id, rows in items.items():
            cost, pp = costs.get(item_id), prices.get(item_id)
            a = decide_for(old_inp, item_id, acc.id, cost.cost_usd if cost else None, usd, pp, rows)
            b = decide_for(new_inp, item_id, acc.id, cost.cost_usd if cost else None, usd, pp, rows)
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
    return [pv]

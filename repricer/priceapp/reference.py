"""Эталонная цена кабинетов WB и её защита.

Эталонная цена SKU — ПОСЛЕДНЯЯ цена, которую программа отправила на карточку
(`dispatch.run_account` записывает её сюда после каждой принятой площадкой отправки;
цены, ушедшие до появления правила, подхватывает `adopt_sent`). Две части правила:

1. «Площадка снизила цену». «К получению» по ТЕКУЩЕЙ цене площадки (с её скидкой
   продавца и комиссией) ниже «к получению» по эталонной больше чем на `ref_drop_percent`
   (10%) — эталонная цена ВОЗВРАЩАЕТСЯ: программа сама ставит её в отправку
   (`PriceChange` со статусом approved, источник «reference»; уходит так же, как любая
   подтверждённая цена). Только если цена на площадке (в списке) ниже эталонной — вернуть
   можно только вверх. Если цена в списке не тронута, а «к получению» упало из-за скидки
   продавца или акции, цена не помогает: это «внимание», не отправка.
2. «Курс». Курс ушёл от курса, по которому посчитана эталонная, больше чем на
   `ref_rate_percent` (2%) — эталонная пересчитывается по наценке на ТЕКУЩЕМ курсе, затем
   работает правило 1. Сама по себе цена на площадке при этом не меняется: уйдёт она,
   только если «к получению» уже ниже новой эталонной больше чем на 10%.

Только кабинеты WB. Опрос площадки — при запуске программы и раз в 6 часов
(`workers/jobs.job_wb_poll`), а также сразу, когда курс ушёл за порог.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from priceapp import audit, rates, settings
from priceapp.models import Account, PriceChange, PriceChangeStatus, ReferencePrice
from priceapp.pricing import BLOCK_FLOOR, _dec, get_rule, item_facts, markup, payout
from priceapp.timeutils import now_utc

SOURCE = "reference"
PLATFORMS = ("wb",)
COMMIT_EVERY = 500


def enabled(db: Session) -> bool:
    return settings.get(db, settings.REF_ENABLED) != "0"


def _pct(db: Session, key: str, default: str) -> Decimal:
    raw = settings.get(db, key) or default
    try:
        v = Decimal(raw.replace(",", "."))
    except Exception:
        v = Decimal(default)
    return v if v > 0 else Decimal(default)


def drop_percent(db: Session) -> Decimal:
    return _pct(db, settings.REF_DROP_PERCENT, "10")


def rate_percent(db: Session) -> Decimal:
    return _pct(db, settings.REF_RATE_PERCENT, "2")


def record(db: Session, account: Account, item_id: str, price: int, rows, rule=None, rate=None,
           source: str = "sent") -> bool:
    """Записать эталонную цену SKU: `price` — отправленная на карточку цена в списке;
    `rows` — строки каталога SKU в кабинете (из них комиссия и скидка продавца).
    Не коммитит. Только WB; без комиссии «к получению» не посчитать — эталон
    без него бесполезен, строка не пишется."""
    if account.platform not in PLATFORMS or not price:
        return False
    rule = rule or get_rule(db, account.platform)
    facts = item_facts(rule, rows)
    if facts.commission is None:
        return False
    disc = facts.discount or Decimal(0)
    got = payout(Decimal(int(price)) * (1 - disc), facts.commission)
    rate = rate if rate is not None else rates.current(db)
    usd = rate.usd_rub if rate is not None else None
    row = (db.query(ReferencePrice).filter(ReferencePrice.account_id == account.id,
                                           ReferencePrice.item_id == item_id).first())
    if row is None:
        row = ReferencePrice(account_id=account.id, item_id=item_id, ref_price=int(price))
        db.add(row)
    row.ref_price = int(price)
    row.ref_payout = got.quantize(Decimal("0.01"))
    row.commission_percent = facts.commission
    row.discount = disc.quantize(Decimal("0.0001"))
    row.usd_rub = usd
    row.source = source
    row.set_at = now_utc()
    db.flush()
    return True


def adopt_sent(db: Session, account: Account | None = None) -> dict:
    """Эталонными делает уже отправленные цены (`ProductPrice.last_sent_price`):
    для цен, ушедших ДО появления правила, или после ручного сброса. Уже записанный
    эталон перезаписывается последней отправленной ценой. Коммитит."""
    from priceapp import mapping
    from priceapp.models import ProductPrice
    st = {"accounts": 0, "recorded": 0, "skipped": 0}
    rate = rates.current(db)
    accs = [account] if account is not None else (
        db.query(Account).filter(Account.is_active.is_(True), Account.platform.in_(PLATFORMS)).all())
    for a in accs:
        if a.platform not in PLATFORMS:
            continue
        st["accounts"] += 1
        rule = get_rule(db, a.platform)
        items = mapping.account_items(db, a.id)
        n = 0
        for pp in db.query(ProductPrice).filter(ProductPrice.account_id == a.id,
                                                ProductPrice.last_sent_price.isnot(None)):
            rows = items.get(pp.item_id)
            if rows and record(db, a, pp.item_id, pp.last_sent_price, rows, rule, rate, source="adopt"):
                st["recorded"] += 1
                n += 1
                if st["recorded"] % COMMIT_EVERY == 0:
                    db.commit()
            else:
                st["skipped"] += 1
        if n:
            audit.log(db, "программа", "reference_adopt", a.name, f"эталонных цен записано: {n}")
        db.commit()
    return st


def rate_moved(db: Session) -> bool:
    """Есть эталонная цена, посчитанная по курсу, который ушёл от текущего дальше порога."""
    cur = rates.current(db)
    if cur is None:
        return False
    thr = rate_percent(db) / 100
    seen = {_dec(r) for (r,) in db.query(ReferencePrice.usd_rub).distinct() if r}
    return any(abs(cur.usd_rub / v - 1) > thr for v in seen)


def classify(r: dict, ref: ReferencePrice, drop: Decimal) -> str | None:
    """`below` — цену вернуть; `eaten` — «к получению» упало, а цена в списке не ниже
    эталонной (скидка или акция) — отправка не поможет; None — всё в порядке."""
    cur_payout = r.get("current_payout")
    if cur_payout is None or ref.ref_payout is None:
        return None
    if _dec(cur_payout) >= _dec(ref.ref_payout) * (1 - drop / 100):
        return None
    cur = r.get("current")
    if cur and cur < ref.ref_price and r.get("block_reason") != BLOCK_FLOOR:
        return "below"
    return "eaten"


def _clear_poll_flag(db: Session) -> None:
    settings.put(db, settings.REF_POLL_NOW, "0")


def run(db: Session, product_rows=None, actor: str = "программа") -> dict:
    """Пересчёт эталонных по курсу и возврат цен, которые площадка занизила.
    Работает по уже загруженным текущим ценам — к площадкам не обращается. Коммит порциями."""
    if product_rows is None:
        from priceapp.routers.prices import product_rows
    st = {"accounts": 0, "revalued": 0, "restored": 0, "eaten": 0, "disabled": False}
    if not enabled(db):
        st["disabled"] = True
        return st
    rate = rates.current(db)
    rate_thr = rate_percent(db) / 100
    drop = drop_percent(db)
    now = now_utc()
    for a in db.query(Account).filter(Account.is_active.is_(True), Account.platform.in_(PLATFORMS)).all():
        refs = {r.item_id: r for r in db.query(ReferencePrice).filter(ReferencePrice.account_id == a.id)}
        if not refs:
            continue
        st["accounts"] += 1
        busy = {i for (i,) in db.query(PriceChange.item_id).filter(
            PriceChange.account_id == a.id, PriceChange.is_test.is_(False),
            PriceChange.status == PriceChangeStatus.approved.value)}
        restored = revalued = 0
        for r in product_rows(db, a):
            ref = refs.get(r["item_id"])
            if ref is None:
                continue
            # 1. Курс ушёл за порог — эталонная пересчитывается по наценке на текущем курсе.
            if (rate is not None and ref.usd_rub and r.get("price") and r.get("payout") is not None
                    and abs(rate.usd_rub / _dec(ref.usd_rub) - 1) > rate_thr):
                ref.ref_price = int(r["price"])
                ref.ref_payout = _dec(r["payout"]).quantize(Decimal("0.01"))
                ref.usd_rub = rate.usd_rub
                ref.source = "rate"
                ref.set_at = now
                revalued += 1
            # 2. Площадка занизила цену — вернуть эталонную (только вверх).
            kind = classify(r, ref, drop)
            if kind == "eaten":
                st["eaten"] += 1
            if kind != "below" or r["item_id"] in busy:
                continue
            got = markup(Decimal(ref.ref_price) * (1 - _dec(ref.discount)), r["cost_rub"], _dec(ref.commission_percent)) \
                if r.get("cost_rub") and ref.commission_percent is not None else (None, None)
            db.add(PriceChange(
                item_id=r["item_id"], account_id=a.id, barcode=r["platform"].barcode,
                cost_usd=r["cost_usd"], usd_rub=rate.usd_rub if rate else None, cost_rub=r["cost_rub"],
                commission_percent=ref.commission_percent, old_price=r["current"], new_price=ref.ref_price,
                markup_rub=got[0], markup_coef=got[1], source=SOURCE,
                status=PriceChangeStatus.approved.value,
                note=(f"площадка держит {r['current']} ₽, к получению {_dec(r['current_payout']):.0f} ₽ ниже эталонных "
                      f"{_dec(ref.ref_payout):.0f} ₽ больше чем на {drop.normalize()}% — возврат эталонной цены "
                      f"{ref.ref_price} ₽")[:255],
                decided_by=actor, decided_at=now))
            busy.add(r["item_id"])
            restored += 1
            if (restored + revalued) % COMMIT_EVERY == 0:
                db.commit()
        st["restored"] += restored
        st["revalued"] += revalued
        if restored or revalued:
            audit.log(db, actor, "reference_run", a.name,
                      f"эталонных пересчитано по курсу: {revalued}; возвращено цен: {restored}")
        db.commit()
    return st


def summary(st: dict) -> str:
    if st.get("disabled"):
        return "Правило эталонной цены выключено (настройка ref_enabled)."
    if not st.get("accounts"):
        return "Эталонных цен WB пока нет: правило начнёт работать после первой отправки цен."
    parts = [f"Эталонные цены WB ({st['accounts']} каб.): пересчитано по курсу — {st['revalued']}; "
             f"возвращено цен — {st['restored']}"]
    if st["eaten"]:
        parts.append(f"«к получению» упало из-за скидки продавца или акции, цена не помогает — {st['eaten']}")
    return "; ".join(parts) + "."

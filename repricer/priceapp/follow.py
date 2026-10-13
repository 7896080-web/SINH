"""Цена Kit следует за ценой WB.

Правило: цена для покупателя на Kit = (100 − `follow_percent`)% от СРЕДНЕЙ цены для покупателя на WB
по артикулу 1С (среднее по карточкам всех кабинетов WB; цена для покупателя = цена продавца на площадке
после скидки × доля W/P из экономики карточки). Правило живёт как маржинальность артикула в кабинете Kit
(`ArticleCoef`, вид «маржинальность»): страница «Утверждение цен» и расчёт цен видят то же самое число.

Когда считается (`run`): при опросе WB (запуск программы, раз в 6 часов, при сдвиге курса), через
несколько минут после отправки цен на WB — автоматической или ручной — и при любой правке цены
площадкой: опрос заново загружает цены WB и Kit. Цены WB берутся ТЕКУЩИЕ, как их видит программа.

Что делается: маржинальность артикула пересчитывается, если средняя цена WB сместилась больше
чем на `follow_tolerance` (1%); затем обычный расчёт предложений по Kit; предложения
подтверждаются сами, если
* по товару мы уже отправляли цену (первую партию человек подтверждает руками);
* изменение к последней отправленной цене не больше `follow_max_step` (50%) — крупнее остаётся
  предложением на ручное подтверждение;
* пол наценки не нарушен (заблокированное не подтверждается никогда).
Подтверждённое уходит обычной отправкой (`dispatch.py`), где пол проверяется ещё раз.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

from sqlalchemy.orm import Session

from priceapp import audit, settings
from priceapp.models import Account, ArticleCoef, PriceChange, PriceChangeStatus, ProductPrice
from priceapp.pricing import approve, get_rule, recalculate_account

logger = logging.getLogger("repricer.follow")

SOURCE = "follow"
SOURCE_PLATFORMS = ("wb",)
TARGET_PLATFORMS = ("kit",)
ACTOR = "программа (Kit следует за WB)"
USER = SimpleNamespace(username=ACTOR)


def enabled(db: Session) -> bool:
    return settings.get(db, settings.FOLLOW_ENABLED) != "0"


def _pct(db: Session, key: str, default: str) -> Decimal:
    raw = (settings.get(db, key) or default).replace(",", ".")
    try:
        v = Decimal(raw)
    except Exception:
        v = Decimal(default)
    return v if v > 0 else Decimal(default)


def percent(db: Session) -> Decimal:
    return _pct(db, settings.FOLLOW_PERCENT, "10")


def max_step(db: Session) -> Decimal:
    return _pct(db, settings.FOLLOW_MAX_STEP, "50")


def tolerance(db: Session) -> Decimal:
    return _pct(db, settings.FOLLOW_TOLERANCE, "1")


def wb_average(db: Session) -> dict[str, float]:
    """Артикул 1С -> средняя ТЕКУЩАЯ цена для покупателя на WB (по карточкам всех кабинетов)."""
    from priceapp import approval
    vals: dict[str, list[float]] = {}
    for a in db.query(Account).filter(Account.is_active.is_(True), Account.platform.in_(SOURCE_PLATFORMS)):
        for r in approval.compute(db, a)["rows"]:
            if r["sale"] and r["w"] and r["art"]:
                vals.setdefault(r["art"], []).append(float(r["sale"]) * float(r["w"]))
    return {art: sum(v) / len(v) for art, v in vals.items()}


def _wb_loaded_at(db: Session) -> str:
    """Когда загружены цены WB — метка опроса: по ней отличаем НОВЫЙ опрос от повторного
    прогона правила на тех же ценах."""
    ts = [a.prices_loaded_at for a in db.query(Account).filter(
        Account.is_active.is_(True), Account.platform.in_(SOURCE_PLATFORMS)) if a.prices_loaded_at]
    return max(ts).isoformat() if ts else ""


def confirmed_average(db: Session, avg: dict[str, float], tol: Decimal) -> dict[str, float]:
    """Только артикулы, у которых средняя цена WB ПОДТВЕРДИЛАСЬ вторым опросом (в пределах
    `follow_tolerance`). 13.10 за час на WB побывали цены, выставленные по ошибке (до 236 240 ₽
    за пуховик), и Kit каждый раз шёл за ними — 3 500 изменений. Одно случайное значение WB
    больше не уводит Kit: цена должна продержаться два опроса. Снимок прошлого опроса — в
    настройке `follow_wb_snapshot`; коммитит вызывающий."""
    import json
    at = _wb_loaded_at(db)
    try:
        snap = json.loads(settings.get(db, settings.FOLLOW_WB_SNAPSHOT) or "{}")
    except ValueError:
        snap = {}
    if snap.get("at") != at:
        snap = {"at": at, "avg": avg, "prev": snap.get("avg") or {}}
        settings.put(db, settings.FOLLOW_WB_SNAPSHOT, json.dumps(snap, ensure_ascii=False))
    prev = snap.get("prev") or {}
    t = float(tol)
    return {art: v for art, v in avg.items()
            if prev.get(art) and abs(v / prev[art] - 1) * 100 <= t}


def wanted_margin(avg: float, cost: float, commission: Decimal, pct: Decimal) -> Decimal:
    """Маржинальность, при которой цена Kit = (1 − pct)·avg: цена = себестоимость × m / (1 − комиссия)."""
    price = avg * float(1 - pct / 100)
    return Decimal(str(price * float(1 - commission / 100) / cost)).quantize(Decimal("0.001"), ROUND_HALF_UP)


def _step(old: int | None, new: int | None) -> Decimal | None:
    if not old or not new:
        return None
    return abs(Decimal(new) / Decimal(old) - 1) * 100


def run(db: Session, actor: str = ACTOR) -> dict:
    """Пересчитать зависимость и поставить в отправку то, что можно без человека. Коммит порциями."""
    from priceapp import approval, mapping
    from priceapp.routers.sku_prices import set_coef
    st = {"accounts": 0, "margins": 0, "recalculated": False, "approved": 0, "manual": 0, "duplicate": 0,
          "disabled": False, "nodata": False}
    if not enabled(db):
        st["disabled"] = True
        return st
    seen = wb_average(db)
    if not seen:
        st["nodata"] = True
        return st
    pct, tol, step = percent(db), tolerance(db), max_step(db)
    avg = confirmed_average(db, seen, tol)
    st["waiting"] = len(seen) - len(avg)
    for a in db.query(Account).filter(Account.is_active.is_(True), Account.platform.in_(TARGET_PLATFORMS)).all():
        st["accounts"] += 1
        rule = get_rule(db, a.platform)
        commission = Decimal(rule.commission_percent or 0) + Decimal(rule.commission_extra or 0)
        data = approval.compute(db, a)["rows"]
        cost: dict[str, float] = {}
        for r in data:
            if r["cost"]:
                cost.setdefault(r["art"], float(r["cost"]))
        have = {c.article: c.coef for c in db.query(ArticleCoef).filter(
            ArticleCoef.account_id == a.id, ArticleCoef.kind == "margin")}
        changed = 0
        for art, c in cost.items():
            if art not in avg:
                continue
            m = wanted_margin(avg[art], c, commission, pct)
            if m < Decimal("0.5") or m > 100:
                continue
            old = have.get(art)
            if old is not None and abs(m / Decimal(old) - 1) * 100 <= tol:
                continue
            changed += set_coef(db, USER, art, a.platform, a.id, m, "margin")
        if changed:
            audit.log(db, actor, "follow_margins", a.name, f"маржинальностей пересчитано: {changed}")
            db.commit()
        st["margins"] += changed
        # Нужен ли расчёт предложений: сместилась маржинальность или цена на площадке разошлась с
        # расчётной у товара, по которому мы уже отправляли цену.
        sent = {p.item_id for p in db.query(ProductPrice).filter(
            ProductPrice.account_id == a.id, ProductPrice.last_sent_price.isnot(None))}
        drift = any(r["ap"] and r["sale"] and abs(r["sale"] / r["ap"] - 1) * 100 > tol
                    and any(i in sent for i in r["item_ids"]) for r in data)
        if not (changed or drift):
            continue
        recalculate_account(db, a)
        st["recalculated"] = True
        pps = {p.item_id: p for p in db.query(ProductPrice).filter(ProductPrice.account_id == a.id)}
        items = mapping.account_items(db, a.id)
        busy = {i for (i,) in db.query(PriceChange.item_id).filter(
            PriceChange.account_id == a.id, PriceChange.is_test.is_(False),
            PriceChange.status == PriceChangeStatus.approved.value)}
        n = 0
        for ch in db.query(PriceChange).filter(
                PriceChange.account_id == a.id, PriceChange.is_test.is_(False),
                PriceChange.status == PriceChangeStatus.proposed.value):
            pp = pps.get(ch.item_id)
            last = pp.last_sent_price if pp else None
            if last is None:
                st["manual"] += 1               # первую партию подтверждает человек
                continue
            if ch.new_price == last:
                cur = max((r.current_price for r in items.get(ch.item_id, []) if r.current_price), default=None)
                if cur is None or cur == last:
                    ch.status = PriceChangeStatus.rejected.value
                    ch.note = "цена уже отправлена — без изменений"
                    st["duplicate"] += 1
                    continue
                # площадка сама сменила цену: вернуть расчётную (она равна последней отправленной)
            if ch.item_id in busy:
                continue
            s = _step(last, ch.new_price)
            if s is not None and s > step:
                ch.note = ((ch.note + "; " if ch.note else "")
                           + f"изменение {s:.0f}% больше {step:.0f}% — подтвердите вручную")[:255]
                st["manual"] += 1
                continue
            if approve(ch, actor, confirm_large=True) is None:
                ch.source = SOURCE
                ch.note = (f"Kit следует за WB: {100 - pct:.0f}% от средней цены WB"
                           + (f"; {ch.note}" if ch.note else ""))[:255]
                st["approved"] += 1
                n += 1
        if n:
            audit.log(db, actor, "follow_run", a.name, f"поставлено в отправку цен: {n}")
        db.commit()
    return st


def summary(st: dict) -> str:
    if st.get("disabled"):
        return "Следование Kit за WB выключено (настройка follow_enabled)."
    if st.get("nodata"):
        return "Следование Kit за WB: цен WB для покупателя нет."
    return (f"Kit следует за WB: маржинальностей пересчитано — {st['margins']}; в отправку — {st['approved']}; "
            f"на ручное подтверждение — {st['manual']}"
            + (f"; ждут подтверждения цены WB вторым опросом — {st['waiting']}" if st.get("waiting") else "") + ".")

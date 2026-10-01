"""Отправка ПОДТВЕРЖДЁННЫХ цен на площадки.

Уходит только `approved` и только не-тестовое. Перед отправкой пол наценки
проверяется ЕЩЁ РАЗ — по текущему курсу, себестоимости и комиссии кабинета:
между подтверждением и отправкой доллар мог вырасти, 1С прислать новую
себестоимость, а оператор поменять комиссию или правило.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from priceapp import accounts, rates
from priceapp.models import (Account, OnecCost, PlatformItem, PriceChange, PriceChangeStatus,
                             ProductPrice)
from priceapp.platforms import PriceItem
from priceapp.pricing import BLOCK_FLOOR, floor_price, get_rule
from priceapp.timeutils import now_utc

MAX_ATTEMPTS = 5
RETRY_MINUTES = (1, 2, 5, 15)


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(minutes=RETRY_MINUTES[min(max(attempts, 1), len(RETRY_MINUTES)) - 1])


def _record_sent(db: Session, change: PriceChange, price: int) -> None:
    pp = db.query(ProductPrice).filter(ProductPrice.item_id == change.item_id,
                                       ProductPrice.account_id == change.account_id).first()
    if pp is None:
        pp = ProductPrice(item_id=change.item_id, account_id=change.account_id)
        db.add(pp)
        db.flush()   # autoflush выключен — следующий запрос должен видеть строку
    pp.last_sent_price = price
    pp.last_sent_at = now_utc()


def run_account(db: Session, account: Account, client) -> dict:
    approved = (db.query(PriceChange)
                .filter(PriceChange.account_id == account.id,
                        PriceChange.status == PriceChangeStatus.approved.value,
                        PriceChange.is_test.is_(False))   # тренировочные НИКОГДА не уходят
                .order_by(PriceChange.id).all())
    if not approved:
        return {}
    latest: dict[str, PriceChange] = {}
    for ch in approved:
        prev = latest.get(ch.item_id)
        if prev is not None:
            prev.status = PriceChangeStatus.rejected.value
            prev.note = "вытеснено более новой подтверждённой ценой"
        latest[ch.item_id] = ch

    rule = get_rule(db, account.platform)
    commission = rule.commission_percent
    rate = rates.current(db)
    costs = {c.item_id: c for c in db.query(OnecCost).filter(OnecCost.item_id.in_(list(latest)))}
    now = now_utc()
    items, by_barcode, floored = [], {}, 0
    for item_id, ch in latest.items():
        if ch.next_attempt_at is not None and ch.next_attempt_at > now:
            continue
        cost = costs.get(item_id)
        if cost is not None and rate is not None and commission is not None:
            cost_rub = Decimal(str(cost.cost_usd)) * rate.usd_rub
            floor = floor_price(cost_rub, rule, commission)
            if ch.new_price < floor:
                ch.status = PriceChangeStatus.blocked.value
                ch.block_reason = BLOCK_FLOOR
                ch.note = (f"при отправке: минимум {floor} ₽ (курс {rate.usd_rub}, "
                           f"себестоимость {cost.cost_usd} $, комиссия {commission}%)")
                floored += 1
                continue
        row = (db.query(PlatformItem).filter(PlatformItem.account_id == account.id,
                                             PlatformItem.barcode == ch.barcode).first())
        if row is None:
            ch.status = PriceChangeStatus.error.value
            ch.last_error = "баркода нет в каталоге кабинета — обновите каталог и пересчитайте"
            continue
        items.append(PriceItem(row.barcode, ch.new_price, row.external_id, row.article))
        by_barcode[row.barcode] = ch

    result = client.push_prices(items) if items else {"ok": [], "errors": [], "sent_prices": {}}
    ok = set(result.get("ok", []))
    sent_prices = result.get("sent_prices") or {}
    errors_text = str(result.get("errors"))[:400]
    for barcode, ch in by_barcode.items():
        ch.attempts += 1
        if barcode in ok:
            price = int(sent_prices.get(barcode, ch.new_price))
            ch.status = PriceChangeStatus.sent.value
            ch.sent_at = now_utc()
            ch.next_attempt_at = None
            if price != ch.new_price:
                ch.note = f"площадка приняла {price} ₽ (одна цена на карточку)"
            _record_sent(db, ch, price)
        elif ch.attempts < MAX_ATTEMPTS:
            ch.next_attempt_at = now_utc() + _retry_delay(ch.attempts)
            ch.last_error = f"попытка {ch.attempts} из {MAX_ATTEMPTS}: {errors_text}"
        else:
            ch.status = PriceChangeStatus.error.value
            ch.last_error = f"не отправлено за {ch.attempts} попыток: {errors_text}"
    db.commit()
    return {"sent": len(ok & set(by_barcode)), "tried": len(by_barcode), "floored": floored}


def run(db: Session, client_factory=None) -> dict:
    stats = {}
    for account in db.query(Account).filter(Account.is_active.is_(True)):
        has = db.query(PriceChange.id).filter(PriceChange.account_id == account.id,
                                             PriceChange.status == PriceChangeStatus.approved.value,
                                             PriceChange.is_test.is_(False)).first()
        if has is None:
            continue
        try:
            client = accounts.client_for(db, account, client_factory)
        except Exception as e:
            stats[account.name] = {"error": str(e)}
            continue
        stats[account.name] = run_account(db, account, client)
    return stats

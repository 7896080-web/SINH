"""Отправка ПОДТВЕРЖДЁННЫХ цен на площадки (репрайсер, см. app/pricing.py).

Сюда попадают только PriceChange в статусе approved — то есть то, что оператор
явно подтвердил на странице «Цены». Расчёт сам ничего не отправляет.

Перед отправкой пол минимальной наценки проверяется ЕЩЁ РАЗ по текущей
себестоимости и текущему правилу: между подтверждением и отправкой 1С могла
прислать новую себестоимость, а оператор — поменять правило.
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import PlatformAccount, PriceChange, PriceChangeStatus, Product, ProductPrice
from app.pricing import BLOCK_FLOOR, floor_price, get_or_create_rule
from app.timeutils import now_utc
from app.workers.dispatch import _resolve_push_target
from app.workers.platform_clients.base import PricePushItem

MAX_ATTEMPTS = 5
RETRY_BACKOFF_MINUTES = (1, 2, 5, 15)


def _retry_delay(attempts: int) -> timedelta:
    idx = min(max(attempts, 1), len(RETRY_BACKOFF_MINUTES)) - 1
    return timedelta(minutes=RETRY_BACKOFF_MINUTES[idx])


def _record_sent(db: Session, change: PriceChange, price: int):
    pp = db.query(ProductPrice).filter(ProductPrice.uid_1c == change.uid_1c,
                                       ProductPrice.account_id == change.account_id).first()
    if pp is None:
        pp = ProductPrice(uid_1c=change.uid_1c, account_id=change.account_id)
        db.add(pp)
        db.flush()   # autoflush выключен — следующий запрос должен видеть строку
    pp.last_sent_price = price
    pp.last_sent_at = now_utc()


def run_price_dispatch(db: Session, clients: dict, accounts: list[PlatformAccount] | None = None) -> dict:
    if accounts is None:
        accounts = list(db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all())

    stats = {}
    now = now_utc()
    for account in accounts:
        client = clients.get(account.id)
        if client is None:
            continue

        approved = db.query(PriceChange).filter(
            PriceChange.account_id == account.id,
            PriceChange.status == PriceChangeStatus.approved,
            PriceChange.is_test.is_(False),   # тестовые записи НИКОГДА не уходят на площадку
        ).order_by(PriceChange.id.asc()).all()
        if not approved:
            continue

        # Несколько подтверждённых цен на один товар — уходит последняя, прежние
        # вытеснены (иначе отложенная после сбоя пережила бы более новую).
        latest = {}
        for change in approved:
            previous = latest.get(change.uid_1c)
            if previous is not None:
                previous.status = PriceChangeStatus.rejected
                previous.note = "вытеснено более новой подтверждённой ценой"
            latest[change.uid_1c] = change

        rule = get_or_create_rule(db, account.id)
        products = {p.uid_1c: p for p in db.query(Product).filter(Product.uid_1c.in_(list(latest)))}

        items, by_barcode = [], {}
        floored = 0
        for uid, change in latest.items():
            if change.next_attempt_at is not None and change.next_attempt_at > now:
                continue
            product = products.get(uid)
            cost = product.cost_price if product is not None else None
            if cost is not None and Decimal(str(cost)) > 0:
                floor = floor_price(Decimal(str(cost)), rule)
                if change.new_price < floor:
                    change.status = PriceChangeStatus.blocked
                    change.block_reason = BLOCK_FLOOR
                    change.note = f"при отправке: минимум {floor} ₽ (себестоимость изменилась)"
                    floored += 1
                    continue
            target = _resolve_push_target(db, uid, account.id)
            if target is None:
                change.status = PriceChangeStatus.error
                change.last_error = "нет баркода для отправки"
                continue
            barcode, external_id, article = target
            items.append(PricePushItem(barcode=barcode, price=change.new_price,
                                       external_id=external_id, article=article))
            by_barcode[barcode] = change

        result = {"ok": [], "errors": [], "sent_prices": {}}
        if items:
            result = client.push_prices(items)

        ok = set(result.get("ok", []))
        sent_prices = result.get("sent_prices") or {}
        errors_text = str(result.get("errors"))[:400]
        for barcode, change in by_barcode.items():
            change.attempts += 1
            if barcode in ok:
                price = int(sent_prices.get(barcode, change.new_price))
                change.status = PriceChangeStatus.sent
                change.sent_at = now_utc()
                change.next_attempt_at = None
                if price != change.new_price:
                    change.note = f"площадка приняла {price} ₽ (одна цена на карточку)"
                _record_sent(db, change, price)
            elif change.attempts < MAX_ATTEMPTS:
                change.next_attempt_at = now_utc() + _retry_delay(change.attempts)
                change.last_error = f"попытка {change.attempts} из {MAX_ATTEMPTS}: {errors_text}"
            else:
                change.status = PriceChangeStatus.error
                change.last_error = f"не отправлено за {change.attempts} попыток: {errors_text}"

        db.commit()
        stats[account.name] = {"sent": len(ok & set(by_barcode)), "tried": len(by_barcode),
                               "floored": floored}
    return stats

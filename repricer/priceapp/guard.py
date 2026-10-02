"""Диапазон безопасности для участия в акциях.

Площадка (или акция, в которую попал товар) может сама поменять цену или скидку
продавца — не чаще раза в сутки. Поэтому при запуске программы и раз в сутки
загружаются текущие цены, и по каждому кабинету с заданным диапазоном
маржинальности («от … до …», `Account.guard_*`) проверяется маржинальность по
ТЕКУЩЕЙ цене — той, что платит покупатель:

  * ниже «от», на площадке стоит НАША цена (текущая = последней отправленной) —
    маржинальность съедает скидка продавца или акция; ценой это не исправить —
    «Внимание» (`eaten`);
  * ниже «от», цена на площадке НЕ наша (площадка или акция её опустила), а наша
    расчётная цена выше текущей и даёт маржинальность не ниже «от» при той же
    скидке — программа сама ставит её в отправку (`below`). Только ВВЕРХ: опустить
    цену без человека программа не вправе ни при каком раскладе;
  * ниже «от», но вернуть нечем (расчётная не выше текущей, сама ниже «от» или
    ниже пола, не считается) — «Внимание» (`stuck`);
  * выше «до» — только пометка (`above`).

Цена, подтверждённая человеком и ещё не ушедшая, не трогается: её решение главнее.
Считается по `product_rows` — тому же, что показывает «Цены» → «Товары».
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from priceapp import audit, rates
from priceapp.models import Account, PriceChange, PriceChangeStatus
from priceapp.pricing import BLOCK_FLOOR, OPEN, _dec
from priceapp.timeutils import now_utc

SOURCE = "guard"


def classify(r: dict, a: Account) -> str | None:
    """Строка «Товаров» относительно диапазона кабинета: below / eaten / stuck / above / None."""
    m = r.get("current_coef")
    if m is None:
        return None
    lo = _dec(a.guard_min_margin) if a.guard_min_margin is not None else None
    if lo is not None and m < lo:
        if r.get("last_sent") and r.get("current") == r["last_sent"]:
            return "eaten"
        price, new_m = r.get("price"), r.get("markup_coef")
        if (price and r.get("current") and price > r["current"] and new_m is not None and new_m >= lo
                and r.get("block_reason") != BLOCK_FLOOR):
            return "below"
        return "stuck"
    if a.guard_max_margin is not None and m > _dec(a.guard_max_margin):
        return "above"
    return None


COMMIT_EVERY = 500


def run(db: Session, product_rows=None, actor: str = "программа") -> dict:
    """Проверить все кабинеты с диапазоном и вернуть цены, где можно. Только по
    уже загруженным текущим ценам — к площадкам не обращается. Коммит порциями:
    одна транзакция на весь каталог держала бы базу заблокированной минутами."""
    if product_rows is None:
        from priceapp.routers.prices import product_rows
    st = {"restored": 0, "eaten": 0, "stuck": 0, "above": 0, "accounts": 0}
    rate = rates.current(db)
    now = now_utc()
    for a in db.query(Account).filter(Account.is_active.is_(True)).all():
        if a.guard_min_margin is None and a.guard_max_margin is None:
            continue
        st["accounts"] += 1
        busy = {i for (i,) in db.query(PriceChange.item_id).filter(
            PriceChange.account_id == a.id, PriceChange.is_test.is_(False),
            PriceChange.status == PriceChangeStatus.approved.value)}
        restored = 0
        for r in product_rows(db, a):
            kind = classify(r, a)
            if kind in ("eaten", "stuck", "above"):
                st[kind] += 1
            if kind != "below" or r["item_id"] in busy:
                continue
            db.add(PriceChange(
                item_id=r["item_id"], account_id=a.id, barcode=r["platform"].barcode,
                cost_usd=r["cost_usd"], usd_rub=rate.usd_rub if rate else None, cost_rub=r["cost_rub"],
                commission_percent=r["commission"], old_price=r["current"], new_price=r["price"],
                markup_rub=r["markup_rub"], markup_coef=r["markup_coef"], source=SOURCE,
                status=PriceChangeStatus.approved.value,
                note=(f"площадка держит {r['current']} ₽, маржинальность {r['current_coef']} ниже «от» "
                      f"{_dec(a.guard_min_margin).normalize()} — возврат к наценке кабинета")[:255],
                decided_by=actor, decided_at=now))
            busy.add(r["item_id"])
            restored += 1
            if restored % COMMIT_EVERY == 0:
                db.commit()
        st["restored"] += restored
        if restored:
            audit.log(db, actor, "guard_restored", a.name, f"возвращено цен: {restored}")
        db.commit()
    return st


def preview(db: Session, product_rows=None) -> dict:
    """То же, что `run`, но ничего не пишет: сколько вернётся и сколько на «Внимание»."""
    if product_rows is None:
        from priceapp.routers.prices import product_rows
    st = {"restored": 0, "eaten": 0, "stuck": 0, "above": 0, "accounts": 0}
    for a in db.query(Account).filter(Account.is_active.is_(True)).all():
        if a.guard_min_margin is None and a.guard_max_margin is None:
            continue
        st["accounts"] += 1
        for r in product_rows(db, a):
            kind = classify(r, a)
            if kind:
                st["restored" if kind == "below" else kind] += 1
    return st


def summary(st: dict, preview: bool = False) -> str:
    if not st.get("accounts"):
        return "Диапазон безопасности не задан ни у одного кабинета («Цены» → «Правила»)."
    verb = "вернётся цен" if preview else "возвращено цен"
    parts = [f"Диапазоны безопасности ({st['accounts']} каб.): {verb} — {st['restored']}"]
    if st["eaten"]:
        parts.append(f"цена наша, маржинальность съедает скидка или акция — {st['eaten']}")
    if st["stuck"]:
        parts.append(f"ниже «от», а вернуть нечем (наша цена не выше текущей или сама ниже «от») — {st['stuck']}")
    if st["above"]:
        parts.append(f"выше «до» — {st['above']}")
    return "; ".join(parts) + "."

"""Диапазон безопасности для участия в акциях.

Площадка (или акция, в которую попал товар) может сама поменять цену или скидку
продавца — не чаще раза в сутки. Поэтому при запуске программы и раз в сутки
загружаются текущие цены, и по каждому кабинету с заданным диапазоном
маржинальности («от … до …», `Account.guard_*`) проверяется маржинальность по
ТЕКУЩЕЙ цене — той, что платит покупатель:

  * ниже «от», а на площадке стоит НЕ наша цена — возвращаем цену по наценке,
    установленной на кабинет (артикул / категория / умолчание — тем же расчётом,
    что и везде). Это подтверждение от имени программы: цена встаёт в очередь
    отправки. Пол не обходится никогда; лимит шага — обходится, это возврат к уже
    заданной наценке, а не новое решение;
  * ниже «от», а цена на площадке и так наша — маржинальность съедает скидка
    продавца или акция. Ценой это не исправить, а слать одно и то же каждый день
    бессмысленно: такие товары показывает «Внимание» (решает человек — снять
    скидку или выйти из акции);
  * выше «до» — только пометка.

Считается по `product_rows` — тому же, что показывает «Цены» → «Товары»: число
здесь и строки там не разойдутся.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from priceapp import audit, rates
from priceapp.models import Account, PriceChange, PriceChangeStatus
from priceapp.pricing import BLOCK_FLOOR, OPEN, _dec
from priceapp.timeutils import now_utc

SOURCE = "guard"


def classify(r: dict, a: Account) -> str | None:
    """Строка «Товаров» относительно диапазона кабинета: below / eaten / above / None."""
    m = r.get("current_coef")
    if m is None:
        return None
    if a.guard_min_margin is not None and m < _dec(a.guard_min_margin):
        return "eaten" if r.get("price") and r["price"] == r.get("current") else "below"
    if a.guard_max_margin is not None and m > _dec(a.guard_max_margin):
        return "above"
    return None


def run(db: Session, product_rows=None, actor: str = "программа") -> dict:
    """Проверить все кабинеты с диапазоном и вернуть цены, где можно. Только по
    уже загруженным текущим ценам — к площадкам не обращается."""
    if product_rows is None:
        from priceapp.routers.prices import product_rows
    st = {"restored": 0, "eaten": 0, "above": 0, "blocked": 0, "unpriced": 0, "accounts": 0}
    rate = rates.current(db)
    now = now_utc()
    for a in db.query(Account).filter(Account.is_active.is_(True)):
        if a.guard_min_margin is None and a.guard_max_margin is None:
            continue
        st["accounts"] += 1
        restored = 0
        for r in product_rows(db, a):
            kind = classify(r, a)
            if kind == "above":
                st["above"] += 1
            if kind == "eaten":
                st["eaten"] += 1
            if kind != "below":
                continue
            if not r["price"]:
                st["unpriced"] += 1
                continue
            if r["block_reason"] == BLOCK_FLOOR:
                st["blocked"] += 1
                continue
            pending = db.query(PriceChange).filter(
                PriceChange.account_id == a.id, PriceChange.item_id == r["item_id"],
                PriceChange.is_test.is_(False), PriceChange.status == PriceChangeStatus.approved.value,
                PriceChange.new_price == r["price"]).first()
            if pending is not None:
                continue            # уже в очереди — второй раз не ставим
            for old in db.query(PriceChange).filter(
                    PriceChange.account_id == a.id, PriceChange.item_id == r["item_id"],
                    PriceChange.is_test.is_(False),
                    PriceChange.status.in_(OPEN + (PriceChangeStatus.approved.value,))):
                old.status = PriceChangeStatus.rejected.value
                old.note = "вытеснено возвратом по диапазону безопасности"
            db.add(PriceChange(
                item_id=r["item_id"], account_id=a.id, barcode=r["platform"].barcode,
                cost_usd=r["cost_usd"], usd_rub=rate.usd_rub if rate else None, cost_rub=r["cost_rub"],
                commission_percent=r["commission"], old_price=r["current"], new_price=r["price"],
                markup_rub=r["markup_rub"], markup_coef=r["markup_coef"], source=SOURCE,
                status=PriceChangeStatus.approved.value,
                note=(f"маржинальность по текущей {r['current_coef']} ниже диапазона «от» "
                      f"{_dec(a.guard_min_margin).normalize()} — возврат к наценке кабинета")[:255],
                decided_by=actor, decided_at=now))
            db.flush()
            restored += 1
        st["restored"] += restored
        if restored:
            audit.log(db, actor, "guard_restored", a.name, f"возвращено цен: {restored}")
    db.commit()
    return st


def summary(st: dict) -> str:
    if not st.get("accounts"):
        return "Диапазон безопасности не задан ни у одного кабинета («Цены» → «Правила»)."
    parts = [f"Диапазоны безопасности ({st['accounts']} каб.): возвращено цен — {st['restored']}"]
    if st["eaten"]:
        parts.append(f"цена наша, маржинальность съедает скидка или акция — {st['eaten']} (см. «Внимание»)")
    if st["blocked"]:
        parts.append(f"наша цена тоже ниже пола, не отправлено — {st['blocked']}")
    if st["unpriced"]:
        parts.append(f"цена не считается — {st['unpriced']}")
    if st["above"]:
        parts.append(f"выше «до» — {st['above']}")
    return "; ".join(parts) + "."

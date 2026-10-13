"""Проверка скидки продавца на WB: площадка сменила скидку — вернуть эталонную.

WB умеет менять скидку карточки сам: автоакция из календаря акций ставит товарам свою
скидку (13.10 так ушло 221 карточку одного кабинета разом). Цена до скидки при этом не
меняется, поэтому правило эталонной цены (`reference.py`) этого не видит, а покупатель
платит другую цену. Хуже того, правило «Kit следует за WB» (`follow.py`) берёт цену WB
ПОСЛЕ скидки и честно уводит за ней Kit.

Эталонная скидка — по карточке (nmID), целая, как на WB (`ReferenceDiscount`). Пишется:
* командой `scripts/reference_tool.py adopt-discount` — нынешние скидки, загруженные с WB,
  объявляются эталонными (делается, когда на WB всё стоит как надо);
* скриптом `scripts/wb_restore_discount.py --send` — по каждой отправленной карточке.
Карточки без эталонной скидки не трогаем: угадывать, какая скидка «правильная», нельзя.

`run` идёт в опросе WB СРАЗУ после загрузки цен и ДО правила «Kit следует за WB»: иначе Kit
успел бы уйти за чужой скидкой. Принятая площадкой отправка сразу правит у нас цену со
скидкой по карточке — так `follow` в этом же опросе видит эталонную цену, а не ту, что
поставила акция. Площадка задание отклонит позже — следующий опрос увидит расхождение снова
и повторит. Повышение цены выводит товар из акции — это и есть цель.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from priceapp import audit, settings
from priceapp.models import Account, ApiCredential, PlatformItem, ReferenceDiscount
from priceapp.timeutils import now_utc

PLATFORMS = ("wb",)
CHUNK = 1000


def enabled(db: Session) -> bool:
    return settings.get(db, settings.DISCOUNT_GUARD_ENABLED) != "0"


def card_discount(price: int | None, sale: int | None) -> int | None:
    """Скидка карточки целым процентом, как её держит WB."""
    if not price or not sale:
        return None
    if sale >= price:
        return 0
    return int((Decimal(100) - Decimal(sale) * 100 / Decimal(price)).quantize(Decimal(1), ROUND_HALF_UP))


def nm_of(item: PlatformItem) -> str:
    nm = (item.external_id or "").split(":")[0]
    return nm if nm.isdigit() else ""


def cards(db: Session, account_id: int) -> dict[str, list[PlatformItem]]:
    """nmID -> строки каталога с загруженной ценой."""
    out: dict[str, list[PlatformItem]] = {}
    for it in db.query(PlatformItem).filter(PlatformItem.account_id == account_id,
                                            PlatformItem.current_price.isnot(None)):
        nm = nm_of(it)
        if nm:
            out.setdefault(nm, []).append(it)
    return out


def current_discount(rows: list[PlatformItem]) -> int | None:
    """По строке с наибольшей ценой — ей же программа считает цену карточки."""
    top = max(rows, key=lambda r: r.current_price)
    return card_discount(top.current_price, top.current_sale_price or top.current_price)


def record(db: Session, account_id: int, nm: str, discount: int, source: str = "sent") -> None:
    """Записать эталонную скидку карточки. Не коммитит."""
    row = (db.query(ReferenceDiscount)
           .filter(ReferenceDiscount.account_id == account_id, ReferenceDiscount.nm_id == str(nm)).first())
    if row is None:
        row = ReferenceDiscount(account_id=account_id, nm_id=str(nm), discount=int(discount))
        db.add(row)
    row.discount = int(discount)
    row.source = source
    row.set_at = now_utc()


def adopt(db: Session, actor: str = "программа") -> dict[str, int]:
    """Нынешние скидки, загруженные с WB, — эталонными. Перезаписывает прежние. Коммитит."""
    out = {}
    for a in db.query(Account).filter(Account.platform.in_(PLATFORMS)).all():
        n = 0
        for nm, rows in cards(db, a.id).items():
            d = current_discount(rows)
            if d is not None:
                record(db, a.id, nm, d, source="adopt")
                n += 1
        out[a.name] = n
        audit.log(db, actor, "discount_adopt", a.name, f"эталонными объявлены скидки {n} карточек "
                  f"(цены загружены {a.prices_loaded_at})")
        db.commit()
    return out


def mismatches(db: Session, account: Account) -> list[tuple[str, int, int, list[PlatformItem]]]:
    """(nmID, скидка сейчас, эталонная, строки) — где площадка держит не нашу скидку."""
    refs = {r.nm_id: r.discount for r in db.query(ReferenceDiscount)
            .filter(ReferenceDiscount.account_id == account.id)}
    out = []
    if not refs:
        return out
    for nm, rows in cards(db, account.id).items():
        ref = refs.get(nm)
        cur = current_discount(rows)
        if ref is not None and cur is not None and cur != ref:
            out.append((nm, cur, ref, rows))
    return out


def run(db: Session, client_factory=None, actor: str = "программа") -> dict:
    """Вернуть эталонную скидку там, где площадка её сменила. Коммитит по кабинету."""
    from priceapp import accounts
    from priceapp.platforms import WB_PRICES
    st = {"accounts": 0, "restored": 0, "errors": [], "disabled": False}
    if not enabled(db):
        st["disabled"] = True
        return st
    for a in db.query(Account).filter(Account.is_active.is_(True), Account.platform.in_(PLATFORMS)).all():
        if db.query(ReferenceDiscount.id).filter(ReferenceDiscount.account_id == a.id).first() is None:
            continue
        st["accounts"] += 1
        bad = mismatches(db, a)
        if not bad:
            continue
        if db.query(ApiCredential.id).filter(ApiCredential.account_id == a.id).first() is None:
            st["errors"].append(f"{a.name}: нет ключа API — скидка не возвращена у {len(bad)} карточек")
            continue
        try:
            client = accounts.client_for(db, a, client_factory)
        except Exception as e:
            st["errors"].append(f"{a.name}: {e}"[:200])
            continue
        done = []
        for start in range(0, len(bad), CHUNK):
            chunk = bad[start:start + CHUNK]
            body = {"data": [{"nmID": int(nm), "discount": ref} for nm, _, ref, _ in chunk]}
            try:
                data = client._post(f"{WB_PRICES}/api/v2/upload/task", body)
            except Exception as e:
                st["errors"].append(f"{a.name}: {e}"[:200])
                continue
            if isinstance(data, dict) and data.get("error"):
                st["errors"].append(f"{a.name}: {data.get('errorText') or 'WB вернул error=true'}"[:200])
                continue
            for nm, cur, ref, rows in chunk:
                for r in rows:
                    r.current_sale_price = int((Decimal(r.current_price) * (100 - ref) / 100)
                                               .quantize(Decimal(1), ROUND_HALF_UP))
                done.append((nm, cur, ref))
        if done:
            st["restored"] += len(done)
            sample = ", ".join(f"{nm}: {cur}% -> {ref}%" for nm, cur, ref in done[:20])
            audit.log(db, actor, "discount_restore", a.name,
                      f"WB сменил скидку — возвращена эталонная у {len(done)} карточек: {sample}"
                      + (" …" if len(done) > 20 else ""))
        db.commit()
    return st


def summary(st: dict) -> str:
    if st.get("disabled"):
        return "Проверка скидки WB выключена (настройка discount_guard_enabled)."
    if not st.get("accounts"):
        return "Эталонных скидок WB нет: scripts/reference_tool.py adopt-discount."
    text = f"Скидка WB ({st['accounts']} каб.): возвращена эталонная у {st['restored']} карточек"
    if st["errors"]:
        text += "; ошибки: " + "; ".join(st["errors"])
    return text + "."

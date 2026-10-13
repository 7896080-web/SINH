"""Эталонная цена кабинетов WB и её защита.

ЭТАЛОН — цена ПОСЛЕ СКИДКИ ПРОДАВЦА (решение владельца 13.10): пара «цена до скидки +
скидка продавца», которую мы поставили на карточку. Хранится `ReferencePrice.ref_price`
(цена до скидки) и `ReferencePrice.discount` (скидка); эталон = их произведение
(`ref_sale`). Пишется после каждой принятой площадкой отправки (`dispatch.run_account`),
командой `reference_tool.py adopt-current` / `etalon_apply.py --adopt` — то, что WB держит
сейчас, и `adopt_sent` для цен, ушедших до появления правила.

1. «Площадка занизила цену». Цена после скидки продавца по ТЕКУЩИМ ценам WB ниже эталонной
   больше чем на `ref_drop_percent` (10%) — неважно, что сменилось: цена до скидки, скидка
   (автоакция) или оба. Эталонная пара ВОЗВРАЩАЕТСЯ одним заданием WB на карточку, только
   вверх. Возврат, поднимающий цену больше чем на `ref_max_raise_percent` (50%), не
   отправляется, а уходит во «Внимание»: так выглядит ошибочный эталон, а не занижение.
2. «Курс». Курс ушёл от курса эталонной больше чем на `ref_rate_percent` (2%) — эталонная
   цена до скидки пересчитывается по наценке на ТЕКУЩЕМ курсе, затем работает правило 1.

Только кабинеты WB. Опрос площадки — при запуске программы и раз в 6 часов
(`workers/jobs.job_wb_poll`), а также сразу, когда курс ушёл за порог.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from priceapp import audit, rates, settings
from priceapp.models import Account, PriceChange, PriceChangeStatus, ReferencePrice
from priceapp.pricing import _dec, get_rule, item_facts, payout
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


def adopt_current(db: Session, actor: str = "программа") -> dict:
    """Новый эталон WB: эталонной становится цена до скидки, которую площадка держит СЕЙЧАС
    (загруженная программой при последнем опросе), — у каждого SKU каждого кабинета.
    Делается, когда цены на WB выставлены как надо (скриптом wb_restore_discount.py или
    руками в кабинете), а программа их уже загрузила. Уже записанный эталон
    перезаписывается. Коммитит."""
    from priceapp import mapping
    st = {"accounts": 0, "recorded": 0, "skipped": 0}
    rate = rates.current(db)
    for a in db.query(Account).filter(Account.is_active.is_(True), Account.platform.in_(PLATFORMS)).all():
        st["accounts"] += 1
        rule = get_rule(db, a.platform)
        n = 0
        for item_id, rows in mapping.account_items(db, a.id).items():
            price = max((r.current_price for r in rows if r is not None and r.current_price), default=None)
            if price and record(db, a, item_id, price, rows, rule, rate, source="adopt"):
                n += 1
                if n % COMMIT_EVERY == 0:
                    db.commit()
            else:
                st["skipped"] += 1
        st["recorded"] += n
        audit.log(db, actor, "reference_adopt_current", a.name,
                  f"новый эталон: эталонных цен {n} (цены загружены {a.prices_loaded_at})")
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


def max_raise_percent(db: Session) -> Decimal:
    return _pct(db, settings.REF_MAX_RAISE_PERCENT, "50")


def ref_sale(ref: ReferencePrice) -> int | None:
    """Эталон — цена ПОСЛЕ скидки продавца: эталонная цена до скидки × (1 − эталонная скидка)."""
    if not ref.ref_price:
        return None
    return int((Decimal(ref.ref_price) * (1 - _dec(ref.discount or 0))).quantize(Decimal(1), ROUND_HALF_UP))


def ref_discount_int(ref: ReferencePrice) -> int:
    """Скидка эталона целым процентом, как её держит WB."""
    return int((_dec(ref.discount or 0) * 100).quantize(Decimal(1), ROUND_HALF_UP))


def classify(r: dict, ref: ReferencePrice, drop: Decimal, max_raise: Decimal = Decimal(50)) -> str | None:
    """`below` — цена после скидки продавца ниже эталонной больше чем на `drop` %: вернуть;
    `big` — вернуть надо, но возврат поднял бы её больше чем на `max_raise` % — это скорее
    ошибка эталона, чем занижение площадкой: «Внимание», не отправка; None — в порядке."""
    target, cur = ref_sale(ref), r.get("current_sale")
    if not target or not cur:
        return None
    if Decimal(cur) >= Decimal(target) * (1 - drop / 100):
        return None
    if Decimal(target) > Decimal(cur) * (1 + max_raise / 100):
        return "big"
    return "below"


def _clear_poll_flag(db: Session) -> None:
    settings.put(db, settings.REF_POLL_NOW, "0")


def _nm(item) -> str:
    nm = (getattr(item, "external_id", "") or "").split(":")[0]
    return nm if nm.isdigit() else ""


def run(db: Session, product_rows=None, actor: str = "программа", client_factory=None) -> dict:
    """Пересчёт эталонных по курсу и возврат эталонной цены ПОСЛЕ СКИДКИ ПРОДАВЦА там, где
    площадка её занизила — ценой до скидки, скидкой (автоакция) или обоими.

    Возврат — той же парой, что записана эталоном: цена до скидки + скидка, одним заданием
    WB на карточку (nmID), только вверх. Возврат, поднимающий цену больше чем на
    `max_raise_percent` (50%), не отправляется: 13.10 правило «вернуло» по 3164M эталон
    472 479 при цене 158 281 — эталон был ошибочным, и поднимать цену втрое без человека
    нельзя. Такое — «Внимание». Отправка прошла — цены карточки у нас сразу правятся на
    отправленные (правило «Kit следует за WB» в этом же опросе видит эталонную цену).
    Коммит по кабинету."""
    if product_rows is None:
        from priceapp.routers.prices import product_rows
    st = {"accounts": 0, "revalued": 0, "restored": 0, "eaten": 0, "disabled": False, "errors": []}
    if not enabled(db):
        st["disabled"] = True
        return st
    rate = rates.current(db)
    rate_thr = rate_percent(db) / 100
    drop, max_raise = drop_percent(db), max_raise_percent(db)
    now = now_utc()
    for a in db.query(Account).filter(Account.is_active.is_(True), Account.platform.in_(PLATFORMS)).all():
        refs = {r.item_id: r for r in db.query(ReferencePrice).filter(ReferencePrice.account_id == a.id)}
        if not refs:
            continue
        st["accounts"] += 1
        busy = {i for (i,) in db.query(PriceChange.item_id).filter(
            PriceChange.account_id == a.id, PriceChange.is_test.is_(False),
            PriceChange.status == PriceChangeStatus.approved.value)}
        revalued = 0
        cards: dict[str, dict] = {}
        big: list[str] = []
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
            if r["item_id"] in busy:
                continue
            # 2. Площадка занизила цену после скидки продавца — вернуть эталонную (только вверх).
            kind = classify(r, ref, drop, max_raise)
            nm = _nm(r.get("platform"))
            if kind is None or not nm:
                continue
            if kind == "big":
                big.append(f"{nm}: {r.get('current_sale')} -> {ref_sale(ref)}")
                continue
            c = cards.get(nm)
            if c is None or ref.ref_price > c["ref"].ref_price:
                cards[nm] = {"ref": ref, "cur": r.get("current_sale")}
        st["revalued"] += revalued
        st["eaten"] += len(big)
        if big:
            audit.log(db, actor, "reference_big", a.name,
                      f"эталон выше нынешней цены после скидки больше чем на {max_raise.normalize()}% — НЕ возвращено, "
                      f"проверьте эталон ({len(big)}): " + "; ".join(big[:20]))
        restored = _send(db, a, cards, client_factory, st, actor) if cards else 0
        st["restored"] += restored
        if revalued:
            audit.log(db, actor, "reference_run", a.name, f"эталонных пересчитано по курсу: {revalued}")
        db.commit()
    return st


def _send(db: Session, a: Account, cards: dict, client_factory, st: dict, actor: str) -> int:
    """Эталонная пара (цена до скидки + скидка) на карточки WB одним заданием на пачку."""
    from priceapp import accounts
    from priceapp.models import PlatformItem
    from priceapp.platforms import WB_PRICES
    try:
        client = accounts.client_for(db, a, client_factory)
    except Exception as e:
        st["errors"].append(f"{a.name}: {e}"[:200])
        return 0
    items = list(cards.items())
    done = []
    for start in range(0, len(items), 1000):
        chunk = items[start:start + 1000]
        body = {"data": [{"nmID": int(nm), "price": int(c["ref"].ref_price), "discount": ref_discount_int(c["ref"])}
                         for nm, c in chunk]}
        try:
            data = client._post(f"{WB_PRICES}/api/v2/upload/task", body)
        except Exception as e:
            st["errors"].append(f"{a.name}: {e}"[:200])
            continue
        if isinstance(data, dict) and data.get("error"):
            st["errors"].append(f"{a.name}: {data.get('errorText') or 'WB вернул error=true'}"[:200])
            continue
        done += chunk
    for nm, c in done:
        price, d = int(c["ref"].ref_price), ref_discount_int(c["ref"])
        for it in db.query(PlatformItem).filter(PlatformItem.account_id == a.id,
                                                PlatformItem.external_id.like(f"{nm}:%")):
            it.current_price = price
            it.current_sale_price = int((Decimal(price) * (100 - d) / 100).quantize(Decimal(1), ROUND_HALF_UP))
    if done:
        sample = "; ".join(f"{nm}: {c['cur']} -> {ref_sale(c['ref'])}" for nm, c in done[:20])
        audit.log(db, actor, "reference_run", a.name,
                  f"возвращена эталонная цена после скидки продавца: {len(done)} карточек — {sample}"
                  + (" …" if len(done) > 20 else ""))
    return len(done)


def summary(st: dict) -> str:
    if st.get("disabled"):
        return "Правило эталонной цены выключено (настройка ref_enabled)."
    if not st.get("accounts"):
        return "Эталонных цен WB пока нет: правило начнёт работать после записи эталона."
    parts = [f"Эталонные цены WB ({st['accounts']} каб.): пересчитано по курсу — {st['revalued']}; "
             f"возвращено цен после скидки продавца — {st['restored']}"]
    if st["eaten"]:
        parts.append(f"эталон выше нынешней цены больше допустимого, не возвращено — {st['eaten']} (проверьте эталон)")
    if st.get("errors"):
        parts.append("ошибки: " + "; ".join(st["errors"]))
    return "; ".join(parts) + "."

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
   Кроме АКЦИИ: скидка продавца на WB выше «до» коридора (её ставим не мы) — эталон
   возвращается при любом подъёме до 500% (`PROMO_RAISE_CEILING`).
2. «Курс». Курс ушёл от курса эталонной больше чем на `ref_rate_percent` (2%) — эталон
   пересчитывается ПО КУРСУ (× новый / прежний, скидка та же), затем работает правило 1.
   Скачок больше 20% — повторный запрос к ЦБ: подтвердил — пересчёт, нет — в следующий раз
   (ручной курс повторно не запросить — у него скачок не применяется, запись в журнал).

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
PROMO_RAISE_CEILING = Decimal(500)         # акция: возврат при любом подъёме, кроме совсем абсурдного
MAX_RATE_JUMP = Decimal("0.2")             # скачок курса — повторный запрос к ЦБ перед пересчётом
RATE_CONFIRM_TOLERANCE = Decimal("0.005")   # повторный ответ ЦБ «тот же», если разница не больше 0,5%
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
    _mark_dirty()
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
    snapshot_if_dirty(db, "запись эталона")
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
    snapshot_if_dirty(db, "запись эталона")
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


def is_promo(r: dict, promo_disc: Decimal | None) -> bool:
    """Скидка продавца на WB сейчас ВЫШЕ нашего коридора: такую ставим не мы — её поставила
    акция площадки (14.10: 84–87% при коридоре 35–50%)."""
    price, sale = r.get("current"), r.get("current_sale")
    if promo_disc is None or not price or not sale or sale >= price:
        return False
    return (1 - Decimal(sale) / Decimal(price)) * 100 > promo_disc


def classify(r: dict, ref: ReferencePrice, drop: Decimal, max_raise: Decimal = Decimal(50),
             promo_disc: Decimal | None = None) -> str | None:
    """`below` — цена после скидки продавца ниже эталонной больше чем на `drop` %: вернуть;
    `big` — вернуть надо, но возврат поднял бы её больше чем на `max_raise` % — это скорее
    ошибка эталона, чем занижение площадкой: «Внимание», не отправка; None — в порядке.

    Исключение — АКЦИЯ (`is_promo`: скидка выше коридора `promo_disc`): там глубина подъёма
    говорит о глубине акции, а не об ошибке эталона, и эталон возвращается при любом подъёме
    до `PROMO_RAISE_CEILING` (решение владельца 14.10: «в акциях не участвуем»; при пределе
    200% не вернулись карточки, где акция срезала цену на 70%). Предел `max_raise` остаётся
    там, где скидка в коридоре, а разошлась сама цена до скидки — так выглядел ошибочный
    эталон 13.10."""
    target, cur = ref_sale(ref), r.get("current_sale")
    if not target or not cur:
        return None
    if Decimal(cur) >= Decimal(target) * (1 - drop / 100):
        return None
    limit = PROMO_RAISE_CEILING if is_promo(r, promo_disc) else max_raise
    if Decimal(target) > Decimal(cur) * (1 + limit / 100):
        return "big"
    return "below"


def _clear_poll_flag(db: Session) -> None:
    settings.put(db, settings.REF_POLL_NOW, "0")


def _nm(item) -> str:
    nm = (getattr(item, "external_id", "") or "").split(":")[0]
    return nm if nm.isdigit() else ""


def confirm_rate_jump(db: Session, rate, cbr_session=None) -> tuple[object, bool, str]:
    """Курс ушёл от курса эталонов больше чем на `MAX_RATE_JUMP` — повторный запрос к ЦБ.
    Подтвердил тот же курс — скачок настоящий, эталоны пересчитываются (решение владельца
    14.10: курс берётся у ЦБ автоматически, ограничение снимается повторным запросом).
    Не ответил или дал другое число — в этот раз не пересчитываем: следующий опрос спросит
    снова. Ручной курс повторно не запросить — у него защита от опечатки остаётся.
    -> (курс, подтверждён ли скачок, пояснение)."""
    if rate is None:
        return rate, False, ""
    seen = {_dec(r) for (r,) in db.query(ReferencePrice.usd_rub).distinct() if r}
    if not any(abs(_dec(rate.usd_rub) / v - 1) > MAX_RATE_JUMP for v in seen):
        return rate, False, ""
    if rate.source != "cbr":
        return rate, False, "курс задан вручную — проверьте его на странице «Курс $»"
    try:
        row = rates.fetch_cbr(db, cbr_session)
    except Exception as e:
        return rate, False, f"повторный запрос к ЦБ не удался ({e}) — спросим при следующем опросе"[:300]
    fresh = _dec(row.usd_rub)
    if abs(fresh / _dec(rate.usd_rub) - 1) <= RATE_CONFIRM_TOLERANCE:
        return rates.current(db), True, f"повторный запрос к ЦБ подтвердил курс {fresh}"
    return rates.current(db), False, (f"повторный запрос к ЦБ дал {fresh} вместо {rate.usd_rub} — "
                                      "пересчитаем, когда курс подтвердится")


def run(db: Session, product_rows=None, actor: str = "программа", client_factory=None, cbr_session=None) -> dict:
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
    rate, jump_confirmed, jump_note = confirm_rate_jump(db, rate, cbr_session)
    if jump_confirmed:
        audit.log(db, actor, "reference_rate_jump", "",
                  f"курс ушёл больше чем на {int(MAX_RATE_JUMP * 100)}% от курса эталонов; {jump_note} — "
                  "эталоны пересчитываются")
    rate_thr = rate_percent(db) / 100
    drop, max_raise = drop_percent(db), max_raise_percent(db)
    from priceapp.etalon import corridor
    promo_disc = Decimal(corridor(db)[1])          # скидка выше «до» коридора — акция площадки
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
        promo: set[str] = set()
        rate_jumps: set[str] = set()
        cards: dict[str, dict] = {}
        big: dict[str, str] = {}                 # по карточке: строка на каждый размер повторяла бы одно и то же
        for r in product_rows(db, a):
            ref = refs.get(r["item_id"])
            if ref is None:
                continue
            # 1. Курс ушёл за порог — эталон пересчитывается ПО КУРСУ: тот же эталон × новый курс /
            # курс эталона, скидка прежняя. Раньше он заменялся ценой по наценке на новом курсе, а это
            # другое число: ручной эталон со страницы «Эталонные цены» стирался, а в акцию цена до
            # скидки считалась под акционную скидку при прежней скидке эталона — эталон раздувался,
            # и правило 2 само поднимало WB (проба: +93% без человека; аудит 14.10).
            if rate is not None and ref.usd_rub and ref.ref_price:
                ratio = _dec(rate.usd_rub) / _dec(ref.usd_rub)
                if abs(ratio - 1) > MAX_RATE_JUMP and not jump_confirmed:
                    rate_jumps.add(f"{ref.usd_rub} -> {rate.usd_rub}")
                elif abs(ratio - 1) > rate_thr:
                    ref.ref_price = int((Decimal(ref.ref_price) * ratio).to_integral_value(rounding=ROUND_HALF_UP))
                    if ref.ref_payout is not None:
                        ref.ref_payout = (_dec(ref.ref_payout) * ratio).quantize(Decimal("0.01"))
                    ref.usd_rub = rate.usd_rub
                    ref.source = "rate"
                    ref.set_at = now
                    revalued += 1
                    _mark_dirty()
            if r["item_id"] in busy:
                continue
            # 2. Площадка занизила цену после скидки продавца — вернуть эталонную (только вверх).
            kind = classify(r, ref, drop, max_raise, promo_disc)
            nm = _nm(r.get("platform"))
            if kind is None or not nm:
                continue
            if kind == "big":
                big.setdefault(nm, f"{nm}: {r.get('current_sale')} -> {ref_sale(ref)}")
                continue
            if is_promo(r, promo_disc):
                promo.add(nm)
            c = cards.get(nm)
            if c is None or ref.ref_price > c["ref"].ref_price:
                cards[nm] = {"ref": ref, "cur": r.get("current_sale"), "items": c["items"] if c else set()}
            cards[nm]["items"].add(r["item_id"])
            cards[nm]["promo"] = nm in promo
        st["revalued"] += revalued
        st["promo"] = st.get("promo", 0) + len(promo & set(cards))
        if rate_jumps:
            # Скачок больше `MAX_RATE_JUMP` не подтвердился повторным запросом к ЦБ (или курс
            # ручной) — в этот раз эталоны не трогаем; следующий опрос спросит ЦБ снова.
            audit.log(db, actor, "reference_rate_jump", a.name,
                      f"курс ушёл больше чем на {int(MAX_RATE_JUMP * 100)}% от курса эталона "
                      f"({'; '.join(sorted(rate_jumps))[:200]}) — эталоны НЕ пересчитаны: {jump_note}"[:1000])
        st["eaten"] += len(big)
        if big:
            audit.log(db, actor, "reference_big", a.name,
                      f"эталон выше нынешней цены после скидки больше чем на {format(max_raise.normalize(), 'f')}% — НЕ возвращено, "
                      f"проверьте эталон ({len(big)}): " + "; ".join(list(big.values())[:20]))
        restored = _send(db, a, cards, client_factory, st, actor) if cards else 0
        st["restored"] += restored
        if revalued:
            audit.log(db, actor, "reference_run", a.name, f"эталонных пересчитано по курсу: {revalued}")
        db.commit()
    snapshot_if_dirty(db, "пересчёт по курсу")
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
    from priceapp.dispatch import _record_sent
    # Строки кабинета — одним запросом, по nmID: `LIKE 'nm:%'` на каждую карточку без индекса
    # шёл 46 с на 4000 карточек в тяжёлом потоке (аудит 14.10).
    by_nm: dict[str, list] = {}
    if done:
        for it in db.query(PlatformItem).filter(PlatformItem.account_id == a.id):
            by_nm.setdefault((it.external_id or "").split(":")[0], []).append(it)
    for nm, c in done:
        price, d = int(c["ref"].ref_price), ref_discount_int(c["ref"])
        for item_id in c.get("items", ()):
            _record_sent(db, item_id, a.id, price)   # цена наша — диапазон безопасности не должен её «возвращать»
        from priceapp.etalon import journal_sent
        journal_sent(db, a.id, sorted(c.get("items", ())), "", None, price, SOURCE,
                     f"возврат эталона: площадка держала {c['cur']} ₽ после скидки, скидка {d}%", actor)
        for it in by_nm.get(str(nm), []):
            if ":" not in (it.external_id or ""):
                continue
            it.current_price = price
            it.current_sale_price = int((Decimal(price) * (100 - d) / 100).quantize(Decimal(1), ROUND_HALF_UP))
    if done:
        sample = "; ".join(f"{nm}: {c['cur']} -> {ref_sale(c['ref'])}" + (" (акция)" if c.get("promo") else "")
                           for nm, c in done[:20])
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
             f"возвращено цен после скидки продавца — {st['restored']}"
             + (f" (из них после акции со скидкой выше коридора — {st['promo']})" if st.get("promo") else "")]
    if st["eaten"]:
        parts.append(f"эталон выше нынешней цены больше допустимого, не возвращено — {st['eaten']} (проверьте эталон)")
    if st.get("errors"):
        parts.append("ошибки: " + "; ".join(st["errors"]))
    return "; ".join(parts) + "."


# --- Копии эталона ----------------------------------------------------------------
# Эталон меняют четыре события: ручная отправка цены, команды записи эталона, отправка по
# диапазону безопасности и пересчёт по курсу. После каждого сохраняется Excel с ПОЛНЫМ
# эталоном (`config.ETALON_DIR`) — чтобы было видно, что и когда стало эталоном, и чтобы
# было к чему вернуться. Тот же эталон второй раз не сохраняется (сверка по хэшу).
SNAPSHOT_KEEP = 500
_dirty = False


def _mark_dirty() -> None:
    global _dirty
    _dirty = True


def snapshot_rows(db: Session) -> list[list]:
    from priceapp import mapping
    from priceapp.pricing import article_map, article_of
    articles = article_map(db)
    out = []
    for a in db.query(Account).filter(Account.platform.in_(PLATFORMS)).order_by(Account.name):
        refs = db.query(ReferencePrice).filter(ReferencePrice.account_id == a.id).all()
        if not refs:
            continue
        items = mapping.account_items(db, a.id)
        for r in sorted(refs, key=lambda r: (article_of(articles, r.item_id), r.item_id)):
            rows = items.get(r.item_id) or []
            nm = next((_nm(x) for x in rows if _nm(x)), "")
            out.append([a.name, int(nm) if nm else None, article_of(articles, r.item_id), r.item_id,
                        int(r.ref_price), ref_discount_int(r), ref_sale(r), r.source,
                        r.set_at.strftime("%Y-%m-%d %H:%M") if r.set_at else "",
                        float(r.usd_rub) if r.usd_rub is not None else None])
    return out


SNAPSHOT_HEAD = ["Кабинет", "Артикул WB", "Артикул 1С", "ID_1С", "Цена до скидки, руб", "Скидка продавца, %",
                 "ЭТАЛОН: цена после скидки продавца, руб", "Источник", "Записан (UTC)", "Курс $"]


def snapshot(db: Session, reason: str = ""):
    """Сохранить копию эталона, если он изменился с прошлой копии. Возвращает путь или None."""
    import hashlib
    import json
    from datetime import datetime

    from priceapp import config
    from priceapp.excel import xlsx_bytes
    rows = snapshot_rows(db)
    if not rows:
        return None
    digest = hashlib.sha256(json.dumps([r[:7] for r in rows], ensure_ascii=False).encode()).hexdigest()
    if settings.get(db, settings.REF_SNAPSHOT_HASH) == digest:
        return None
    config.ETALON_DIR.mkdir(parents=True, exist_ok=True)
    tag = "".join(ch if ch.isalnum() else "_" for ch in reason)[:30].strip("_")
    stem = f"etalon_{datetime.now():%Y%m%d-%H%M%S}"
    path = config.ETALON_DIR / f"{stem}{('_' + tag) if tag else ''}.xlsx"
    n = 1
    while path.exists():                                  # две копии в одну секунду — не затирать
        n += 1
        path = config.ETALON_DIR / f"{stem}-{n}{('_' + tag) if tag else ''}.xlsx"
    path.write_bytes(xlsx_bytes(SNAPSHOT_HEAD, rows))
    settings.put(db, settings.REF_SNAPSHOT_HASH, digest)
    audit.log(db, "программа", "etalon_snapshot", "", f"копия эталона: {path.name} ({len(rows)} строк; {reason})")
    db.commit()
    for old in sorted(config.ETALON_DIR.glob("etalon_*.xlsx"))[:-SNAPSHOT_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def snapshot_if_dirty(db: Session, reason: str = ""):
    """Копия — только если эталон в этом процессе менялся (дёшево звать после каждой отправки)."""
    global _dirty
    if not _dirty:
        return None
    _dirty = False
    try:
        return snapshot(db, reason)
    except Exception:
        import logging
        logging.getLogger("repricer.reference").exception("копия эталона не сохранилась")
        return None

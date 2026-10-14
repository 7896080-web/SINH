"""Эталонные цены: все площадки следуют за эталоном WB (страница «Эталонные цены»).

ЭТАЛОН артикула 1С — цена ПОСЛЕ скидки продавца на WB, записанная программой
(`ReferencePrice`, см. `reference.py`), средняя по всем карточкам всех кабинетов WB.

Площадки-последователи (решение владельца 14.10):
  * цена после скидки продавца = эталон − N% площадки (`etalon_shift_<площадка>`; Kit 45,
    Ламода 31, Ozon — пусто, то есть пока не следует);
  * Kit и Ламода: скидка продавца ЦЕЛАЯ, в коридоре 35–50% — нынешняя, если в коридоре, иначе
    ближняя граница; цена до скидки = цель / (1 − скидка), вверх до рубля, — цена со скидкой
    остаётся ровно целью. Ламоде — ещё даты скидки (без них она скидку не ставит);
  * Ozon: цена = цель (зачёркнутую не трогаем).
  * ниже пола хотя бы у одного размера карточки — карточку не трогаем и называем.

Смена эталона (`set_etalon`): на всех карточках артикула во всех кабинетах WB — цена до скидки
и целая скидка под новую цель; эталон записывается СРАЗУ (иначе правило возврата `reference.run`
вернуло бы прежний, пока идут шаги снижения); снижение больше `wb_max_drop_percent` — первый шаг
сейчас, остальное — обычной очередью (`dispatch`, шагами). Затем сразу — последователи.

Автоследование (`follow_all`) — при опросе WB, если включено (`etalon_follow_auto`): только то,
что разошлось с эталоном, и не больше `follow_max_step` (50%) за раз — крупнее ждёт человека
на странице («Привести к эталону»).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from priceapp import audit, mapping, rates, settings
from priceapp.models import Account, OnecCost, PlatformItem, PriceChange, PriceChangeStatus, ReferencePrice
from priceapp.platforms import card_key
from priceapp.pricing import (OPEN, article_map, article_of, base_price, floor_price, get_rule, item_facts,
                              payout)
from priceapp.timeutils import now_utc

FOLLOWERS = ("kit", "lamoda", "ozon")
SHIFT_KEYS = {"kit": settings.ETALON_SHIFT_KIT, "lamoda": settings.ETALON_SHIFT_LAMODA,
              "ozon": settings.ETALON_SHIFT_OZON}
WITH_DISCOUNT = ("kit", "lamoda")       # там ставим и скидку продавца
SOURCE = "etalon"
ACTOR = "программа (площадки следуют за эталоном)"


def _dec(raw: str) -> Decimal | None:
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except Exception:
        return None


def shift(db: Session, platform: str) -> Decimal | None:
    """На сколько % цена площадки ниже эталона; None — площадка за эталоном не следует."""
    key = SHIFT_KEYS.get(platform)
    return _dec(settings.get(db, key)) if key else None


def corridor(db: Session) -> tuple[int, int]:
    lo = _dec(settings.get(db, settings.ETALON_DISC_MIN)) or Decimal(35)
    hi = _dec(settings.get(db, settings.ETALON_DISC_MAX)) or Decimal(50)
    return int(lo), int(hi)


def auto_enabled(db: Session) -> bool:
    return settings.get(db, settings.ETALON_FOLLOW_AUTO) == "1"


def tolerance(db: Session) -> int:
    """Рублей разницы с целью, которые расхождением не считаются (округление, курс)."""
    v = _dec(settings.get(db, settings.ETALON_TOLERANCE_RUB))
    return int(v) if v is not None and v >= 0 else 100


def max_step(db: Session) -> Decimal:
    return _dec(settings.get(db, settings.FOLLOW_MAX_STEP)) or Decimal(50)


def disc_of(price: int | None, sale: int | None) -> int | None:
    """Скидка продавца целым процентом."""
    if not price or not sale:
        return None
    return int(((1 - Decimal(sale) / Decimal(price)) * 100).quantize(Decimal(1), ROUND_HALF_UP))


def list_for(target: int, cur_disc: int | None, lo: int, hi: int) -> tuple[int, int]:
    """Цена до скидки и целая скидка: нынешняя, если в коридоре, иначе ближняя граница; цена до
    скидки вверх до рубля — цена со скидкой не ниже цели."""
    d = cur_disc if cur_disc is not None and lo <= cur_disc <= hi else (lo if (cur_disc or 0) < lo else hi)
    return math.ceil(target * 100 / (100 - d) - 1e-9), d


def target_of(etalon: int, pct: Decimal) -> int:
    return int((Decimal(etalon) * (1 - pct / 100)).quantize(Decimal(1), ROUND_HALF_UP))


# --- эталон по артикулам ----------------------------------------------------------------------

@dataclass
class WbCard:
    account: Account
    nm: str
    item_ids: list[str]
    rows: list
    ref_list: int | None
    ref_disc: int | None
    cur_list: int | None
    cur_sale: int | None

    @property
    def ref_sale(self) -> int | None:
        if not self.ref_list or self.ref_disc is None:
            return None
        return int((Decimal(self.ref_list) * (100 - self.ref_disc) / 100).quantize(Decimal(1), ROUND_HALF_UP))


@dataclass
class Etalon:
    article: str
    sale: int | None = None                      # эталон: средняя цена после скидки продавца
    cards: list[WbCard] = field(default_factory=list)
    name: str = ""


def etalons(db: Session) -> dict[str, Etalon]:
    """Артикул 1С -> эталон и его карточки WB."""
    from priceapp.reference import ref_discount_int
    from priceapp.reference import ref_sale as _ref_sale
    articles = article_map(db)
    out: dict[str, Etalon] = {}
    sales: dict[str, list[int]] = {}
    for a in db.query(Account).filter(Account.is_active.is_(True), Account.platform == "wb").order_by(Account.name):
        refs = {r.item_id: r for r in db.query(ReferencePrice).filter(ReferencePrice.account_id == a.id)}
        cards: dict[tuple[str, str], WbCard] = {}
        for item_id, rows in mapping.account_items(db, a.id).items():
            art = article_of(articles, item_id)
            et = out.setdefault(art, Etalon(art))
            if not et.name:
                et.name = next((r.name for r in rows if getattr(r, "name", "")), "")
            ref = refs.get(item_id)
            if ref is not None and _ref_sale(ref):
                sales.setdefault(art, []).append(_ref_sale(ref))
            for r in rows:
                nm = card_key("wb", r)
                if not nm.isdigit():
                    continue
                c = cards.get((art, nm))
                if c is None:
                    c = cards[(art, nm)] = WbCard(a, nm, [], [], None, None, None, None)
                    out[art].cards.append(c)
                if item_id not in c.item_ids:
                    c.item_ids.append(item_id)
                c.rows.append(r)
                if r.current_price and (c.cur_list is None or r.current_price > c.cur_list):
                    c.cur_list, c.cur_sale = r.current_price, r.current_sale_price or r.current_price
                if ref is not None and (c.ref_list is None or ref.ref_price > c.ref_list):
                    c.ref_list, c.ref_disc = ref.ref_price, ref_discount_int(ref)
    for art, v in sales.items():
        out[art].sale = int((Decimal(sum(v)) / len(v)).quantize(Decimal(1), ROUND_HALF_UP))
    return out


# --- последователи ----------------------------------------------------------------------------

@dataclass
class FCard:
    platform: str
    account: Account
    key: str                     # Kit — variant, Ламода — parentSku, Ozon — offer_id
    rows: list
    item_ids: list[str]
    articles: set
    cur_list: int | None = None
    cur_sale: int | None = None
    target: int | None = None    # цена после скидки продавца
    new_list: int | None = None
    disc: int | None = None
    skip: str = ""
    tol: int = 0                 # допуск, ₽: разница цены после скидки с целью в его пределах — не расхождение

    @property
    def same(self) -> bool:
        if self.skip or self.target is None:
            return False
        if self.platform in WITH_DISCOUNT:
            return self.cur_list == self.new_list and self.cur_sale == self.target
        return self.cur_list == self.target

    @property
    def follows(self) -> bool:
        """Держит эталон с допуском: цена после скидки отличается от цели не больше чем на `tol` ₽.
        Так считают счётчики, «Привести к эталону» и автоследование; смена эталона руками
        отправляет при любом отличии (`same`)."""
        if self.skip or self.target is None or not self.cur_sale:
            return self.same
        return abs(self.cur_sale - self.target) <= self.tol

    @property
    def change_pct(self) -> Decimal | None:
        if not self.cur_sale or not self.target:
            return None
        return (Decimal(self.target) / Decimal(self.cur_sale) - 1) * 100


def follower_plan(db: Session, ets: dict[str, Etalon] | None = None, only: set | None = None) -> list[FCard]:
    """Карточки площадок-последователей, их цель и что уйдёт. `only` — только эти артикулы."""
    ets = ets if ets is not None else etalons(db)
    articles = article_map(db)
    lo, hi = corridor(db)
    tol = tolerance(db)
    rate = rates.current(db)
    costs = {c.item_id: c.cost_usd for c in db.query(OnecCost)}
    out: list[FCard] = []
    for platform in FOLLOWERS:
        pct = shift(db, platform)
        if pct is None:
            continue
        rule = get_rule(db, platform)
        for a in db.query(Account).filter(Account.is_active.is_(True), Account.platform == platform):
            cards: dict[str, FCard] = {}
            for item_id, rows in mapping.account_items(db, a.id).items():
                art = article_of(articles, item_id)
                if only is not None and art not in only:
                    continue
                for r in rows:
                    key = card_key(platform, r)
                    if not key:
                        continue
                    c = cards.get(key)
                    if c is None:
                        c = cards[key] = FCard(platform, a, key, [], [], set(), tol=tol)
                    c.rows.append(r)
                    if item_id not in c.item_ids:
                        c.item_ids.append(item_id)
                    c.articles.add(art)
                    if r.current_price and (c.cur_list is None or r.current_price > c.cur_list):
                        c.cur_list, c.cur_sale = r.current_price, r.current_sale_price or r.current_price
            for c in cards.values():
                targets = [target_of(ets[art].sale, pct) for art in c.articles if art in ets and ets[art].sale]
                if len(targets) < len(c.articles):
                    c.skip = "нет эталона WB"
                    out.append(c)
                    continue
                c.target = max(targets)
                if platform in WITH_DISCOUNT:
                    c.new_list, c.disc = list_for(c.target, disc_of(c.cur_list, c.cur_sale), lo, hi)
                else:
                    c.new_list, c.disc = c.target, None
                # пол — по каждому размеру карточки, со скидкой, которая уйдёт
                d = Decimal(c.disc or 0) / 100
                for item_id in c.item_ids:
                    base = base_price(costs.get(item_id), rate.usd_rub if rate else None)
                    facts = item_facts(rule, [r for r in c.rows])
                    if base is None or facts.commission is None:
                        continue
                    floor = floor_price(base, rule, facts.commission, d)
                    if c.new_list < floor:
                        c.skip = f"ниже пола: минимум {floor} ₽ до скидки"
                        break
                out.append(c)
    return out


def _lamoda_body(cards: list[FCard], days: int) -> tuple[dict, str, str]:
    start = now_utc().replace(microsecond=0) + timedelta(minutes=2)
    end = start + timedelta(days=days)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
    return ({"country": "RU", "force": False, "prices": [
        {"parentSku": c.key, "price": {"amount": c.new_list * 100, "currency": "RUB"},
         "salePrice": {"amount": c.target * 100, "currency": "RUB"},
         "saleStart": iso(start), "saleEnd": iso(end)} for c in cards]}, iso(start), iso(end))


def send_followers(db: Session, cards: list[FCard], client_factory=None, actor: str = ACTOR) -> dict[str, str]:
    """Отправить последователям цену до скидки и цену со скидкой (Ozon — цену). Принятое сразу
    правим у нас как текущее и как отправленное. Коммитит по кабинету."""
    from priceapp import accounts
    from priceapp.dispatch import _record_sent
    from priceapp.platforms import LAMODA_PUSH_CHUNK, PriceItem, lamoda_push_failures
    out: dict[str, str] = {}
    by_acc: dict[int, list[FCard]] = {}
    for c in cards:
        by_acc.setdefault(c.account.id, []).append(c)
    for group in by_acc.values():
        a = group[0].account
        try:
            client = accounts.client_for(db, a, client_factory)
        except Exception as e:
            out[a.name] = f"не отправлено: {e}"[:300]
            continue
        sent, errors = [], []
        if a.platform == "lamoda":
            days = int(_dec(settings.get(db, settings.ETALON_LAMODA_SALE_DAYS)) or 365)
            for i in range(0, len(group), LAMODA_PUSH_CHUNK):
                chunk = group[i:i + LAMODA_PUSH_CHUNK]
                body, _, _ = _lamoda_body(chunk, days)
                try:
                    resp = client._post("/v2/nomenclatures-prices", body)
                except Exception as e:
                    errors.append(str(e)[:300])
                    continue
                failed, unattributed = lamoda_push_failures(resp or {}, [c.key for c in chunk])
                if unattributed:
                    failed.update({c.key: unattributed for c in chunk if c.key not in failed})
                for c in chunk:
                    if c.key in failed:
                        errors.append(f"{c.key}: {failed[c.key]}"[:300])
                    else:
                        sent.append(c)
        else:
            items, owner = [], {}
            for c in group:
                sale = c.target if a.platform in WITH_DISCOUNT else None
                for r in c.rows:
                    items.append(PriceItem(r.barcode, c.new_list, r.external_id, r.article, sale))
                    owner[r.barcode] = c
            res = client.push_prices(items) if items else {"ok": [], "errors": []}
            ok = set(res.get("ok", []))
            sent = [c for c in group if any(r.barcode in ok for r in c.rows)]
            errors = [str(e.get("detail", e))[:300] for e in res.get("errors", [])]
        for c in sent:
            for r in c.rows:
                r.current_price = c.new_list
                r.current_sale_price = c.target if a.platform in WITH_DISCOUNT else c.new_list
            for item_id in c.item_ids:
                _record_sent(db, item_id, a.id, c.new_list)
        out[a.name] = f"отправлено карточек: {len(sent)} из {len(group)}" + (
            f"; ошибки ({len(errors)}): " + "; ".join(errors[:5]) if errors else "")
        sample = ", ".join(f"{c.key} {c.cur_sale}->{c.target}" for c in sent[:15])
        audit.log(db, actor, "etalon_follow", a.name, f"{out[a.name]}. {sample}"[:1000])
        db.commit()
    return out


# --- смена эталона -------------------------------------------------------------------------

def wb_plan(db: Session, et: Etalon, sale: int) -> list[dict]:
    """Что уйдёт на карточки WB при новом эталоне `sale`: цена до скидки, скидка, первый шаг."""
    lo, hi = corridor(db)
    drop = _dec(settings.get(db, settings.WB_MAX_DROP_PERCENT)) or Decimal(33)
    rule = get_rule(db, "wb")
    rate = rates.current(db)
    costs = {c.item_id: c.cost_usd for c in db.query(OnecCost).filter(
        OnecCost.item_id.in_([i for c in et.cards for i in c.item_ids]))}
    out = []
    for c in et.cards:
        new_list, d = list_for(sale, disc_of(c.cur_list, c.cur_sale), lo, hi)
        step_sale = math.ceil(Decimal(c.cur_sale) * (1 - drop / 100)) if c.cur_sale else None
        step = None
        if step_sale and sale < step_sale:
            step = math.ceil(Decimal(step_sale) * 100 / (100 - d))
        skip = ""
        facts = item_facts(rule, c.rows)
        for item_id in c.item_ids:
            base = base_price(costs.get(item_id), rate.usd_rub if rate else None)
            if base is None or facts.commission is None:
                continue
            floor = floor_price(base, rule, facts.commission, Decimal(d) / 100)
            if new_list < floor:
                skip = f"ниже пола: минимум {floor} ₽ до скидки при скидке {d}%"
                break
        out.append({"card": c, "new_list": new_list, "disc": d, "step": step, "skip": skip,
                    "commission": facts.commission})
    return out


def set_etalon(db: Session, art: str, sale: int, actor: str, client_factory=None) -> list[str]:
    """Новый эталон артикула: WB — сразу (первый шаг при большом снижении, остальное — очередью),
    запись эталона, затем последователи. Возвращает строки отчёта; карточку ниже пола не трогает
    и называет. Коммитит."""
    from priceapp import accounts
    from priceapp.platforms import WB_PRICES
    from priceapp.reference import _mark_dirty, snapshot_if_dirty
    ets = etalons(db)
    et = ets.get(art)
    if et is None or not et.cards:
        return [f"{art}: на WB нет карточек этого артикула — эталон не к чему привязать."]
    plan = wb_plan(db, et, sale)
    blocked = [p for p in plan if p["skip"]]
    if blocked:
        return [f"{art}: эталон {sale} ₽ не записан — {p['card'].account.name} {p['card'].nm}: {p['skip']}"
                for p in blocked]
    rate = rates.current(db)
    report = []
    now = now_utc()
    by_acc: dict[int, list[dict]] = {}
    for p in plan:
        by_acc.setdefault(p["card"].account.id, []).append(p)
    for group in by_acc.values():
        a = group[0]["card"].account
        # Эталон — сразу: правило возврата иначе вернуло бы прежний, пока идут шаги.
        for p in group:
            c = p["card"]
            for item_id in c.item_ids:
                ref = (db.query(ReferencePrice).filter(ReferencePrice.account_id == a.id,
                                                       ReferencePrice.item_id == item_id).first())
                if ref is None:
                    ref = ReferencePrice(account_id=a.id, item_id=item_id, ref_price=p["new_list"])
                    db.add(ref)
                ref.ref_price = p["new_list"]
                ref.discount = (Decimal(p["disc"]) / 100).quantize(Decimal("0.0001"))
                ref.commission_percent = p["commission"]
                if p["commission"] is not None:
                    ref.ref_payout = payout(Decimal(sale), p["commission"]).quantize(Decimal("0.01"))
                ref.usd_rub = rate.usd_rub if rate else None
                ref.source = "manual"
                ref.set_at = now
            # прежние нерешённые и подтверждённые решения по этим товарам — сняты: уйдёт эталон
            for ch in db.query(PriceChange).filter(PriceChange.account_id == a.id, PriceChange.item_id.in_(c.item_ids),
                                                   PriceChange.is_test.is_(False),
                                                   PriceChange.status.in_(OPEN + (PriceChangeStatus.approved.value,))):
                ch.status = PriceChangeStatus.rejected.value
                ch.decided_by, ch.decided_at = actor, now
                ch.note = "снято: новый эталон"
        db.flush()
        _mark_dirty()
        try:
            client = accounts.client_for(db, a, client_factory)
            body = {"data": [{"nmID": int(p["card"].nm), "price": p["step"] or p["new_list"], "discount": p["disc"]}
                             for p in group]}
            data = client._post(f"{WB_PRICES}/api/v2/upload/task", body)
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(data.get("errorText") or "WB вернул error=true")
        except Exception as e:
            report.append(f"{a.name}: эталон записан, но WB не принял отправку — {e}"[:300]
                          + ". Правило эталона вернёт цену при следующем опросе WB.")
            db.commit()
            continue
        for p in group:
            c, price = p["card"], p["step"] or p["new_list"]
            for r in c.rows:
                r.current_price = price
                r.current_sale_price = int((Decimal(price) * (100 - p["disc"]) / 100).quantize(Decimal(1), ROUND_HALF_UP))
            if p["step"]:
                # остальное снижение — обычной очередью, шагами (dispatch.STEP_PAUSE)
                from priceapp.dispatch import STEP_PAUSE
                for item_id in c.item_ids:
                    db.add(PriceChange(item_id=item_id, account_id=a.id, barcode=c.rows[0].barcode,
                                       old_price=price, new_price=p["new_list"], source=SOURCE,
                                       status=PriceChangeStatus.approved.value, decided_by=actor, decided_at=now,
                                       next_attempt_at=now + STEP_PAUSE,
                                       note=f"новый эталон {sale} ₽: снижение шагами, отправлен шаг {price} ₽"))
            report.append(f"WB {a.name} {c.nm}: {c.cur_list}/{c.cur_sale} -> {p['new_list']} ₽, скидка {p['disc']}%"
                          + (f" (снижение шагами: сейчас {price} ₽)" if p["step"] else ""))
        audit.log(db, actor, "etalon_set", a.name, f"{art}: новый эталон {sale} ₽; " + "; ".join(report[-len(group):])[:900])
        db.commit()
    snapshot_if_dirty(db, "эталон изменён вручную")
    fol = [c for c in follower_plan(db, etalons(db), only={art}) if not c.skip and not c.same]
    for name, res in send_followers(db, fol, client_factory, actor).items():
        report.append(f"{name}: {res}")
    skipped = [c for c in follower_plan(db, only={art}) if c.skip]
    for c in skipped:
        report.append(f"{c.account.name} {c.key}: не отправлено — {c.skip}")
    return report


def follow_all(db: Session, client_factory=None) -> dict:
    """Автоследование (опрос WB): только разошедшееся с эталоном и не больше `follow_max_step`
    за раз; крупнее — ждёт «Привести к эталону» на странице."""
    st = {"enabled": auto_enabled(db), "sent": {}, "big": 0, "floor": 0}
    if not st["enabled"]:
        return st
    lim = max_step(db)
    cards = []
    for c in follower_plan(db):
        if c.skip:
            st["floor"] += c.skip.startswith("ниже пола")
            continue
        if c.follows:
            continue
        ch = c.change_pct
        if ch is not None and abs(ch) > lim:
            st["big"] += 1
            continue
        cards.append(c)
    if cards:
        st["sent"] = send_followers(db, cards, client_factory)
    return st


def summary(st: dict) -> str:
    if not st.get("enabled"):
        return "Автоследование за эталоном выключено (страница «Эталонные цены»)."
    parts = [f"{k}: {v}" for k, v in st.get("sent", {}).items()] or ["расхождений с эталоном нет"]
    if st.get("big"):
        parts.append(f"ждут подтверждения (шаг больше допустимого): {st['big']}")
    if st.get("floor"):
        parts.append(f"ниже пола, не отправлено: {st['floor']}")
    return "Следование за эталоном: " + "; ".join(parts) + "."

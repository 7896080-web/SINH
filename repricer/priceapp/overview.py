"""То, что оператор должен видеть, не обходя все вкладки.

* `card_key` / `change_context` — цена КАРТОЧКИ. У WB (nmID) и Lamoda (parentSku)
  цена ставится на карточку, у Ozon — на offer_id: размеры с разными расчётными
  ценами уходят одной, наибольшей (`platforms.*.push_prices`). Видно это было
  только в журнале, после отправки; теперь — до подтверждения, тем же правилом.
* `proposals_summary` — итог по отбору перед подтверждением.
* `rate_shift` — насколько курс ушёл от того, по которому считали.
* `attention` — список «что требует внимания» со ссылками прямо на строки.

Модуль только читает.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from priceapp import platforms, rates, settings
from priceapp.models import (Account, ApiCredential, PlatformItem, PriceChange, PriceChangeStatus)
from priceapp.timeutils import now_utc

PENDING = ("proposed", "blocked", "approved")
STALE = timedelta(hours=26)


def card_key(platform: str, item: PlatformItem | None) -> str:
    """Чем площадка адресует цену — тем же правилом, что `push_prices`."""
    if item is None:
        return ""
    if platform == "wb":
        return (item.external_id or "").split(":")[0]
    if platform == "lamoda":
        return platforms.lamoda_parent(item.external_id)
    if platform == "ozon":
        return item.article or item.barcode
    return item.external_id or item.barcode


@dataclass
class ChangeInfo:
    card: str = ""
    card_price: int | None = None     # что уйдёт на карточку, если подтвердить всё ждущее
    card_size: int = 1                # сколько SKU карточки сейчас в предложениях
    min_price: int | None = None      # минимальная цена площадки
    price_status: str | None = None   # что площадка говорит о текущей цене


def _items_by_barcode(db: Session, account_ids, barcodes) -> dict:
    out = {}
    barcodes = list(set(barcodes))
    for i in range(0, len(barcodes), 500):
        for it in db.query(PlatformItem).filter(PlatformItem.account_id.in_(list(account_ids)),
                                                PlatformItem.barcode.in_(barcodes[i:i + 500])):
            out[(it.account_id, it.barcode)] = it
    return out


def change_context(db: Session, changes: list[PriceChange]) -> dict[int, ChangeInfo]:
    """Для показанных предложений: цена карточки, считая ВСЕ ждущие предложения
    тех же кабинетов (на карточку уйдёт наибольшая из них, кроме заблокированных
    полом), минимальная цена и статус площадки."""
    if not changes:
        return {}
    accounts = {c.account_id for c in changes}
    platform_of = {a.id: a.platform for a in db.query(Account).filter(Account.id.in_(list(accounts)))}
    pending = (db.query(PriceChange).filter(PriceChange.account_id.in_(list(accounts)),
                                            PriceChange.status.in_(PENDING),
                                            PriceChange.is_test.is_(False)).all())
    allc = {c.id: c for c in pending}
    allc.update({c.id: c for c in changes})
    items = _items_by_barcode(db, accounts, [c.barcode for c in allc.values()])
    groups: dict[tuple, list[PriceChange]] = defaultdict(list)
    keys = {}
    for c in allc.values():
        key = (c.account_id, card_key(platform_of.get(c.account_id, ""), items.get((c.account_id, c.barcode))))
        keys[c.id] = key
        if c.status in PENDING and c.block_reason != "floor" and key[1]:
            groups[key].append(c)
    out = {}
    for c in changes:
        key = keys[c.id]
        group = groups.get(key, [])
        it = items.get((c.account_id, c.barcode))
        out[c.id] = ChangeInfo(card=key[1], card_price=max((g.new_price for g in group), default=None),
                               card_size=len(group) or 1, min_price=it.min_price if it else None,
                               price_status=it.price_status if it else None)
    return out


@dataclass
class Summary:
    total: int = 0
    up: int = 0
    down: int = 0
    new: int = 0
    floor: int = 0
    big_step: int = 0
    below_platform_min: int = 0
    card_raised: int = 0
    min_coef: Decimal | None = None
    avg_coef: Decimal | None = None
    avg_change: float | None = None


def proposals_summary(db: Session, changes: list[PriceChange]) -> Summary:
    s = Summary(total=len(changes))
    ctx = change_context(db, changes)
    coefs, pcts = [], []
    for c in changes:
        if c.old_price is None:
            s.new += 1
        elif c.new_price > c.old_price:
            s.up += 1
        elif c.new_price < c.old_price:
            s.down += 1
        if c.old_price:
            pcts.append((c.new_price - c.old_price) * 100.0 / c.old_price)
        if c.block_reason == "floor":
            s.floor += 1
        elif c.block_reason == "max_change":
            s.big_step += 1
        if c.markup_coef is not None:
            coefs.append(Decimal(str(c.markup_coef)))
        info = ctx.get(c.id)
        if info and info.min_price and c.new_price < info.min_price:
            s.below_platform_min += 1
        if info and info.card_price and info.card_price != c.new_price and c.block_reason != "floor":
            s.card_raised += 1
    if coefs:
        s.min_coef = min(coefs)
        s.avg_coef = (sum(coefs) / len(coefs)).quantize(Decimal("0.01"))
    if pcts:
        s.avg_change = sum(pcts) / len(pcts)
    return s


def rate_alert_percent(db: Session) -> Decimal:
    try:
        return Decimal(settings.get(db, settings.RATE_ALERT_PERCENT).replace(",", "."))
    except Exception:
        return Decimal("2")


def rate_shift(db: Session) -> dict | None:
    """Курс сейчас против курса последнего расчёта. None — сравнивать не с чем
    или сдвиг в пределах порога."""
    cur = rates.current(db)
    last = (db.query(PriceChange).filter(PriceChange.is_test.is_(False), PriceChange.usd_rub.isnot(None))
            .order_by(PriceChange.id.desc()).first())
    if cur is None or last is None:
        return None
    was = Decimal(str(last.usd_rub))
    if was <= 0:
        return None
    pct = (cur.usd_rub - was) * 100 / was
    limit = rate_alert_percent(db)
    if abs(pct) < limit:
        return None
    return {"was": was, "now": cur.usd_rub, "pct": pct.quantize(Decimal("0.1")), "limit": limit,
            "at": last.created_at}


@dataclass
class Item:
    level: str           # bad / warn / ok
    text: str
    link: str = ""
    count: int | None = None


@dataclass
class Attention:
    items: list[Item] = field(default_factory=list)
    heavy_at: str = ""          # когда посчитаны счётчики по товарам (ISO UTC)
    heavy_dirty: bool = False   # данные менялись после подсчёта

    def add(self, level, text, link="", count=None):
        self.items.append(Item(level, text, link, count))


HEAVY_KEY = "attention_heavy"      # JSON: {"at": ISO UTC, "items": [[level, text, link, count], ...]}
DIRTY_KEY = "attention_dirty"      # "1" — данные менялись после подсчёта, пора пересчитать


def mark_dirty(db: Session) -> None:
    """Пометить счётчики по товарам устаревшими (зовут после любых правок)."""
    settings.put(db, DIRTY_KEY, "1")


def _heavy(db: Session, product_rows) -> list[Item]:
    """Счётчики ПО ТОВАРАМ — сборка сопоставления и расчёт цены по каждому
    кабинету. На боевом каталоге это десятки секунд, поэтому их считает фон
    (`refresh_heavy`), а страница показывает готовое."""
    a = Attention()
    for acc in db.query(Account).filter(Account.is_active.is_(True)).order_by(Account.platform, Account.name):
        name = f"{acc.name} ({platforms.PLATFORMS.get(acc.platform, acc.platform)})"
        from priceapp import mapping
        not_in_1c = mapping.counts(mapping.build(db, acc.id)).get("not_in_1c", 0)
        if not_in_1c and acc.catalog_loaded_at:
            a.add("warn", f"{name}: баркоды площадки, которых нет в 1С — цена по ним не считается",
                  f"/mapping?view=status&account_id={acc.id}&status=not_in_1c", not_in_1c)
        rows = product_rows(db, acc)
        link = f"/prices?view=products&account_id={acc.id}"
        below = sum(1 for r in rows if r["below_floor_now"])
        if below:
            a.add("bad", f"{name}: продаётся ниже пола по текущей цене", f"{link}&flt=below_floor_now", below)
        no_cost = sum(1 for r in rows if r["cost_usd"] is None)
        if no_cost:
            a.add("warn", f"{name}: нет себестоимости из 1С — цена не считается", f"{link}&flt=no_cost", no_cost)
        if acc.guard_min_margin is not None or acc.guard_max_margin is not None:
            from priceapp import guard
            kinds = [guard.classify(r, acc) for r in rows]
            eaten = kinds.count("eaten")
            if eaten:
                a.add("bad", f"{name}: маржинальность ниже диапазона безопасности, а цена на площадке наша — "
                             "съедает скидка продавца или акция: снимите скидку или выйдите из акции",
                      f"{link}&flt=guard_eaten", eaten)
            stuck = kinds.count("stuck")
            if stuck:
                a.add("bad", f"{name}: ниже диапазона безопасности, а вернуть нечем — наша расчётная цена не выше "
                             "текущей или сама ниже «от»: поднимите наценку", f"{link}&flt=guard_stuck", stuck)
            below = kinds.count("below")
            if below:
                a.add("warn", f"{name}: ниже диапазона безопасности — наша цена вернётся при запуске программы "
                              "и раз в сутки (или «Вернуть цены по диапазонам» на «Правилах»)",
                      f"{link}&flt=guard_below", below)
            above = kinds.count("above")
            if above:
                a.add("ok", f"{name}: маржинальность выше диапазона безопасности", f"{link}&flt=guard_above", above)
        bad_status = sum(1 for r in rows if r["price_status"] in ("QUARANTINE", "ERROR"))
        if bad_status:
            a.add("bad", f"{name}: площадка держит цену на карантине или с ошибкой",
                  f"{link}&flt=platform_status", bad_status)
        under_min = sum(1 for r in rows if r["min_price"] and r["price"] and r["price"] < r["min_price"])
        if under_min:
            a.add("warn", f"{name}: расчётная цена ниже минимальной цены площадки",
                  f"{link}&flt=below_platform_min", under_min)
    return a.items


def _orphans(db: Session) -> list[Item]:
    """Наценки, которые не находят ни одного товара: ключ — строка (артикул 1С,
    название категории площадки), и переименование на той стороне молча роняет
    товары на умолчание площадки."""
    from priceapp.models import ArticleCoef, CategoryTarget
    from priceapp.pricing import article_map
    out = []
    arts = db.query(ArticleCoef).all()
    if arts:
        known = set(article_map(db).values()) | set(article_map(db))
        lost = sorted({c.article for c in arts if c.article not in known})
        if lost:
            out.append(Item("warn", "Наценки артикулов, которых больше нет в справочнике 1С: "
                                    + ", ".join(lost[:10]) + (" …" if len(lost) > 10 else ""),
                            "/sku-prices", len(lost)))
    cats = db.query(CategoryTarget).all()
    if cats:
        present = {(p, c) for p, c in db.query(Account.platform, PlatformItem.category)
                   .join(PlatformItem, PlatformItem.account_id == Account.id).distinct()}
        lost = sorted({f"{platforms.PLATFORMS.get(c.platform, c.platform)}: {c.category}" for c in cats
                       if (c.platform, c.category) not in present})
        if lost:
            out.append(Item("warn", "Наценки категорий, которых больше нет в каталогах (площадка переименовала?) — "
                                    "товары ушли на умолчание: " + "; ".join(lost[:10]),
                            "/sku-prices?by=category", len(lost)))
    return out


def refresh_heavy(db: Session, product_rows) -> None:
    import json
    items = _heavy(db, product_rows) + _orphans(db)
    settings.put(db, HEAVY_KEY, json.dumps({"at": now_utc().isoformat(),
                                            "items": [[i.level, i.text, i.link, i.count] for i in items]},
                                           ensure_ascii=False))
    settings.put(db, DIRTY_KEY, "")
    db.commit()


def heavy_cached(db: Session) -> tuple[list[Item] | None, str, bool]:
    """(строки или None, когда посчитаны, устарели ли)."""
    import json
    raw = settings.get(db, HEAVY_KEY)
    if not raw:
        return None, "", True
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "", True
    return [Item(*x) for x in data.get("items", [])], data.get("at", ""), settings.get(db, DIRTY_KEY) == "1"


JOB_NAMES = {"onec_exchange": "обмен с 1С", "price_dispatch": "отправка цен", "rate": "курс ЦБ",
             "daily_refresh": "суточное обновление (каталоги, цены, диапазоны)", "backup": "копия базы",
             "attention": "счётчики «Внимания»"}


def _health(db: Session, a: Attention) -> None:
    """Фоновые задания, 1С и свежесть данных из 1С — то, что раньше было видно только
    на «Диагностике»: остановилась отправка цен — узнавали случайно."""
    from priceapp.models import OnecTask, OnecTaskStatus
    from priceapp.workers import heartbeat
    for line in heartbeat.stale_workers(db):
        name = line.split(":", 1)[0]
        a.add("bad", f"Фоновое задание «{JOB_NAMES.get(name, name)}» не отвечает: {line.split(':', 1)[1].strip()}",
              "/diagnostics")
    stale = {line.split(":", 1)[0] for line in heartbeat.stale_workers(db)}
    for name, text, is_error in heartbeat.failed_or_noted(db):
        if name in stale:
            continue
        a.add("bad" if is_error else "warn",
              f"«{JOB_NAMES.get(name, name)}»: {'ошибка' if is_error else 'оговорка'} — {text}", "/diagnostics")
    bad = db.query(OnecTask).filter(OnecTask.status.in_((OnecTaskStatus.failed.value,
                                                         OnecTaskStatus.timeout.value)),
                                    OnecTask.created_at > now_utc() - timedelta(days=2)).count()
    if bad:
        a.add("warn", "Задания 1С без ответа или с отказом за двое суток", "/diagnostics", bad)
    for key, what in ((settings.COST_LOADED_AT, "Себестоимость из 1С"), (settings.DICT_LOADED_AT, "Справочник баркодов 1С")):
        raw = settings.get(db, key)
        try:
            from datetime import datetime
            at = datetime.fromisoformat(raw) if raw else None
        except ValueError:
            at = None
        if at is None:
            a.add("warn", f"{what} ещё не загружались", "/diagnostics")
        elif now_utc() - at > STALE:
            a.add("warn", f"{what} не обновлялись больше суток (последний раз {at:%d.%m %H:%M} UTC)",
                  "/diagnostics")


def attention(db: Session, product_rows, background_alive: bool = False) -> Attention:
    """Что требует внимания, по убыванию цены ошибки. Лёгкое (курс, предложения,
    свежесть данных, фоновые задания) — сразу; счётчики по товарам — из кэша, если
    его обновляет фон, иначе (кэша нет или он устарел без фона) — тут же.
    `product_rows` — из роутера цен: число здесь и строки на «Товарах» одни."""
    a = Attention()
    rate = rates.current(db)
    if rate is None:
        a.add("bad", "Курса доллара нет — цены не считаются", "/rate")
    shift = rate_shift(db)
    if shift:
        a.add("warn", f"Курс изменился на {shift['pct']}% с последнего расчёта ({shift['was']:.2f} → "
                      f"{shift['now']:.2f} ₽) — новые цены видны на «Ценах товаров», там их и передавать",
              "/sku-prices?flt=changes")
    base = PriceChange.is_test.is_(False)
    n = db.query(PriceChange).filter(base, PriceChange.status == PriceChangeStatus.error.value).count()
    if n:
        a.add("bad", "Площадка не приняла цену", "/prices?view=log&status=error", n)
    n = db.query(PriceChange).filter(base, PriceChange.status == "proposed").count()
    if n:
        a.add("warn", "Пересчёт всех цен: предложения ждут решения", "/prices?view=proposals&status=proposed", n)
    n = db.query(PriceChange).filter(base, PriceChange.status == "blocked").count()
    if n:
        a.add("warn", "Пересчёт всех цен: заблокировано (пол или большой шаг)", "/prices?view=proposals&status=blocked", n)
    n = db.query(PriceChange).filter(base, PriceChange.status == "approved").count()
    if n:
        a.add("ok", "Подтверждено и ждёт отправки (уходит в течение пары минут)", "/prices?view=log&status=approved", n)

    _health(db, a)
    now = now_utc()
    for acc in db.query(Account).filter(Account.is_active.is_(True)).order_by(Account.platform, Account.name):
        has_keys = db.query(ApiCredential.id).filter(ApiCredential.account_id == acc.id).first() is not None
        name = f"{acc.name} ({platforms.PLATFORMS.get(acc.platform, acc.platform)})"
        if not has_keys:
            # Без ключей молчат только проверки свежести — товары, цены и расхождения
            # по уже загруженным данным смотрим всё равно.
            a.add("warn", f"{name}: не заданы ключи", "/api-keys")
        else:
            if acc.last_check_ok is False:
                a.add("bad", f"{name}: проверка ключей не прошла — {acc.last_check_message[:120]}", "/api-keys")
            if not acc.catalog_loaded_at or now - acc.catalog_loaded_at > STALE:
                a.add("warn", f"{name}: каталог не загружался больше суток", "/api-keys")
            if acc.platform in platforms.READS_PRICES and (
                    not acc.prices_loaded_at or now - acc.prices_loaded_at > STALE):
                a.add("warn", f"{name}: текущие цены не загружались больше суток",
                      f"/prices?view=products&account_id={acc.id}")
    items, at, dirty = heavy_cached(db)
    if items is None or (dirty and not background_alive):
        refresh_heavy(db, product_rows)
        items, at, dirty = heavy_cached(db)
    a.items.extend(items or [])
    a.heavy_at, a.heavy_dirty = at, dirty
    return a

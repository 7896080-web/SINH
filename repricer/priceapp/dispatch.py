"""Отправка ПОДТВЕРЖДЁННЫХ цен на площадки.

Уходит только `approved` и только не-тестовое. Перед отправкой пол наценки
проверяется ЕЩЁ РАЗ — по текущему курсу, себестоимости, комиссии товара (тариф
площадки + надбавка) и ТЕКУЩЕЙ скидке продавца на площадке: между
подтверждением и отправкой доллар мог вырасти, 1С прислать новую себестоимость,
площадка — сменить тариф, а в кабинете могли поставить скидку под акцию.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from priceapp import accounts, rates, reference
from priceapp.models import (Account, OnecCost, PlatformItem, PriceChange, PriceChangeStatus,
                             ProductPrice)
from priceapp.platforms import PriceItem, card_key
from priceapp.pricing import BLOCK_FLOOR, _ru, base_price, floor_price, get_rule, item_facts
from priceapp.timeutils import now_utc

MAX_ATTEMPTS = 5
# Подтверждённое, но не ушедшее дольше этого — устарело: курс, себестоимость,
# решение человека могли смениться (кабинет был выключен, площадка не отвечала).
# Такое не отправляется, а отклоняется с причиной — подтвердить заново.
STALE_AFTER = timedelta(hours=24)
RETRY_MINUTES = (1, 2, 5, 15)
# Между шагами большого снижения на WB: площадке нужно время применить шаг.
STEP_PAUSE = timedelta(minutes=10)


def _max_drop(db: Session) -> Decimal:
    from priceapp import settings
    try:
        v = Decimal((settings.get(db, settings.WB_MAX_DROP_PERCENT) or "33").replace(",", "."))
    except Exception:
        v = Decimal(33)
    return v if 0 < v < 50 else Decimal(33)


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(minutes=RETRY_MINUTES[min(max(attempts, 1), len(RETRY_MINUTES)) - 1])


def _record_sent(db: Session, item_id: str, account_id: int, price: int) -> None:
    pp = db.query(ProductPrice).filter(ProductPrice.item_id == item_id,
                                       ProductPrice.account_id == account_id).first()
    if pp is None:
        pp = ProductPrice(item_id=item_id, account_id=account_id)
        db.add(pp)
        db.flush()   # autoflush выключен — следующий запрос должен видеть строку
    pp.last_sent_price = price
    pp.last_sent_at = now_utc()


LAMODA_REFUSED = ("Ламода — цены ставит страница «Эталонные цены» (цена со скидкой и даты скидки); "
                  "общая отправка шлёт только цену и сбила бы скидку")


def _commission_ok(c) -> bool:
    return c is not None and Decimal(0) <= c < Decimal(100)


def run_account(db: Session, account: Account, client) -> dict:
    """Отправка подтверждённого по кабинету. Цена ставится на КАРТОЧКУ (WB nmID,
    Ozon offer_id, Lamoda parentSku; у Kit — вариант), поэтому карточка
    собирается ЦЕЛИКОМ: размеры вне очереди участвуют своей последней
    отправленной (или текущей на площадке) ценой, уходит наибольшая, и пол
    проверяется у КАЖДОГО размера по цене, которая реально уйдёт. Иначе
    подтверждённый дешёвый размер утянул бы карточку — а с ней и дорогой размер —
    ниже его пола, и никто бы этого не проверил."""
    from priceapp import mapping

    if account.platform == "lamoda":
        # Ламоде нужна цена со скидкой и даты скидки — их ставит страница «Эталонные цены»;
        # голая `price` отсюда сбила бы скидку. Отказ здесь — одна точка на ВСЕ пути
        # (кнопка, файл предложений, диапазон безопасности).
        refused = (db.query(PriceChange)
                   .filter(PriceChange.account_id == account.id,
                           PriceChange.status == PriceChangeStatus.approved.value,
                           PriceChange.is_test.is_(False)).all())
        for ch in refused:
            ch.status = PriceChangeStatus.rejected.value
            ch.note = LAMODA_REFUSED[:255]
        db.commit()
        return {"refused": len(refused)} if refused else {}

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
    rate = rates.current(db)
    now = now_utc()
    for item_id, ch in list(latest.items()):
        decided = ch.decided_at or ch.created_at
        if decided is not None and now - decided > STALE_AFTER:
            ch.status = PriceChangeStatus.rejected.value
            from priceapp.templating import local
            ch.note = (f"устарело: подтверждено {local(decided)} и не ушло за сутки — "
                       "пересчитайте и подтвердите заново")[:255]
            del latest[item_id]
    if not latest:
        db.commit()
        return {"stale": True}
    if rate is None:
        # Без курса пол не проверить — не отправляем вовсе, ждём курса.
        for ch in latest.values():
            ch.last_error = "не отправлено: нет курса доллара — пол не проверить (страница «Курс $»)"
        db.commit()
        return {"waiting": len(latest), "reason": "нет курса"}

    items = mapping.account_items(db, account.id)          # item_id -> строки каталога кабинета
    members: dict[str, set[str]] = {}                       # карточка -> SKU 1С на ней
    for item_id, rows in items.items():
        for r in rows:
            k = card_key(account.platform, r)
            if k:
                members.setdefault(k, set()).add(item_id)
    involved = set(latest) | {i for k in members for i in members[k]}
    costs = {c.item_id: c for c in db.query(OnecCost).filter(OnecCost.item_id.in_(list(involved)))}
    pps = {p.item_id: p for p in db.query(ProductPrice).filter(ProductPrice.account_id == account.id)}

    def size_label(item_id: str) -> str:
        rows = items.get(item_id) or []
        return f"{rows[0].article} {rows[0].size}".strip() if rows else item_id

    # Позиция в очереди -> строка каталога и карточка. Баркод обязан по-прежнему
    # вести на ЭТОТ товар: после перезагрузки справочника он мог уйти к другому.
    cards: dict[str, list[tuple[PriceChange, PlatformItem]]] = {}
    for item_id, ch in latest.items():
        row = next((r for r in items.get(item_id, []) if r.barcode == ch.barcode), None)
        if row is None:
            row = (items.get(item_id) or [None])[0]
        if row is None:
            ch.status = PriceChangeStatus.error.value
            ch.last_error = "товар больше не сопоставлен с каталогом кабинета — обновите каталог и пересчитайте"
            continue
        cards.setdefault(card_key(account.platform, row) or row.barcode, []).append((ch, row))

    items_out, by_barcode, card_of_barcode, card_price, floored, waiting = [], {}, {}, {}, 0, 0
    stepping: dict[str, int] = {}                           # карточка -> цель, к которой идём шагами
    max_drop = _max_drop(db)
    for key, queued in cards.items():
        if any(ch.next_attempt_at is not None and ch.next_attempt_at > now for ch, _ in queued):
            waiting += len(queued)
            continue
        in_queue = {ch.item_id: ch for ch, _ in queued}
        desired = {i: ch.new_price for i, ch in in_queue.items()}
        for sib in members.get(key, set()) - set(in_queue):
            pp = pps.get(sib)
            cur = max((r.current_price for r in items.get(sib, []) if r.current_price), default=None)
            p = (pp.last_sent_price if pp and pp.last_sent_price else None) or cur
            if p:
                desired[sib] = p
        price = max(desired.values())
        problem = None
        # Пол — у КАЖДОГО размера карточки, в том числе у того, чьей цены мы не знаем
        # (новая карточка, кабинет до загрузки цен): цена уйдёт на всю карточку.
        for item_id in sorted(set(desired) | members.get(key, set())):
            facts = item_facts(rule, items.get(item_id, []))
            if not _commission_ok(facts.commission):
                problem = (f"комиссия {facts.commission}% у «{size_label(item_id)}» вне 0–99,99 — "
                           "проверьте тариф и надбавку в правилах")
                break
            base = base_price(costs[item_id].cost_usd, rate.usd_rub) if item_id in costs else None
            if base is None:
                continue
            floor = floor_price(base, rule, facts.commission, facts.discount)
            if price < floor:
                problem = (f"при отправке: цена карточки {price} ₽ ниже пола «{size_label(item_id)}» — "
                           f"минимум {floor} ₽ (курс {rate.usd_rub}, себестоимость {costs[item_id].cost_usd} $, "
                           f"комиссия {_ru(facts.commission)}%"
                           + (f", скидка продавца {_ru((facts.discount * 100).quantize(Decimal('1')))}%"
                              if facts.discount else "") + ")")
                break
        if problem:
            for ch, _ in queued:
                ch.status = PriceChangeStatus.blocked.value
                ch.block_reason = BLOCK_FLOOR
                ch.note = problem[:255]
                floored += 1
            continue
        # WB: снижение цены на 50% и больше одним изменением не применяется, а больше порога
        # карантина категории (33,3% по умолчанию) — уходит в карантин. Большое снижение —
        # ШАГАМИ не больше `wb_max_drop_percent` от нынешней цены карточки; позиция остаётся
        # в очереди и через `STEP_PAUSE` уходит следующим шагом, пока не дойдёт до цели.
        if account.platform == "wb":
            cur = max((r.current_price for sib in members.get(key, set()) for r in items.get(sib, [])
                       if card_key(account.platform, r) == key and r.current_price), default=None)
            floor_step = (int((Decimal(cur) * (1 - max_drop / 100)).to_integral_value(rounding="ROUND_CEILING"))
                          if cur else None)
            if floor_step and price < floor_step:
                stepping[key] = price
                price = floor_step
        card_price[key] = price
        for ch, row in queued:
            sale = None
            if account.platform == "kit":
                # Kit: покупатель платит «цену со скидкой» — отдельное число; прежний процент скидки сохраняется.
                disc = item_facts(rule, items.get(ch.item_id) or [row]).discount
                if disc and disc > 0:
                    sale = int((Decimal(price) * (1 - disc)).quantize(Decimal(1), rounding="ROUND_HALF_UP"))
            items_out.append(PriceItem(row.barcode, price, row.external_id, row.article, sale))
            by_barcode[row.barcode] = ch
            card_of_barcode[row.barcode] = key

    result = client.push_prices(items_out) if items_out else {"ok": [], "errors": [], "sent_prices": {}}
    ok = set(result.get("ok", []))
    sent_prices = result.get("sent_prices") or {}
    errors_text = str(result.get("errors"))[:400]
    # Отказ, который не переживёт повтор (площадка назвала позицию и причину), —
    # закрываем сразу: пять попыток в заведомо тот же ответ только оттянут момент,
    # когда человек узнает.
    terminal = {b: e.get("detail", "") for e in result.get("errors", []) if e.get("terminal")
                for b in e.get("items", [])}
    accepted_cards: dict[str, int] = {}
    stepped_cards: dict[str, int] = {}
    for barcode, ch in by_barcode.items():
        ch.attempts += 1
        if barcode in terminal and barcode not in ok:
            ch.status = PriceChangeStatus.error.value
            ch.last_error = str(terminal[barcode])[:400]
            continue
        if barcode in ok and card_of_barcode[barcode] in stepping:
            key = card_of_barcode[barcode]
            step = card_price[key]
            ch.attempts -= 1                                  # шаг — не неудачная попытка
            ch.next_attempt_at = now_utc() + STEP_PAUSE
            ch.note = (f"снижение шагами: отправлено {step} ₽, цель {stepping[key]} ₽ — "
                       f"следующий шаг через {int(STEP_PAUSE.total_seconds() // 60)} мин")[:255]
            stepped_cards[key] = step
            continue
        if barcode in ok:
            price = int(sent_prices.get(barcode, card_price[card_of_barcode[barcode]]))
            ch.status = PriceChangeStatus.sent.value
            ch.sent_at = now_utc()
            ch.next_attempt_at = None
            if price != ch.new_price:
                ch.note = f"площадка приняла {price} ₽ — одна цена на карточку, наибольшая из её размеров"
            _record_sent(db, ch.item_id, account.id, price)
            reference.record(db, account, ch.item_id, price, items.get(ch.item_id) or [], rule, rate)
            accepted_cards[card_of_barcode[barcode]] = price
        elif ch.attempts < MAX_ATTEMPTS:
            ch.next_attempt_at = now_utc() + _retry_delay(ch.attempts)
            ch.last_error = f"попытка {ch.attempts} из {MAX_ATTEMPTS}: {errors_text}"
        else:
            ch.status = PriceChangeStatus.error.value
            ch.last_error = f"не отправлено за {ch.attempts} попыток: {errors_text}"
    # Остальные размеры принятой карточки теперь тоже стоят по её цене — так и
    # запоминаем: иначе лимит шага у них считался бы от прежнего числа, а диапазон
    # безопасности считал бы цену карточки «не нашей».
    for key, price in accepted_cards.items():
        for sib in members.get(key, set()):
            if sib not in latest:
                _record_sent(db, sib, account.id, price)
                reference.record(db, account, sib, price, items.get(sib) or [], rule, rate)
    # Шаг принят — цены карточки у нас сразу на шаге: следующий шаг считается от него, а не
    # от цены до шага (иначе ушёл бы тот же шаг ещё раз). Не применит WB — следующий шаг он
    # просто не примет, а опрос WB вернёт настоящие цены.
    for key, step in stepped_cards.items():
        for sib in members.get(key, set()):
            for r in items.get(sib, []):
                if card_key(account.platform, r) == key and r.current_price:
                    disc = (Decimal(1) - Decimal(r.current_sale_price) / Decimal(r.current_price)
                            if r.current_sale_price else Decimal(0))
                    r.current_price = step
                    r.current_sale_price = int((Decimal(step) * (1 - disc)).quantize(Decimal(1), rounding="ROUND_HALF_UP"))
    db.commit()
    return {"sent": len(ok & set(by_barcode)), "tried": len(by_barcode), "floored": floored, "waiting": waiting,
            "stepped": len(stepped_cards)}


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
        try:
            stats[account.name] = run_account(db, account, client)
        except Exception as e:
            # Сбой одного кабинета не должен останавливать отправку остальных.
            db.rollback()
            stats[account.name] = {"error": f"{type(e).__name__}: {e}"}
    reference.snapshot_if_dirty(db, "отправка цен")
    return stats

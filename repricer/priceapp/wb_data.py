"""Сбор данных WB для оценки цен и акций (страница «Данные WB»).

Зачем: решать, входить ли в акцию и окупилась ли она, можно только по своей истории, а у WB она живёт
недолго — воронка по дням в API за неделю, заказы и продажи 90 дней, остатки — только «сейчас», истории
цен нет вовсе (отчёт «Акции WB алгоритм и метрики», раздел про окна данных). Поэтому копим у себя с
первого дня, по каждому активному кабинету WB:

  * заказы и продажи — `statistics-api` (категория токена «Статистика»), каждые 30 минут по
    `lastChangeDate`, первый проход — 89 дней назад. Тут же скидка WB и цена для покупателя по каждому
    заказу: больше их у каждого заказа нет нигде, а метод WB объявил к отключению;
  * воронка по дням — `seller-analytics-api` («Аналитика»): день целиком, до 1000 карточек за запрос.
    Вчера — каждый день; свежие дни перечитываются ~35 суток (выкупы и возвраты WB дописывает к дню
    заказа); назад — до 365 дней, пока не соберём год;
  * остатки на складах WB — снимок раз в сутки (там же, «Аналитика»);
  * детализация к отчётам реализации — `finance-api` («Финансы»): раз в сутки, первый проход — 120 дней
    назад, дальше последние 3 недели (отчёт за неделю WB выкладывает после её конца);
  * цены карточек — снимок раз в сутки из уже загруженных текущих цен, без запросов к WB.

Как ходит: шагами. Задание (`job_wb_data`, тяжёлый поток, каждые 30 с) делает по кабинету не больше
одного запроса на домен WB и только когда лимит домена позволяет (`GAP`): воронка за год идёт часами,
но ни один проход не держит поток дольше нескольких запросов. Где остановились — в `WbDataState`, так
что перезапуск программы продолжает, а не начинает заново. Отказ по одному набору не трогает остальные:
401/403 (у токена нет категории) — повтор через 6 часов или сразу после смены ключа, 429 — когда скажет
WB, прочее — через 15 минут. Ничего наружу не пишет: только читает.

Запросы — по официальной спеке WB (скачана 18.10.2026); вживую НЕ проверялись.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from priceapp.models import (Account, ApiCredential, PlatformItem, WbDataState, WbFinanceRow, WbFunnelDay,
                             WbOrder, WbPriceDay, WbSale, WbStockDay)
from priceapp.timeutils import now_utc

logger = logging.getLogger("repricer.wb_data")

ORDERS, SALES, FUNNEL, STOCKS, FINANCE, PRICES = "orders", "sales", "funnel", "stocks", "finance", "prices"
DATASETS = (ORDERS, SALES, FUNNEL, STOCKS, FINANCE, PRICES)
LABELS = {ORDERS: "Заказы", SALES: "Продажи и возвраты", FUNNEL: "Воронка по дням", STOCKS: "Остатки на складах WB",
          FINANCE: "Детализация реализации", PRICES: "Цены по дням"}
HOSTS = {"statistics": (ORDERS, SALES), "analytics": (STOCKS, FUNNEL), "finance": (FINANCE,)}
HOST_OF = {ds: h for h, dss in HOSTS.items() for ds in dss}
CATEGORY = {"statistics": "Статистика", "analytics": "Аналитика", "finance": "Финансы"}
# Пауза между запросами к домену по кабинету, с: лимиты спеки с запасом — «Статистика» и «Финансы» —
# запрос в минуту, «Аналитика» — 3 в минуту.
GAP = {"statistics": 65, "analytics": 22, "finance": 65}
ORDER_HISTORY_DAYS = 89            # WB хранит заказы и продажи 90 дней
POLL_EVERY = timedelta(minutes=30)  # «Статистика» обновляется раз в 30 минут
STAT_CAP = 75000                   # ответ «Статистики» — до ~80 000 строк: столько пришло — значит, есть ещё
FUNNEL_BACK_DAYS = 364             # воронка — максимум за последние 365 дней
FUNNEL_FINAL_DAYS = 35             # столько дней WB дописывает выкупы и возвраты к дню заказа
FUNNEL_FRESH_DAYS = 7
FINANCE_START_DAYS = 120
FINANCE_REREAD_DAYS = 21
FINANCE_LIMIT = 20000
FINANCE_EVERY = timedelta(hours=24)
AUTH_RETRY = timedelta(hours=6)
ERROR_RETRY = timedelta(minutes=15)
BATCH = 500
MSK = timezone(timedelta(hours=3))  # время в ответах WB — московское (без перехода на летнее с 2014)
KEEP_DAYS = 730                    # сколько хранить собранное (`retention.prune`)
NEVER = datetime(2000, 1, 1)

FINANCE_FIELDS = [
    "reportId", "dateFrom", "dateTo", "rrdId", "nmId", "vendorCode", "techSize", "sku", "docTypeName",
    "sellerOperName", "srid", "orderDt", "saleDt", "rrDate", "quantity", "retailPrice", "retailAmount",
    "retailPriceWithDisc", "salePercent", "commissionPercent", "kvwBase", "kvw", "isKgvpV2", "spp", "forPay",
    "ppvzSalesCommission", "acquiringFee", "acquiringPercent", "deliveryAmount", "returnAmount", "deliveryService",
    "penalty", "additionalPayment", "paidStorage", "deduction", "paidAcceptance", "rebillLogisticCost",
    "sellerPromoId", "sellerPromoDiscount",
]

# Когда домен кабинета можно спрашивать снова: (кабинет, домен) -> время UTC. В памяти: после перезапуска
# первый запрос может прийтись в ту же минуту — тогда WB ответит 429, и следующую попытку назначит он сам.
_next_call: dict[tuple[int, str], datetime] = {}


def reset() -> None:
    _next_call.clear()


# --- разбор значений ------------------------------------------------------------------------------------

def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _int(v) -> int | None:
    d = _dec(v)
    return int(d) if d is not None else None


def _s(v, n: int) -> str:
    return ("" if v is None else str(v))[:n]


def _nm(v) -> str:
    i = _int(v)
    return str(i) if i else ""


def _parse(raw) -> datetime | None:
    if not raw or str(raw).startswith("0001-01-01"):
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:                                       # дробная часть длиннее микросекунд
            head, _, tail = text.partition(".")
            frac = "".join(ch for ch in tail if ch.isdigit())[:6]
            rest = tail[len("".join(ch for ch in tail if ch.isdigit())):]
            dt = datetime.fromisoformat(f"{head}.{frac}{rest}" if frac else head + rest)
        except ValueError:
            return None
    return dt


def _utc(raw) -> datetime | None:
    """Время WB (без пояса — московское) -> наивное UTC, как хранятся отметки."""
    dt = _parse(raw)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _msk_day(raw) -> date | None:
    """Московский календарный день момента WB — им WB считает сутки в отчётах."""
    dt = _parse(raw)
    if dt is None:
        return None
    return (dt if dt.tzinfo is None else dt.astimezone(MSK)).date()


def msk_date(moment_utc: datetime) -> date:
    """Московский день момента (наивного UTC): сутки отчётов WB — московские, где бы ни стоял сервер."""
    return moment_utc.replace(tzinfo=timezone.utc).astimezone(MSK).date()


def _date(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


# --- состояние --------------------------------------------------------------------------------------------

def _state(db: Session, account_id: int, dataset: str, key: str = "") -> WbDataState:
    s = db.query(WbDataState).filter_by(account_id=account_id, dataset=dataset, key=key).first()
    if s is None:
        # `updated_at` — последнее обращение; у нового — «никогда», чтобы он шёл первым среди наборов домена
        s = WbDataState(account_id=account_id, dataset=dataset, key=key, cursor="", rows=0, error="",
                        updated_at=NEVER)
        db.add(s)
        db.flush()            # autoflush выключен — следующий запрос должен видеть строку
    return s


def _due(st: WbDataState, now: datetime, cred_at: datetime | None) -> bool:
    if st.next_at is None or st.next_at <= now:
        return True
    # Ключ сменили после отказа — не ждать шести часов: скорее всего, добавили категорию.
    return bool(st.error) and cred_at is not None and cred_at > st.updated_at


def _fail(db: Session, st: WbDataState, e: Exception, host: str, now: datetime) -> str:
    from priceapp.platforms import wb_error_detail
    resp = getattr(e, "response", None)
    code = resp.status_code if resp is not None else None
    if code in (401, 403):
        text, wait = f"нет доступа — у токена нет категории «{CATEGORY[host]}»", AUTH_RETRY
    elif code == 429:
        from priceapp.http_retry import _retry_after_seconds
        sec = _retry_after_seconds(resp) or GAP[host]
        text, wait = f"WB просит подождать (лимит запросов), повтор через {int(sec)} с", timedelta(seconds=sec)
    elif isinstance(e, requests.RequestException):
        text, wait = (wb_error_detail(e) if resp is not None else f"нет связи с WB: {e}"), ERROR_RETRY
    else:
        text, wait = f"{type(e).__name__}: {e}", ERROR_RETRY
    st.error, st.next_at, st.updated_at = text[:500], now + wait, now
    db.commit()
    return text


def _ok(st: WbDataState, now: datetime) -> None:
    st.error, st.done_at, st.updated_at = "", now, now


# --- заказы и продажи («Статистика») ---------------------------------------------------------------------

def _store_stat(db: Session, account_id: int, rows: list[dict], model, key_field: str, key_col: str,
                fields, now: datetime) -> int:
    """Записать строки «Статистики» по ключу (`srid` заказа, `saleID` продажи): есть — обновить, нет — добавить.
    Повтор ключа в одном ответе — побеждает последняя строка (там самое свежее `lastChangeDate`)."""
    last: dict[str, dict] = {}
    for r in rows:
        k = _s(r.get(key_field), 100)
        if k:
            last[k] = r
    keys = list(last)
    n = 0
    for start in range(0, len(keys), BATCH):
        chunk = keys[start:start + BATCH]
        have = {getattr(o, key_col): o for o in db.query(model).filter(model.account_id == account_id,
                                                                       getattr(model, key_col).in_(chunk))}
        for k in chunk:
            r = last[k]
            vals = fields(r)
            if vals["day"] is None:
                vals["day"] = msk_date(now)
            o = have.get(k)
            if o is None:
                o = model(account_id=account_id, **{key_col: k})
                db.add(o)
            for f, v in vals.items():
                setattr(o, f, v)
            o.fetched_at = now
            n += 1
        db.commit()
    return n


def _common(r: dict) -> dict:
    return dict(day=_msk_day(r.get("date")), changed_at=_utc(r.get("lastChangeDate")), nm_id=_nm(r.get("nmId")),
                barcode=_s(r.get("barcode"), 64), article=_s(r.get("supplierArticle"), 200),
                size=_s(r.get("techSize"), 64), total_price=_dec(r.get("totalPrice")),
                discount_pct=_int(r.get("discountPercent")), spp=_dec(r.get("spp")),
                price_with_disc=_dec(r.get("priceWithDisc")), finished_price=_dec(r.get("finishedPrice")))


def _order_fields(r: dict) -> dict:
    return dict(_common(r), ordered_at=_utc(r.get("date")), is_cancel=bool(r.get("isCancel")),
                cancel_at=_utc(r.get("cancelDate")), warehouse=_s(r.get("warehouseName"), 100),
                region=_s(r.get("regionName"), 200))


def _sale_fields(r: dict) -> dict:
    # у продажи день — день ПРОДАЖИ (выкупа или возврата), а не заказа
    return dict(_common(r), sold_at=_utc(r.get("date")), srid=_s(r.get("srid"), 100), for_pay=_dec(r.get("forPay")))


def _stat_step(db: Session, acc: Account, client, dataset: str, now: datetime) -> int:
    st = _state(db, acc.id, dataset)
    since = st.cursor or (msk_date(now) - timedelta(days=ORDER_HISTORY_DAYS)).isoformat()
    if dataset == ORDERS:
        rows = client.stat_orders(since)
        n = _store_stat(db, acc.id, rows, WbOrder, "srid", "srid", _order_fields, now)
    else:
        rows = client.stat_sales(since)
        n = _store_stat(db, acc.id, rows, WbSale, "saleID", "sale_id", _sale_fields, now)
    if rows:
        st.cursor = _s(rows[-1].get("lastChangeDate"), 64) or st.cursor
    st.rows = len(rows)
    st.next_at = now if len(rows) >= STAT_CAP else now + POLL_EVERY
    _ok(st, now)
    db.commit()
    return n


# --- воронка по дням («Аналитика») ------------------------------------------------------------------------

def nm_ids(db: Session, account_id: int) -> list[int]:
    """Карточки кабинета из его каталога (`external_id` = `nmID:chrtID`)."""
    out = set()
    for (ext,) in db.query(PlatformItem.external_id).filter(PlatformItem.account_id == account_id):
        nm = (ext or "").split(":")[0]
        if nm.isdigit() and int(nm) > 0:
            out.add(int(nm))
    return sorted(out)


def funnel_next_day(db: Session, account_id: int, now: datetime) -> tuple[date, WbDataState | None] | None:
    """Какой день воронки собирать: начатый; затем свежие (вчера и до 35 дней назад), ещё не собранные или
    пора перечитать — выкупы и возвраты WB дописывает к дню заказа; затем добор назад, до года. Ближние дни
    раньше дальних: они нужнее, и у WB они уходят раньше."""
    today = msk_date(now)
    days = {s.key: s for s in db.query(WbDataState).filter(WbDataState.account_id == account_id,
                                                           WbDataState.dataset == FUNNEL, WbDataState.key != "")}
    oldest = today - timedelta(days=FUNNEL_BACK_DAYS)
    for k, s in sorted(days.items(), reverse=True):
        if s.cursor and k >= oldest.isoformat():
            return date.fromisoformat(k), s
    for back in range(1, FUNNEL_FINAL_DAYS + 1):
        d = today - timedelta(days=back)
        if d < oldest:
            break
        s = days.get(d.isoformat())
        if s is None or s.done_at is None:
            return d, s
        start = datetime(d.year, d.month, d.day, tzinfo=MSK).astimezone(timezone.utc).replace(tzinfo=None)
        final = s.done_at >= start + timedelta(days=FUNNEL_FINAL_DAYS)
        gap = timedelta(hours=20) if back <= FUNNEL_FRESH_DAYS else timedelta(hours=70)
        if not final and now - s.done_at >= gap:
            return d, s
    for back in range(FUNNEL_FINAL_DAYS + 1, FUNNEL_BACK_DAYS + 1):
        d = today - timedelta(days=back)
        s = days.get(d.isoformat())
        if s is None or s.done_at is None:
            return d, s
    return None


_FUNNEL_COLS = (("open_count", "openCount"), ("cart_count", "cartCount"), ("order_count", "orderCount"),
                ("order_sum", "orderSum"), ("buyout_count", "buyoutCount"), ("buyout_sum", "buyoutSum"),
                ("cancel_count", "cancelCount"), ("cancel_sum", "cancelSum"), ("wishlist", "addToWishlist"))


def _store_funnel(db: Session, account_id: int, day: date, products: list[dict], now: datetime) -> int:
    """Строки с движением — добавить или обновить; без движения — только обнулить уже лежащую (новых нулевых
    строк не заводим: год по всем карточкам — миллионы пустых строк)."""
    got: dict[str, dict] = {}
    for p in products:
        nm = _nm(((p or {}).get("product") or {}).get("nmId"))
        if nm:
            got[nm] = ((p.get("statistic") or {}).get("selected") or {})
    have = {}
    keys = list(got)
    for start in range(0, len(keys), BATCH):
        for r in db.query(WbFunnelDay).filter(WbFunnelDay.account_id == account_id, WbFunnelDay.day == day,
                                              WbFunnelDay.nm_id.in_(keys[start:start + BATCH])):
            have[r.nm_id] = r
    n = 0
    for nm, sel in got.items():
        vals = {col: _int(sel.get(src)) or 0 for col, src in _FUNNEL_COLS}
        row = have.get(nm)
        if row is None:
            if not any(vals.values()):
                continue
            row = WbFunnelDay(account_id=account_id, day=day, nm_id=nm)
            db.add(row)
        for col, v in vals.items():
            setattr(row, col, v)
        row.avg_price = _int(sel.get("avgPrice"))
        row.fetched_at = now
        n += 1
    return n


def _funnel_step(db: Session, acc: Account, client, plan, now: datetime) -> int:
    day, st, nms = plan
    if st is None:
        st = _state(db, acc.id, FUNNEL, day.isoformat())
    from priceapp.platforms import WB_FUNNEL_CHUNK
    part = int(st.cursor or 0)
    chunk = nms[part * WB_FUNNEL_CHUNK:(part + 1) * WB_FUNNEL_CHUNK]
    n = _store_funnel(db, acc.id, day, client.funnel_day(day, chunk), now) if chunk else 0
    if (part + 1) * WB_FUNNEL_CHUNK < len(nms):
        st.cursor = str(part + 1)
        st.updated_at = now
    else:
        st.cursor = ""
        db.flush()                                 # autoflush выключен — счёт должен видеть эту часть
        st.rows = db.query(func.count(WbFunnelDay.id)).filter(WbFunnelDay.account_id == acc.id,
                                                              WbFunnelDay.day == day).scalar() or 0
        _ok(st, now)
    db.commit()
    return n


# --- остатки («Аналитика») --------------------------------------------------------------------------------

def _stocks_step(db: Session, acc: Account, client, st: WbDataState, now: datetime) -> int:
    from priceapp.platforms import WB_STOCKS_PAGE
    day = date.fromisoformat(st.key)
    offset = int(st.cursor or 0)
    items = client.stocks_wb(offset)
    if offset == 0:
        db.query(WbStockDay).filter(WbStockDay.account_id == acc.id, WbStockDay.day == day).delete()
    agg: dict[tuple[str, str], list[int]] = {}
    for it in items:
        nm = _nm(it.get("nmId"))
        if not nm:
            continue
        v = agg.setdefault((nm, _s(_int(it.get("chrtId")) or "", 32)), [0, 0, 0])
        v[0] += _int(it.get("quantity")) or 0
        v[1] += _int(it.get("inWayToClient")) or 0
        v[2] += _int(it.get("inWayFromClient")) or 0
    have = {(r.nm_id, r.chrt_id): r for r in db.query(WbStockDay).filter(WbStockDay.account_id == acc.id,
                                                                          WbStockDay.day == day)} if offset else {}
    for (nm, chrt), (q, to_c, from_c) in agg.items():
        r = have.get((nm, chrt))
        if r is None:
            db.add(WbStockDay(account_id=acc.id, day=day, nm_id=nm, chrt_id=chrt, quantity=q, to_client=to_c,
                              from_client=from_c, fetched_at=now))
        else:
            r.quantity, r.to_client, r.from_client = r.quantity + q, r.to_client + to_c, r.from_client + from_c
    if len(items) >= WB_STOCKS_PAGE:
        st.cursor, st.updated_at = str(offset + len(items)), now
    else:
        st.cursor = ""
        db.flush()
        st.rows = db.query(func.count(WbStockDay.id)).filter(WbStockDay.account_id == acc.id,
                                                             WbStockDay.day == day).scalar() or 0
        _ok(st, now)
    db.commit()
    return len(agg)


# --- детализация реализации («Финансы») ---------------------------------------------------------------------

def _finance_vals(r: dict) -> dict:
    return dict(
        report_id=_s(r.get("reportId"), 32), period_from=_date(r.get("dateFrom")), period_to=_date(r.get("dateTo")),
        day=_date(r.get("rrDate")), nm_id=_nm(r.get("nmId")), barcode=_s(r.get("sku"), 64),
        article=_s(r.get("vendorCode"), 200), size=_s(r.get("techSize"), 64), doc_type=_s(r.get("docTypeName"), 64),
        oper=_s(r.get("sellerOperName"), 100), srid=_s(r.get("srid"), 100), ordered_at=_utc(r.get("orderDt")),
        sold_at=_utc(r.get("saleDt")), quantity=_int(r.get("quantity")), retail_price=_dec(r.get("retailPrice")),
        retail_amount=_dec(r.get("retailAmount")), retail_price_with_disc=_dec(r.get("retailPriceWithDisc")),
        sale_pct=_int(r.get("salePercent")), commission_pct=_dec(r.get("commissionPercent")),
        kvw_base=_dec(r.get("kvwBase")), kvw=_dec(r.get("kvw")), kgvp_v2=_dec(r.get("isKgvpV2")),
        spp=_dec(r.get("spp")), for_pay=_dec(r.get("forPay")), sales_commission=_dec(r.get("ppvzSalesCommission")),
        acquiring_fee=_dec(r.get("acquiringFee")), acquiring_pct=_dec(r.get("acquiringPercent")),
        delivery_count=_int(r.get("deliveryAmount")), return_count=_int(r.get("returnAmount")),
        logistics=_dec(r.get("deliveryService")), penalty=_dec(r.get("penalty")),
        additional_payment=_dec(r.get("additionalPayment")), storage=_dec(r.get("paidStorage")),
        deduction=_dec(r.get("deduction")), acceptance=_dec(r.get("paidAcceptance")),
        rebill_logistics=_dec(r.get("rebillLogisticCost")),
        seller_promo_id=_s(_int(r.get("sellerPromoId")) or "", 32),
        seller_promo_discount=_dec(r.get("sellerPromoDiscount")))


def _finance_step(db: Session, acc: Account, client, now: datetime) -> int:
    st = _state(db, acc.id, FINANCE)
    today = msk_date(now)
    try:
        c = json.loads(st.cursor) if st.cursor else {}
    except ValueError:
        c = {}
    start = c.get("start") or (today - timedelta(days=FINANCE_START_DAYS)).isoformat()
    p = c.get("pass") or {"from": start, "to": (today - timedelta(days=1)).isoformat(), "rrd": 0}
    data = client.finance_detailed(p["from"], p["to"], int(p.get("rrd") or 0), FINANCE_LIMIT, FINANCE_FIELDS) \
        if p["from"] <= p["to"] else None
    n = 0
    if data:
        last: dict[str, dict] = {}
        for r in data:
            k = _s(_int(r.get("rrdId")) or "", 32)
            if k:
                last[k] = r
        keys = list(last)
        for s in range(0, len(keys), BATCH):
            chunk = keys[s:s + BATCH]
            have = {x for (x,) in db.query(WbFinanceRow.rrd_id).filter(WbFinanceRow.account_id == acc.id,
                                                                       WbFinanceRow.rrd_id.in_(chunk))}
            for k in chunk:
                if k not in have:                  # строка детализации не меняется — повтор пропускаем
                    db.add(WbFinanceRow(account_id=acc.id, rrd_id=k, fetched_at=now, **_finance_vals(last[k])))
                    n += 1
            db.commit()
        p["rrd"] = _int(data[-1].get("rrdId")) or p.get("rrd") or 0
        st.cursor = json.dumps({"start": start, "pass": p})
        st.rows = len(data)
        st.next_at = now                           # дальше той же частью — через паузу домена
    else:                                          # 204 — проход закончен
        st.cursor = json.dumps({"start": (today - timedelta(days=FINANCE_REREAD_DAYS)).isoformat(), "pass": None})
        st.rows = 0
        st.next_at = now + FINANCE_EVERY
    _ok(st, now)
    db.commit()
    return n


# --- цены по дням (без запросов к WB) -----------------------------------------------------------------------

def snapshot_prices(db: Session, acc: Account, now: datetime) -> int:
    """Цены карточек кабинета за сегодня — из загруженных текущих цен (`accounts.load_prices`). Перезаписывается
    при каждой новой загрузке в тот же день: за день остаётся последняя известная цена."""
    today = msk_date(now)
    if acc.prices_loaded_at is None or msk_date(acc.prices_loaded_at) != today:
        return 0
    st = _state(db, acc.id, PRICES, today.isoformat())
    if st.done_at is not None and st.done_at >= acc.prices_loaded_at:
        return 0
    best: dict[str, tuple[int, int | None]] = {}
    for ext, price, sale in db.query(PlatformItem.external_id, PlatformItem.current_price,
                                     PlatformItem.current_sale_price).filter(PlatformItem.account_id == acc.id,
                                                                             PlatformItem.current_price.isnot(None)):
        nm = (ext or "").split(":")[0]
        if nm.isdigit() and (nm not in best or price > best[nm][0]):
            best[nm] = (int(price), int(sale) if sale is not None else None)
    have = {r.nm_id: r for r in db.query(WbPriceDay).filter(WbPriceDay.account_id == acc.id, WbPriceDay.day == today)}
    for nm, (price, sale) in best.items():
        r = have.get(nm)
        if r is None:
            r = WbPriceDay(account_id=acc.id, day=today, nm_id=nm)
            db.add(r)
        r.price, r.sale_price, r.fetched_at = price, sale, now
    st.rows = len(best)
    _ok(st, now)
    db.commit()
    return len(best)


# --- проход -------------------------------------------------------------------------------------------------

def _accounts(db: Session) -> list[Account]:
    return (db.query(Account).filter(Account.platform == "wb", Account.is_active.is_(True))
            .filter(Account.id.in_(select(ApiCredential.account_id))).order_by(Account.id).all())


def _plan(db: Session, acc: Account, dataset: str, now: datetime, cred_at):
    """Что сделать по набору одним запросом, или None (сейчас нечего / ещё рано)."""
    st = _state(db, acc.id, dataset)
    if not _due(st, now, cred_at):
        return None
    if dataset in (ORDERS, SALES, FINANCE):
        return st
    if dataset == STOCKS:
        day = _state(db, acc.id, STOCKS, msk_date(now).isoformat())
        return day if (day.done_at is None or day.cursor) else None
    nms = nm_ids(db, acc.id)
    if not nms:
        return None
    nxt = funnel_next_day(db, acc.id, now)
    return (nxt[0], nxt[1], nms) if nxt else None


def run(db: Session, client_factory=None, now: datetime | None = None) -> dict:
    """Один проход: по каждому активному кабинету WB с ключом — снимок цен и не больше одного запроса на
    домен WB, если его лимит позволяет. Из наборов одного домена первым идёт тот, к которому дольше не
    обращались, — годовой добор воронки не задерживает суточный снимок остатков. Коммитит."""
    from priceapp import accounts
    now = now or now_utc()
    st = {"requests": 0, "rows": 0, "errors": []}
    for acc in _accounts(db):
        try:
            snapshot_prices(db, acc, now)
        except Exception as e:
            db.rollback()
            st["errors"].append(f"{acc.name}: цены по дням — {e}"[:300])
        cred_at = db.query(func.max(ApiCredential.updated_at)).filter(ApiCredential.account_id == acc.id).scalar()
        client = None
        for host, datasets in HOSTS.items():
            if _next_call.get((acc.id, host), datetime.min) > now:
                continue
            order = sorted(datasets, key=lambda ds: _state(db, acc.id, ds).updated_at)
            for ds in order:
                plan = _plan(db, acc, ds, now, cred_at)
                if plan is None:
                    continue
                try:
                    client = client or accounts.client_for(db, acc, client_factory)
                    _next_call[(acc.id, host)] = now + timedelta(seconds=GAP[host])
                    if ds in (ORDERS, SALES):
                        n = _stat_step(db, acc, client, ds, now)
                    elif ds == FINANCE:
                        n = _finance_step(db, acc, client, now)
                    elif ds == STOCKS:
                        n = _stocks_step(db, acc, client, plan, now)
                    else:
                        n = _funnel_step(db, acc, client, plan, now)
                    head = _state(db, acc.id, ds)
                    head.error, head.done_at, head.updated_at = "", now, now
                    db.commit()
                    st["requests"] += 1
                    st["rows"] += n
                except Exception as e:
                    db.rollback()
                    resp = getattr(e, "response", None)
                    if ds == FUNNEL and resp is not None and resp.status_code == 400:
                        # WB отказал в этом дне (вне окна, неверный период) — день пропускаем, иначе он
                        # стоял бы первым в очереди вечно и держал всю воронку.
                        day = _state(db, acc.id, FUNNEL, plan[0].isoformat())
                        day.cursor, day.done_at, day.updated_at = "", now, now
                        day.error = "WB отказал: день пропущен"
                        db.commit()
                    head = _state(db, acc.id, ds)
                    text = _fail(db, head, e, host, now)
                    logger.warning("данные WB, %s, %s: %s", acc.name, LABELS[ds], text)
                    st["errors"].append(f"{acc.name}: {LABELS[ds]} — {text}"[:300])
                break                              # один запрос на домен за проход
    return st


def problems(db: Session) -> list[str]:
    """Отказы, которые требуют человека, — для отметки задания и «Внимания». Лимит запросов (429) не в счёт:
    это пауза, следующую попытку назначил сам WB."""
    names = {a.id: a.name for a in _accounts(db)}
    by: dict[tuple[str, str], list[str]] = {}
    for s in db.query(WbDataState).filter(WbDataState.key == "", WbDataState.error != ""):
        if s.account_id in names and "лимит запросов" not in s.error:
            by.setdefault((names[s.account_id], s.error), []).append(LABELS.get(s.dataset, s.dataset).lower())
    return [f"{acc}: {', '.join(sorted(dss))} — {err}" for (acc, err), dss in sorted(by.items())]


def status(db: Session, now: datetime | None = None) -> list[dict]:
    """По кабинету и набору: что собрано (сколько, за какие дни), когда, ошибка, следующая попытка."""
    now = now or now_utc()
    out = []
    for acc in _accounts(db):
        sets = []
        for ds in DATASETS:
            st = db.query(WbDataState).filter_by(account_id=acc.id, dataset=ds, key="").first()
            row = {"dataset": ds, "label": LABELS[ds], "category": CATEGORY.get(HOST_OF.get(ds, ""), ""),
                   "error": st.error if st else "", "done_at": st.done_at if st else None,
                   "next_at": st.next_at if st and st.next_at and st.next_at > now else None}
            model = {ORDERS: WbOrder, SALES: WbSale, FUNNEL: WbFunnelDay, STOCKS: WbStockDay,
                     FINANCE: WbFinanceRow, PRICES: WbPriceDay}[ds]
            n, lo, hi = db.query(func.count(model.id), func.min(model.day), func.max(model.day)).filter(
                model.account_id == acc.id).one()
            row.update(rows=n or 0, first=lo, last=hi)
            if ds in (FUNNEL, STOCKS, PRICES):
                days = db.query(WbDataState).filter(WbDataState.account_id == acc.id, WbDataState.dataset == ds,
                                                    WbDataState.key != "", WbDataState.done_at.isnot(None),
                                                    WbDataState.cursor == "")
                row["days"] = days.count()
                if ds == PRICES:
                    row["done_at"] = max((d.done_at for d in days), default=row["done_at"])
            if ds == FUNNEL:
                row["target_days"] = FUNNEL_BACK_DAYS
            sets.append(row)
        out.append({"account": acc, "sets": sets})
    return out


def retry_now(db: Session, account_id: int = 0) -> int:
    """Снять паузу после отказа: «Повторить сейчас» (например, сразу после того, как токену добавили
    категорию). Коммитит."""
    q = db.query(WbDataState).filter(WbDataState.key == "", WbDataState.error != "")
    if account_id:
        q = q.filter(WbDataState.account_id == account_id)
    n = 0
    for s in q:
        s.next_at = None
        n += 1
    for k in list(_next_call):
        if not account_id or k[0] == account_id:
            del _next_call[k]
    db.commit()
    return n

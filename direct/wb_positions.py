# -*- coding: utf-8 -*-
"""
Позиции в поиске и воронка продаж Wildberries — ежедневное отслеживание с историей (нужна подписка «Джем»).

Что собирает (по каждому кабинету) и хранит в базе wb_history.db — история копится с каждым запуском:
  1. Воронка по товарам по дням: переходы в карточку, корзины, заказы, выкупы, отложенные, средняя цена,
     срок доставки, рейтинг; остатки на WB и на своём складе.
  2. Позиция товара в поиске по дням: медианная и средняя, видимость, переходы из поиска.
     Плюс медианная позиция всего кабинета.
  3. Позиции по каждому поисковому запросу товара по дням: медианная и средняя позиция, частотность,
     переходы, корзины, заказы, конверсия против конкурентов по тому же запросу
     (до 100 запросов на товар — «Джем Продвинутый/Премиальный», до 30 — «Стандарт»; скрипт определит сам).
  4. Ваш список «следить» (wb_watch.csv): позиции по дням по конкретным запросам и цель по позиции.
  5. Реклама: рекламная позиция товара (буст), расход по дням; по рекламным запросам — расход и позиция в рекламе.

Что показывает:
  • воронку отслеживаемых товаров за 7 дней против прошлых 7 дней;
  • позиции товаров: медиана сейчас (последние 3 дня) против 7 дней до этого, средняя, видимость, воронка;
  • ваши запросы из wb_watch.csv по дням;
  • сигналы и действия: позиция упала — и почему (остатки, доставка, цена, рейтинг, реклама); запрос выпал из топа;
    «провалы» (средняя намного хуже медианы); меньше переходов при тех же позициях; просела конверсия;
    конверсия хуже конкурентов; новые продающие запросы; запросы в топе, где стоит проверить, нужна ли реклама;
  • wb_positions.html — графики позиций и воронки по товарам (открыть двойным щелчком в браузере).

Запуск:  python wb_positions.py                       (все кабинеты; обычно 5–15 минут на кабинет)
         python wb_positions.py --cabinet "Яворская"
         python wb_positions.py --backfill 30           (догрузить историю за 30 дней; первый запуск сам берёт 14)
         python wb_positions.py --report                (только отчёт по уже собранной истории, без запросов к WB)
Нужны категории токена: «Аналитика» (с подпиской «Джем»), «Продвижение».
Свой список запросов: wb_watch.csv (создаётся при первом запуске; открыть в Excel и дописать строки).
Результат: wb_positions_report.txt, wb_positions.html, wb_positions_products.csv, wb_positions_queries.csv,
           wb_positions_actions.csv (его читает wb_daily.py); история — wb_history.db.
"""
import datetime as dt
import html
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict

from wb_common import (ADV, ANA, HERE, JAM_KEY, JAM_LIMIT, TODAY, YESTERDAY, ApiError, arg_cabinet, call, campaigns,
                       chunks, cut, fm, funnel, load_tokens, log, num, pace, read_csv, save_log, search_texts,
                       to_float, write_csv)

# ======================== НАСТРОЙКИ ========================
TOP_NMS = 40              # отслеживать: лучшие по заказам за 30 дней + рекламируемые + из wb_watch.csv
MAX_NMS = 60              # но не больше стольких товаров на кабинет (каждые 50 товаров — +2 запроса к WB в день)
MEDIAN_NMS = 20           # по скольким лучшим товарам брать медианную позицию товара (≈20 секунд на товар)
QUERIES_PER_NM = 100      # запросов на товар: 100 — «Джем Продвинутый/Премиальный»; на «Стандарте» скрипт возьмёт 30
FIRST_BACKFILL = 14       # история при первом запуске; в этом же окне скрипт сам дозаполняет пропущенные дни
NOW_DAYS, BEFORE_DAYS = 3, 7           # «сейчас» — медиана за 3 последних дня; «было» — за 7 дней до них
POS_DROP_ABS, POS_DROP_REL = 5, 0.30   # «позиция упала»: хуже минимум на 5 мест и на 30%
TOP_POS = 10              # «топ» по запросу
FUNNEL_DROP = 0.30        # переходов меньше на 30% неделя к неделе — сигнал
CONV_DROP = 0.25          # конверсия (в корзину, в заказ) хуже на 25% неделя к неделе — сигнал
PERCENTILE_BAD = -30      # конверсия в корзину хуже конкурентов на 30% и больше — сигнал
ORGANIC_SPEND = 500       # реклама по запросу за 10 дней (₽), при которой проверять «нужна ли реклама в топе»
HF, MF = 10000, 1000      # ВЧ / СЧ / НЧ — поисков в неделю (как в wb_queries.py)
CHART_DAYS = 30           # сколько дней на графиках wb_positions.html
HTML_PRODUCTS = 20        # сколько товаров показывать на странице с графиками
KEEP_DAYS = 400           # сколько дней хранить историю
# ===========================================================

DB = HERE / "wb_history.db"
WATCH_FILE = "wb_watch.csv"
Y = YESTERDAY
NOW = [Y - dt.timedelta(days=i) for i in range(NOW_DAYS)]
BEFORE = [Y - dt.timedelta(days=NOW_DAYS + i) for i in range(BEFORE_DAYS)]
TEN = NOW + BEFORE
W7 = [Y - dt.timedelta(days=i) for i in range(7)]
P7 = [Y - dt.timedelta(days=7 + i) for i in range(7)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (cab TEXT, nm INTEGER, vendor TEXT, title TEXT, subject TEXT, PRIMARY KEY (cab, nm));
CREATE TABLE IF NOT EXISTS funnel_day (cab TEXT, nm INTEGER, date TEXT, opens REAL, carts REAL, orders REAL,
  order_sum REAL, buyouts REAL, buyout_sum REAL, cancels REAL, wishlist REAL, avg_price REAL, ready_hours REAL,
  rating REAL, feedback_rating REAL, PRIMARY KEY (cab, nm, date));
CREATE TABLE IF NOT EXISTS stock_day (cab TEXT, nm INTEGER, date TEXT, stock_wb REAL, stock_mp REAL,
  PRIMARY KEY (cab, nm, date));
CREATE TABLE IF NOT EXISTS product_pos_day (cab TEXT, nm INTEGER, date TEXT, median_pos REAL, avg_pos REAL,
  visibility REAL, search_opens REAL, search_carts REAL, search_orders REAL, is_ad INTEGER,
  PRIMARY KEY (cab, nm, date));
CREATE TABLE IF NOT EXISTS query_day (cab TEXT, nm INTEGER, query TEXT, date TEXT, freq REAL, week_freq REAL,
  median_pos REAL, avg_pos REAL, visibility REAL, opens REAL, carts REAL, orders REAL, p_opens REAL, p_cart REAL,
  p_order REAL, p_orders REAL, price REAL, PRIMARY KEY (cab, nm, query, date));
CREATE TABLE IF NOT EXISTS ad_day (cab TEXT, nm INTEGER, date TEXT, ad_pos REAL, views REAL, clicks REAL,
  spend REAL, orders REAL, PRIMARY KEY (cab, nm, date));
CREATE TABLE IF NOT EXISTS ad_query_day (cab TEXT, nm INTEGER, query TEXT, date TEXT, views REAL, clicks REAL,
  carts REAL, orders REAL, spend REAL, ad_pos REAL, PRIMARY KEY (cab, nm, query, date));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
DATE_TABLES = ("funnel_day", "stock_day", "product_pos_day", "query_day", "ad_day", "ad_query_day")


# ---------------- база ----------------

def iso(d):
    return d.isoformat()


def drange(d1, d2):
    return [d1 + dt.timedelta(days=i) for i in range((d2 - d1).days + 1)]


def connect():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def upsert(con, table, keys, row):
    """Вставить строку; если есть — обновить, не затирая известные значения пустыми."""
    cols = list(row)
    upd = ", ".join(f"{c}=COALESCE(excluded.{c}, {table}.{c})" for c in cols if c not in keys)
    con.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                f"ON CONFLICT ({', '.join(keys)}) DO " + (f"UPDATE SET {upd}" if upd else "NOTHING"),
                [row[c] for c in cols])


def meta_get(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def meta_set(con, key, value):
    con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))


def fetched(con, cab, src):
    return set(meta_get(con, f"got:{cab}:{src}", []))


def mark(con, cab, src, days):
    cutoff = iso(TODAY - dt.timedelta(days=KEEP_DAYS))
    s = fetched(con, cab, src) | {iso(d) for d in days}
    meta_set(con, f"got:{cab}:{src}", sorted(x for x in s if x >= cutoff))
    con.commit()


def cleanup(con):
    cutoff = iso(TODAY - dt.timedelta(days=KEEP_DAYS))
    for t in DATE_TABLES:
        con.execute(f"DELETE FROM {t} WHERE date < ?", (cutoff,))
    con.commit()


# ---------------- мелочи ----------------

def pos(v):
    v = num(v)
    return v if v > 0 else None


def cur(x, k="current"):
    return x.get(k) if isinstance(x, dict) else None


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def klass(freq):
    if not freq:
        return ""
    return "ВЧ" if freq >= HF else ("СЧ" if freq >= MF else "НЧ")


def fp(v):
    """Позиция для вывода."""
    return "—" if v is None else f"{v:.0f}"


def move(now, before):
    """↑ — товар поднялся (позиция стала меньше, лучше), ↓ — опустился."""
    if now is None or before is None:
        return ""
    d = before - now
    if abs(d) < 0.5:
        return "="
    return f"↑{abs(d):.0f}" if d > 0 else f"↓{abs(d):.0f}"


def chg(a, b):
    return f"{(a / b - 1) * 100:+.0f}%" if b else ""


def worse(now, before):
    return (now is not None and before is not None and now - before >= POS_DROP_ABS
            and now >= before * (1 + POS_DROP_REL))


def better(now, before):
    return (now is not None and before is not None and before - now >= POS_DROP_ABS
            and now <= before * (1 - POS_DROP_REL))


def arg_value(name):
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def load_watch(cab):
    """wb_watch.csv: кабинет;артикул WB;запрос;цель позиции;заметка → {артикул: {запрос: цель}}."""
    p = HERE / WATCH_FILE
    if not p.exists():
        p.write_text("кабинет;артикул WB;запрос;цель позиции;заметка\n"
                     "#пример;123456789;куртка мужская зимняя;10;строки, которые начинаются с #, скрипт пропускает\n",
                     encoding="utf-8-sig")
    res = {}
    for r in read_csv(WATCH_FILE):
        c = (r.get("кабинет") or "").strip()
        if not c or c.startswith("#") or c != cab:
            continue
        try:
            nm = int(str(r.get("артикул WB") or "").strip())
        except ValueError:
            continue
        q = (r.get("запрос") or "").strip().lower()
        res.setdefault(nm, {})
        if q:
            res[nm][q] = to_float(r.get("цель позиции"))
    return res


# ---------------- сбор данных ----------------

def per(d1, d2):
    return {"start": iso(d1), "end": iso(d2)}


def jam(token, path, body, what):
    pace(token, JAM_KEY, 21)
    return call("POST", ANA + path, token, body=body, what=what)


def save_funnel(con, cab, prods, day, keep, current):
    for p in prods:
        pr = p.get("product") or {}
        st = (p.get("statistic") or {}).get("selected") or {}
        nm = pr.get("nmId")
        if not nm:
            continue
        upsert(con, "products", ("cab", "nm"), {"cab": cab, "nm": nm, "vendor": pr.get("vendorCode"),
                                                "title": pr.get("title"), "subject": pr.get("subjectName")})
        if nm not in keep and not (num(st.get("openCount")) or num(st.get("orderCount"))):
            continue
        ttr = st.get("timeToReady") or {}
        hours = num(ttr.get("days")) * 24 + num(ttr.get("hours")) + num(ttr.get("mins")) / 60
        upsert(con, "funnel_day", ("cab", "nm", "date"), {
            "cab": cab, "nm": nm, "date": iso(day), "opens": num(st.get("openCount")),
            "carts": num(st.get("cartCount")), "orders": num(st.get("orderCount")), "order_sum": num(st.get("orderSum")),
            "buyouts": num(st.get("buyoutCount")), "buyout_sum": num(st.get("buyoutSum")),
            "cancels": num(st.get("cancelCount")), "wishlist": num(st.get("addToWishlist")),
            "avg_price": num(st.get("avgPrice")) or None, "ready_hours": hours or None,
            # рейтинг WB отдаёт только текущий — в историю пишем его только за вчера
            "rating": (num(pr.get("productRating")) or None) if current else None,
            "feedback_rating": (num(pr.get("feedbackRating")) or None) if current else None})


def funnel_day(con, cab, token, day, keep):
    """Воронка всех товаров за один день (1 запрос на 1000 товаров)."""
    offset = 0
    while True:
        pace(token, "funnel", 21)
        body = {"selectedPeriod": per(day, day), "nmIds": [], "brandNames": [], "subjectIds": [], "tagIds": [],
                "skipDeletedNm": False, "orderBy": {"field": "orderSum", "mode": "desc"}, "limit": 1000,
                "offset": offset}
        data = call("POST", ANA + "/api/analytics/v3/sales-funnel/products", token, body=body,
                    what="Воронка за день") or {}
        prods = (data.get("data") or {}).get("products") or []
        save_funnel(con, cab, prods, day, keep, current=(day == Y))
        if len(prods) < 1000:
            break
        offset += 1000


def funnel_history(con, cab, token, nms, d1, d2):
    """Воронка по дням за последнюю неделю (WB отдаёт до 20 товаров за запрос)."""
    for part in chunks(nms, 20):
        pace(token, "funnel_hist", 21)
        data = call("POST", ANA + "/api/analytics/v3/sales-funnel/products/history", token, what="Воронка по дням",
                    body={"selectedPeriod": per(d1, d2), "nmIds": part, "skipDeletedNm": False,
                          "aggregationLevel": "day"})
        if isinstance(data, dict):
            data = data.get("data") or []
        for p in data or []:
            nm = (p.get("product") or {}).get("nmId")
            for h in p.get("history") or []:
                d = str(h.get("date") or "")[:10]
                if nm and d:
                    upsert(con, "funnel_day", ("cab", "nm", "date"), {
                        "cab": cab, "nm": nm, "date": d, "opens": num(h.get("openCount")),
                        "carts": num(h.get("cartCount")), "orders": num(h.get("orderCount")),
                        "order_sum": num(h.get("orderSum")), "buyouts": num(h.get("buyoutCount")),
                        "buyout_sum": num(h.get("buyoutSum")), "wishlist": num(h.get("addToWishlistCount"))})


def texts_day(con, cab, token, nms, day):
    """Позиции и воронка по каждому поисковому запросу товаров за один день."""
    items = search_texts(token, nms, day, day, QUERIES_PER_NM)
    for it in items:
        q = (it.get("text") or "").strip().lower()
        nm = it.get("nmId")
        if not q or not nm:
            continue
        upsert(con, "query_day", ("cab", "nm", "query", "date"), {
            "cab": cab, "nm": nm, "query": q, "date": iso(day),
            "freq": num(cur(it.get("frequency"))), "week_freq": num(it.get("weekFrequency")) or None,
            "median_pos": pos(cur(it.get("medianPosition"))), "avg_pos": pos(cur(it.get("avgPosition"))),
            "visibility": cur(it.get("visibility")), "opens": num(cur(it.get("openCard"))),
            "carts": num(cur(it.get("addToCart"))), "orders": num(cur(it.get("orders"))),
            "p_opens": cur(it.get("openCard"), "percentile"), "p_cart": cur(it.get("openToCart"), "percentile"),
            "p_order": cur(it.get("cartToOrder"), "percentile"), "p_orders": cur(it.get("orders"), "percentile"),
            "price": num((it.get("price") or {}).get("minPrice")) or None})


def details_day(con, cab, token, nms, day):
    """Средняя позиция, видимость, переходы из поиска по товарам за один день."""
    for part in chunks(nms, 50):
        body = {"currentPeriod": per(day, day), "nmIds": part, "orderBy": {"field": "openCard", "mode": "desc"},
                "positionCluster": "all", "includeSubstitutedSKUs": True, "includeSearchTexts": True,
                "limit": 50, "offset": 0}
        data = jam(token, "/api/v2/search-report/table/details", body, "Позиции товаров") or {}
        for p in (data.get("data") or {}).get("products") or []:
            if p.get("nmId"):
                upsert(con, "product_pos_day", ("cab", "nm", "date"), {
                    "cab": cab, "nm": p["nmId"], "date": iso(day), "avg_pos": pos(cur(p.get("avgPosition"))),
                    "visibility": cur(p.get("visibility")), "search_opens": num(cur(p.get("openCard"))),
                    "search_carts": num(cur(p.get("addToCart"))), "search_orders": num(cur(p.get("orders"))),
                    "is_ad": 1 if p.get("isAdvertised") else 0})


def report_nm(con, cab, token, nm, d1, d2):
    """Медианная и средняя позиция по дням для одного товара (nm=0 — весь кабинет)."""
    body = {"currentPeriod": per(d1, d2), "positionCluster": "all", "orderBy": {"field": "avgPosition", "mode": "asc"},
            "includeSubstitutedSKUs": True, "includeSearchTexts": True, "limit": 1, "offset": 0}
    if nm:
        body["nmIds"] = [nm]
    data = ((jam(token, "/api/v2/search-report/report", body, "Медианная позиция") or {}).get("data")) or {}
    for c in (data.get("positionInfo") or {}).get("chartItems") or []:
        d = str(c.get("dt") or "")[:10]
        if d:
            upsert(con, "product_pos_day", ("cab", "nm", "date"), {
                "cab": cab, "nm": nm, "date": d, "median_pos": pos(c.get("median")), "avg_pos": pos(c.get("average"))})
    for v in (data.get("visibilityInfo") or {}).get("byDay") or []:
        d = str(v.get("dt") or "")[:10]
        if d:
            upsert(con, "product_pos_day", ("cab", "nm", "date"), {
                "cab": cab, "nm": nm, "date": d, "visibility": v.get("visibility"), "search_opens": num(v.get("open"))})


def watch_orders(con, cab, token, nm, queries, d1, d2):
    """Ваши запросы из wb_watch.csv: средняя позиция и заказы по дням (даже если запроса нет в топе товара)."""
    lim = JAM_LIMIT.get(token[-16:], 30)
    for part in chunks(sorted(queries), lim):
        data = jam(token, "/api/v2/search-report/product/orders", {"period": per(d1, d2), "nmId": nm,
                                                                    "searchTexts": part}, "Мои запросы") or {}
        for it in (data.get("data") or {}).get("items") or []:
            q = (it.get("text") or "").strip().lower()
            for di in it.get("dateItems") or []:
                d = str(di.get("dt") or "")[:10]
                if q and d:
                    upsert(con, "query_day", ("cab", "nm", "query", "date"), {
                        "cab": cab, "nm": nm, "query": q, "date": d, "avg_pos": pos(di.get("avgPosition")),
                        "orders": num(di.get("orders"))})
            if q and it.get("frequency"):      # частота за 7 дней — только если её не дал основной отчёт
                con.execute("INSERT INTO query_day (cab, nm, query, date, week_freq) VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT (cab, nm, query, date) DO UPDATE SET "
                            "week_freq=COALESCE(query_day.week_freq, excluded.week_freq)",
                            (cab, nm, q, iso(d2), num(it.get("frequency"))))


def ads_collect(con, cab, token, ids, d1, d2):
    """Реклама по товарам по дням: расход, показы, клики, заказы и рекламная позиция (буст)."""
    agg = defaultdict(lambda: defaultdict(float))
    poss = defaultdict(list)
    a = d1
    while a <= d2:
        b = min(d2, a + dt.timedelta(days=30))
        for part in chunks(ids, 50):
            pace(token, "fullstats", 21)
            data = call("GET", ADV + "/adv/v3/fullstats", token, what="Статистика рекламы",
                        params={"ids": ",".join(map(str, part)), "beginDate": iso(a), "endDate": iso(b)}) or []
            for item in data:
                for day in item.get("days") or []:
                    d = str(day.get("date") or "")[:10]
                    for app in day.get("apps") or []:
                        for n in app.get("nms") or []:
                            g = agg[(n.get("nmId"), d)]
                            for k, f in (("views", "views"), ("clicks", "clicks"), ("spend", "sum"), ("orders", "orders")):
                                g[k] += num(n.get(f))
                for bs in item.get("boosterStats") or []:
                    p = pos(bs.get("avg_position"))
                    if p:
                        poss[(bs.get("nm"), str(bs.get("date") or "")[:10])].append(p)
        a = b + dt.timedelta(days=1)
    for key in set(agg) | set(poss):
        nm, d = key
        if not nm or not d:
            continue
        g = agg.get(key, {})
        upsert(con, "ad_day", ("cab", "nm", "date"), {
            "cab": cab, "nm": nm, "date": d, "ad_pos": mean(poss.get(key, [])), "views": g.get("views", 0.0),
            "clicks": g.get("clicks", 0.0), "spend": g.get("spend", 0.0), "orders": g.get("orders", 0.0)})


def ad_queries_day(con, cab, token, pairs, day):
    """Реклама по поисковым кластерам за день: расход, клики, заказы, средняя позиция в рекламе."""
    agg = defaultdict(lambda: defaultdict(float))
    for part in chunks(pairs, 100):
        pace(token, "nqstats", 7)
        data = call("POST", ADV + "/adv/v0/normquery/stats", token, what="Реклама по запросам",
                    body={"from": iso(day), "to": iso(day),
                          "items": [{"advert_id": a, "nm_id": n} for a, n in part]}) or {}
        for it in data.get("stats") or []:
            nm = it.get("nm_id")
            for s in it.get("stats") or []:
                q = (s.get("norm_query") or "").strip().lower()
                if not q or not nm:
                    continue
                g = agg[(nm, q)]
                for k, f in (("views", "views"), ("clicks", "clicks"), ("carts", "atbs"), ("orders", "orders"),
                             ("spend", "spend")):
                    g[k] += num(s.get(f))
                p = pos(s.get("avg_pos"))
                if p:
                    w = num(s.get("views")) or num(s.get("clicks")) or 1
                    g["pos_w"] += p * w
                    g["w"] += w
    for (nm, q), g in agg.items():
        upsert(con, "ad_query_day", ("cab", "nm", "query", "date"), {
            "cab": cab, "nm": nm, "query": q, "date": iso(day), "views": g["views"], "clicks": g["clicks"],
            "carts": g["carts"], "orders": g["orders"], "spend": g["spend"],
            "ad_pos": g["pos_w"] / g["w"] if g["w"] else None})


def collect(con, cab, token, window):
    d_from = Y - dt.timedelta(days=window - 1)
    days = drange(d_from, Y)
    print("  товары, остатки, кампании...")
    f30 = funnel(token, Y - dt.timedelta(days=29), Y)
    for nm, v in f30.items():
        upsert(con, "products", ("cab", "nm"), {"cab": cab, "nm": nm, "vendor": v.get("vendor"),
                                                "title": v.get("title"), "subject": v.get("subject")})
        if v.get("stock_wb") is not None or v.get("stock_mp") is not None:
            upsert(con, "stock_day", ("cab", "nm", "date"), {"cab": cab, "nm": nm, "date": iso(TODAY),
                                                             "stock_wb": v.get("stock_wb"), "stock_mp": v.get("stock_mp")})
    try:
        camps = campaigns(token, statuses=(7, 9, 11), changed_since=iso(d_from))
    except ApiError as e:
        log(f"  ! {e} — без рекламы.")
        camps = {}
    active = {cid: c for cid, c in camps.items() if c.get("status") in (9, 11, None)}
    ad_nms = {s.get("nm_id") for c in active.values() for s in c.get("nm_settings") or [] if s.get("nm_id")}
    watch = load_watch(cab)
    top = [nm for nm, v in sorted(f30.items(), key=lambda x: -x[1]["orderCount"]) if v["orderCount"] > 0]
    tracked = list(dict.fromkeys(list(watch) + top[:TOP_NMS] + sorted(ad_nms)))[:MAX_NMS]
    med_nms = [n for n in dict.fromkeys(list(watch) + top[:MEDIAN_NMS]) if n in tracked]
    meta_set(con, f"tracked:{cab}", tracked)
    meta_set(con, f"orders30:{cab}", {str(nm): f30[nm]["orderCount"] for nm in tracked if nm in f30})
    con.commit()
    if not tracked:
        log("  Нет товаров с заказами за 30 дней, рекламой или в wb_watch.csv.")
        return

    need_f = [d for d in days if iso(d) not in fetched(con, cab, "funnel") and d < Y - dt.timedelta(days=6)]
    need_t = sorted({d for d in days if iso(d) not in fetched(con, cab, "texts")} | {Y})
    need_d = sorted({d for d in days if iso(d) not in fetched(con, cab, "details")} | {Y})
    parts = math.ceil(len(tracked) / 50)
    jam_calls = (len(need_t) + len(need_d)) * parts + len(med_nms) + 1 + sum(
        math.ceil(len(q) / 30) for q in watch.values() if q)
    log(f"  Отслеживаю {len(tracked)} товаров (медиана товара — по {len(med_nms)}). Запросов к «Джему» ≈{jam_calls}, "
        f"WB разрешает 3 в минуту → ≈{jam_calls / 3 + 1:.0f} мин.")

    print("  воронка по дням...")
    keep = set(tracked)
    for d in need_f:
        try:
            funnel_day(con, cab, token, d, keep)
            mark(con, cab, "funnel", [d])
        except ApiError as e:
            log(f"  ! воронка за {d:%d.%m}: {e}")
            break
    try:
        funnel_history(con, cab, token, tracked, Y - dt.timedelta(days=6), Y)
        funnel_day(con, cab, token, Y, keep)
        mark(con, cab, "funnel", drange(Y - dt.timedelta(days=6), Y))
    except ApiError as e:
        log(f"  ! воронка по дням: {e}")

    state = {"ok": True}

    def jam_step(label, fn, *args):
        if not state["ok"]:
            return False
        try:
            fn(*args)
            con.commit()
            return True
        except ApiError as e:
            if e.status in (401, 402, 403):
                state["ok"] = False
                log(f"  ✗ {label}: {e}")
                log("    Позиции не собраны: отчёт «Поисковые запросы» доступен с подпиской «Джем» и токеном "
                    "с категорией «Аналитика».")
            else:
                log(f"  ! {label}: {e}")
            return False

    print("  позиции по поисковым запросам...")
    for d in need_t:
        if jam_step(f"запросы за {d:%d.%m}", texts_day, con, cab, token, tracked, d):
            mark(con, cab, "texts", [d])
    lim = JAM_LIMIT.get(token[-16:])
    if lim:
        meta_set(con, f"jam_limit:{cab}", lim)
    print("  позиции товаров...")
    for d in need_d:
        if jam_step(f"позиции товаров за {d:%d.%m}", details_day, con, cab, token, tracked, d):
            mark(con, cab, "details", [d])
    print("  медианная позиция товаров...")
    for nm in [0] + med_nms:
        got = fetched(con, cab, f"report:{nm}")
        need = sorted({d for d in days if iso(d) not in got} | {Y})
        d1 = min(need[0], Y - dt.timedelta(days=NOW_DAYS - 1))
        if jam_step("медианная позиция " + (str(nm) if nm else "кабинета"), report_nm, con, cab, token, nm, d1, Y):
            mark(con, cab, f"report:{nm}", drange(d1, Y))
    if any(watch.values()):
        print("  мои запросы...")
        for nm, qs in watch.items():
            if qs:
                jam_step(f"мои запросы {nm}", watch_orders, con, cab, token, nm, list(qs), Y - dt.timedelta(days=6), Y)

    if camps:
        print("  реклама...")
        need_a = sorted({d for d in days if iso(d) not in fetched(con, cab, "ads")} | {Y})
        try:
            ads_collect(con, cab, token, list(camps), need_a[0], Y)
            mark(con, cab, "ads", drange(need_a[0], Y))
        except ApiError as e:
            log(f"  ! реклама: {e}")
        pairs = [(cid, s.get("nm_id")) for cid, c in camps.items() for s in c.get("nm_settings") or []
                 if s.get("nm_id") in keep]
        if pairs:
            for d in sorted({d for d in days if iso(d) not in fetched(con, cab, "adq")} | {Y}):
                try:
                    ad_queries_day(con, cab, token, pairs, d)
                    mark(con, cab, "adq", [d])
                except ApiError as e:
                    log(f"  ! реклама по запросам за {d:%d.%m}: {e}")
                    break
    con.commit()


# ---------------- анализ ----------------

def load(con, cab, tracked):
    since = iso(Y - dt.timedelta(days=max(CHART_DAYS, 21) + 5))
    since_q = iso(Y - dt.timedelta(days=20))
    nmset = set(tracked) | {0}
    D = {"names": {}, "F": defaultdict(dict), "P": defaultdict(dict), "A": defaultdict(dict),
         "Q": defaultdict(lambda: defaultdict(dict)), "AQ": defaultdict(dict), "stock": {}, "first": {}}
    for nm, vendor, title, subject in con.execute("SELECT nm, vendor, title, subject FROM products WHERE cab=?", (cab,)):
        D["names"][nm] = {"vendor": vendor or "", "title": title or "", "subject": subject or ""}

    def rows(sql, params):
        cur_ = con.execute(sql, params)
        cols = [c[0] for c in cur_.description]
        for r in cur_:
            yield dict(zip(cols, r))

    for r in rows("SELECT * FROM funnel_day WHERE cab=? AND date>=?", (cab, since)):
        if r["nm"] in nmset:
            D["F"][r["nm"]][r["date"]] = r
    for r in rows("SELECT * FROM product_pos_day WHERE cab=? AND date>=?", (cab, since)):
        if r["nm"] in nmset:
            D["P"][r["nm"]][r["date"]] = r
    for r in rows("SELECT * FROM ad_day WHERE cab=? AND date>=?", (cab, since)):
        if r["nm"] in nmset:
            D["A"][r["nm"]][r["date"]] = r
    for r in rows("SELECT * FROM query_day WHERE cab=? AND date>=?", (cab, since_q)):
        if r["nm"] in nmset:
            D["Q"][r["nm"]][r["query"]][r["date"]] = r
    for r in rows("SELECT * FROM ad_query_day WHERE cab=? AND date>=?", (cab, since_q)):
        if r["nm"] in nmset:
            D["AQ"][(r["nm"], r["query"])][r["date"]] = r
    for nm, d, wb, mp in con.execute("SELECT nm, date, stock_wb, stock_mp FROM stock_day WHERE cab=? ORDER BY date",
                                     (cab,)):
        D["stock"][nm] = (d, wb, mp)
    for nm, q, d in con.execute("SELECT nm, query, MIN(date) FROM query_day WHERE cab=? GROUP BY nm, query", (cab,)):
        D["first"][(nm, q)] = d
    return D


def val(series, days, key):
    return [(series.get(iso(d)) or {}).get(key) for d in days]


def product_stats(nm, D, orders30):
    f, pp, ad = D["F"].get(nm, {}), D["P"].get(nm, {}), D["A"].get(nm, {})

    def fsum(days, k):
        return sum(num(x) for x in val(f, days, k))

    s = {"nm": nm, "orders30": orders30}
    for k in ("opens", "carts", "orders", "order_sum"):
        s[k + "7"], s[k + "_p"] = fsum(W7, k), fsum(P7, k)
    s["pos_now"], s["pos_before"] = med(val(pp, NOW, "median_pos")), med(val(pp, BEFORE, "median_pos"))
    s["med_now"] = s["pos_now"]
    s["approx"] = False
    if s["pos_now"] is None or s["pos_before"] is None:       # медианы нет — сравниваем по средней
        s["pos_now"], s["pos_before"] = med(val(pp, NOW, "avg_pos")), med(val(pp, BEFORE, "avg_pos"))
        s["approx"] = s["pos_now"] is not None
    s["avg_now"] = med(val(pp, NOW, "avg_pos"))
    s["vis_now"], s["vis_before"] = mean(val(pp, NOW, "visibility")), mean(val(pp, BEFORE, "visibility"))
    s["price_now"], s["price_before"] = mean(val(f, NOW, "avg_price")), mean(val(f, BEFORE, "avg_price"))
    s["ready_now"], s["ready_before"] = mean(val(f, NOW, "ready_hours")), mean(val(f, BEFORE, "ready_hours"))
    r_now = [x for x in val(f, NOW, "feedback_rating") if x]       # оценка покупателей
    r_bef = [x for x in val(f, BEFORE, "feedback_rating") if x]
    s["rating_now"], s["rating_before"] = (r_now[0] if r_now else None), (r_bef[-1] if r_bef else None)
    s["ad_now"] = sum(num(x) for x in val(ad, NOW, "spend")) / len(NOW)
    s["ad_before"] = sum(num(x) for x in val(ad, BEFORE, "spend")) / len(BEFORE)
    s["ad_pos_now"] = med(val(ad, NOW, "ad_pos"))
    st = D["stock"].get(nm)
    s["stock_wb"], s["stock_mp"] = (st[1], st[2]) if st else (None, None)
    s["stock_days"] = s["stock_wb"] / (s["orders7"] / 7) if (s["stock_wb"] is not None and s["orders7"]) else None
    pw = sw = dn = dp = 0.0
    for q, qr in D["Q"].get(nm, {}).items():
        for d in W7:
            r = qr.get(iso(d))
            if r and r.get("p_cart") is not None and num(r.get("opens")) > 0:
                pw += r["p_cart"] * r["opens"]
                sw += r["opens"]
        a, b = sum(num(x) for x in val(qr, W7, "freq")), sum(num(x) for x in val(qr, P7, "freq"))
        if a and b:
            dn += a
            dp += b
    s["p_cart"] = pw / sw if sw else None
    s["demand"] = dn / dp - 1 if dp else None
    s["cc_now"] = s["carts7"] / s["opens7"] if s["opens7"] else None
    s["cc_p"] = s["carts_p"] / s["opens_p"] if s["opens_p"] else None
    s["oc_now"] = s["orders7"] / s["carts7"] if s["carts7"] else None
    s["oc_p"] = s["orders_p"] / s["carts_p"] if s["carts_p"] else None
    return s


def query_stats(D, tracked):
    res = []
    last14 = [Y - dt.timedelta(days=i) for i in range(13, -1, -1)]
    for nm in tracked:
        for q, qr in D["Q"].get(nm, {}).items():
            def m(days):
                v = med(val(qr, days, "median_pos"))
                return v if v is not None else med(val(qr, days, "avg_pos"))
            r = {"nm": nm, "q": q, "now": m(NOW), "before": m(BEFORE)}
            r["orders10"] = sum(num(x) for x in val(qr, TEN, "orders"))
            r["orders7"] = sum(num(x) for x in val(qr, W7, "orders"))
            r["orders_now"] = sum(num(x) for x in val(qr, NOW, "orders"))
            r["opens7"] = sum(num(x) for x in val(qr, W7, "opens"))
            r["carts7"] = sum(num(x) for x in val(qr, W7, "carts"))
            wf = [x for x in val(qr, W7, "week_freq") if x]
            r["week_freq"] = wf[0] if wf else None
            pw = sw = 0.0
            for d in W7:
                x = qr.get(iso(d))
                if x and x.get("p_cart") is not None and num(x.get("opens")) > 0:
                    pw += x["p_cart"] * x["opens"]
                    sw += x["opens"]
            r["p_cart"] = round(pw / sw) if sw else None
            aq = D["AQ"].get((nm, q), {})
            r["ad_spend10"] = sum(num(x) for x in val(aq, TEN, "spend"))
            r["ad_now"] = sum(num(x) for x in val(aq, NOW, "spend")) / len(NOW)
            r["ad_before"] = sum(num(x) for x in val(aq, BEFORE, "spend")) / len(BEFORE)
            r["ad_pos"] = med(val(aq, NOW, "ad_pos"))
            r["spark"] = [m([d]) for d in last14]
            r["first"] = D["first"].get((nm, q))
            if r["now"] is None and r["before"] is None and not r["orders10"]:
                continue
            res.append(r)
    return res


def causes(p):
    c = []
    if p["stock_wb"] is not None:
        if p["stock_wb"] <= 0:
            c.append("на складах WB 0 шт" + (f" (на своём {p['stock_mp']:.0f})" if p["stock_mp"] else ""))
        elif p["stock_days"] is not None and p["stock_days"] < 7:
            c.append(f"остаток на WB {p['stock_wb']:.0f} шт — на {p['stock_days']:.0f} дн.")
    if p["ready_now"] and p["ready_before"] and p["ready_now"] - p["ready_before"] >= 12:
        c.append(f"доставка дольше: {p['ready_before']:.0f} → {p['ready_now']:.0f} ч")
    if p["price_now"] and p["price_before"] and p["price_now"] / p["price_before"] - 1 >= 0.05:
        c.append(f"цена выросла на {(p['price_now'] / p['price_before'] - 1) * 100:.0f}% "
                 f"({fm(p['price_before'])} → {fm(p['price_now'])} ₽)")
    if p["rating_now"] and p["rating_before"] and p["rating_before"] - p["rating_now"] >= 0.1:
        c.append(f"рейтинг {p['rating_before']:.1f} → {p['rating_now']:.1f}")
    if p["ad_before"] >= 100 and p["ad_now"] < p["ad_before"] * 0.5:
        c.append(f"реклама снизилась: {fm(p['ad_before'])} → {fm(p['ad_now'])} ₽ в день")
    if not c:
        c.append("остатки, доставка, цена, рейтинг и реклама без резких изменений — посмотрите выдачу по главным "
                 "запросам: конкуренты, их цены и акции, СПП")
    return c


def analyze(con, cab, tracked):
    orders30 = {int(k): v for k, v in (meta_get(con, f"orders30:{cab}", {}) or {}).items()}
    D = load(con, cab, tracked)
    names = D["names"]
    watch = load_watch(cab)
    prods = {nm: product_stats(nm, D, orders30.get(nm, 0)) for nm in tracked}
    cab_pos = product_stats(0, D, 0)
    queries = query_stats(D, tracked)
    acts = []

    def vend(nm):
        return (names.get(nm) or {}).get("vendor") or ""

    def act(prio, kind, nm, text, q=""):
        acts.append({"кабинет": cab, "приоритет": prio, "действие": kind, "артикул WB": nm,
                     "артикул продавца": vend(nm), "запрос": q, "что сделать": text})

    # ---- сигналы по товарам ----
    for nm, p in prods.items():
        if p["orders30"] < 3 and nm not in watch:
            continue
        by = " (по средней позиции)" if p["approx"] else ""
        if worse(p["pos_now"], p["pos_before"]):
            act(1, "позиция товара упала", nm, f"медиана {fp(p['pos_before'])} → {fp(p['pos_now'])}{by}. "
                f"Что изменилось: " + "; ".join(causes(p)))
        elif better(p["pos_now"], p["pos_before"]):
            act(5, "позиция товара выросла", nm, f"медиана {fp(p['pos_before'])} → {fp(p['pos_now'])}{by} — "
                "то, что меняли, работает; проверьте, хватит ли остатка на WB")
        if (p["med_now"] and p["avg_now"] and p["avg_now"] >= 2 * p["med_now"]
                and p["avg_now"] - p["med_now"] >= 30):
            act(2, "провалы в выдаче", nm, f"медиана {fp(p['med_now'])}, а средняя {fp(p['avg_now'])}: части "
                "покупателей товар показывается очень далеко — обычно это регионы без остатка или с долгой доставкой; "
                "проверьте остатки по складам и размерам")
        if p["opens_p"] >= 70 and p["opens7"] < p["opens_p"] * (1 - FUNNEL_DROP) and not worse(p["pos_now"], p["pos_before"]):
            if p["demand"] is not None and p["demand"] <= -0.2:
                act(3, "меньше переходов", nm, f"переходов {fm(p['opens_p'])} → {fm(p['opens7'])} "
                    f"({chg(p['opens7'], p['opens_p'])}), но и спрос по запросам товара {p['demand'] * 100:+.0f}% — "
                    "это сезон или спрос, а не карточка")
            else:
                act(2, "меньше переходов", nm, f"переходов {fm(p['opens_p'])} → {fm(p['opens7'])} "
                    f"({chg(p['opens7'], p['opens_p'])}) при тех же позициях — реже кликают: сравните цену (и СПП) "
                    "с конкурентами, главное фото, рейтинг, плашки акций")
        if p["opens7"] >= 150 and p["opens_p"] >= 150 and p["cc_p"] and p["cc_now"] < p["cc_p"] * (1 - CONV_DROP):
            act(2, "хуже конверсия в корзину", nm, f"в корзину {p['cc_p'] * 100:.1f}% → {p['cc_now'] * 100:.1f}%: "
                "проверьте, не закончились ли ходовые размеры, свежие отзывы, цену")
        if p["carts7"] >= 40 and p["carts_p"] >= 40 and p["oc_p"] and p["oc_now"] < p["oc_p"] * (1 - CONV_DROP):
            rd = f", доставка {p['ready_now']:.0f} ч" if p["ready_now"] else ""
            act(2, "хуже конверсия в заказ", nm, f"из корзины в заказ {p['oc_p'] * 100:.0f}% → {p['oc_now'] * 100:.0f}%: "
                f"цена против конкурентов, срок доставки{rd}")
        if p["p_cart"] is not None and p["p_cart"] <= PERCENTILE_BAD and p["opens7"] >= 100:
            act(3, "конверсия хуже конкурентов", nm, f"по тем же запросам в корзину кладут на {abs(p['p_cart']):.0f}% "
                "реже, чем у конкурентов — дело в карточке (фото, цена, размеры, отзывы); ставкой это не лечится")

    # ---- сигналы по запросам ----
    for r in queries:
        nm, q, now, before = r["nm"], r["q"], r["now"], r["before"]
        if now is None:
            continue
        k = klass(r["week_freq"])
        tag = f"«{q}»" + (f" ({k}, {fm(r['week_freq'])}/нед)" if k else "")
        target = (watch.get(nm) or {}).get(q)
        if target and now > target:
            act(2, "мой запрос ниже цели", nm, f"{tag}: цель {target:.0f}, сейчас {fp(now)}"
                + (f" (было {fp(before)})" if before is not None else ""), q)
        if before is None:
            if r["first"] and r["first"] >= iso(Y - dt.timedelta(days=NOW_DAYS - 1)) and r["orders_now"] >= 2:
                act(3, "новый продающий запрос", nm, f"{tag}: появился недавно, заказов уже {r['orders_now']:.0f}, "
                    f"позиция {fp(now)} — добавьте слова запроса в название/характеристики, проверьте ставку", q)
            continue
        ad_fell = r["ad_before"] >= 50 and r["ad_now"] < r["ad_before"] * 0.5
        if before <= TOP_POS and now > 2 * TOP_POS and r["orders10"] >= 2:
            act(1, "запрос выпал из топа", nm, f"{tag}: {fp(before)} → {fp(now)}, заказов за 10 дн {r['orders10']:.0f}."
                + (" Реклама по запросу снизилась." if ad_fell else "")
                + " Проверьте ставку по запросу (кластеру), остаток и цену", q)
        elif now - before >= max(10, before * 0.5) and r["orders10"] >= 3:
            act(2, "запрос опустился", nm, f"{tag}: {fp(before)} → {fp(now)}, заказов за 10 дн {r['orders10']:.0f}"
                + (" — реклама по запросу снизилась" if ad_fell else ""), q)
        elif before > 2 * TOP_POS and now <= TOP_POS:
            act(5, "запрос поднялся в топ", nm, f"{tag}: {fp(before)} → {fp(now)}", q)
        if now <= 5 and before <= 5 and r["orders10"] >= 3 and r["ad_spend10"] >= ORGANIC_SPEND:
            act(3, "в топе — проверить, нужна ли реклама", nm, f"{tag}: стабильно {fp(now)}-е место, "
                f"{r['orders10']:.0f} заказов за 10 дн, реклама по запросу {fm(r['ad_spend10'])} ₽. Снизьте ставку по "
                "этому запросу на 20–30% на 3–5 дней и смотрите позицию здесь: удержится — это экономия", q)
        if now <= TOP_POS and r["opens7"] >= 30 and r["orders7"] == 0:
            t = f"{tag}: позиция {fp(now)}, {r['opens7']:.0f} переходов за 7 дн и ни одного заказа"
            if r["p_cart"] is not None and r["p_cart"] <= PERCENTILE_BAD:
                t += f"; в корзину кладут на {abs(r['p_cart'])}% реже конкурентов"
            sp = sum(num(x) for x in val(D["AQ"].get((nm, q), {}), W7, "spend"))
            if sp >= 300:
                t += f"; реклама {fm(sp)} ₽ — если запрос не про этот товар, это кандидат в минус-фразы"
            act(3, "в топе без заказов", nm, t, q)
    acts.sort(key=lambda a: a["приоритет"])

    # ---- вывод ----
    jl = meta_get(con, f"jam_limit:{cab}")
    days_hist = len(fetched(con, cab, "texts"))
    log(f"Данные по {Y:%d.%m.%Y}. «Сейчас» — {NOW[-1]:%d.%m}–{Y:%d.%m}, «было» — {BEFORE[-1]:%d.%m}–{BEFORE[0]:%d.%m}. "
        f"История запросов: {days_hist} дн." + (f" «Джем»: до {jl} запросов на товар." if jl else ""))
    tp = {k: sum(p[k] for p in prods.values()) for k in ("opens7", "opens_p", "carts7", "carts_p", "orders7", "orders_p",
                                                         "order_sum7", "order_sum_p")}
    log(f"\n  Воронка отслеживаемых товаров: 7 дней против прошлых 7")
    log(f"  {'':26}{'сейчас':>12}{'было':>12}{'изм.':>8}")
    for title, a, b in (("Переходы в карточку", tp["opens7"], tp["opens_p"]), ("Корзины", tp["carts7"], tp["carts_p"]),
                        ("Заказы, шт", tp["orders7"], tp["orders_p"]), ("Заказы, ₽", tp["order_sum7"], tp["order_sum_p"])):
        log(f"  {title:26}{fm(a):>12}{fm(b):>12}{chg(a, b):>8}")
    for title, a1, a2, b1, b2 in (("Конверсия в корзину", tp["carts7"], tp["opens7"], tp["carts_p"], tp["opens_p"]),
                                  ("Конверсия в заказ", tp["orders7"], tp["carts7"], tp["orders_p"], tp["carts_p"])):
        x = a1 / a2 * 100 if a2 else None
        y = b1 / b2 * 100 if b2 else None
        log(f"  {title:26}{fm(x, 1, '%'):>12}{fm(y, 1, '%'):>12}")
    if cab_pos["med_now"] is not None:
        log(f"  {'Медианная позиция кабинета':26}{fp(cab_pos['med_now']):>12}{fp(med(val(D['P'].get(0, {}), BEFORE, 'median_pos'))):>12}"
            f"{move(cab_pos['med_now'], med(val(D['P'].get(0, {}), BEFORE, 'median_pos'))):>8}")

    order = sorted(prods.values(), key=lambda p: (-p["orders7"], -p["orders30"]))
    log("\n  Позиции товаров (↑ — поднялся выше, ↓ — опустился; «≈» — медианы нет, по средней):")
    log(f"  {'артикул':>11} {'арт. продавца':16}{'медиана':>8}{'было':>6}{'изм.':>6}{'средн.':>7}{'видим.':>7}"
        f"{'перех.7д':>9}{'изм.':>6}{'в корз.':>8}{'в зак.':>7}{'дост.ч':>7}{'на WB':>7}{'рекл.поз':>9}")
    for p in order[:25]:
        nm = p["nm"]
        mark_ = "≈" if p["approx"] else ""
        log(f"  {nm:>11} {cut(vend(nm), 16):16}{mark_ + fp(p['pos_now']):>8}{fp(p['pos_before']):>6}"
            f"{move(p['pos_now'], p['pos_before']):>6}{fp(p['avg_now']):>7}{fm(p['vis_now'], 0, '%'):>7}"
            f"{fm(p['opens7']):>9}{chg(p['opens7'], p['opens_p']):>6}"
            f"{fm(p['cc_now'] * 100 if p['cc_now'] is not None else None, 1, '%'):>8}"
            f"{fm(p['oc_now'] * 100 if p['oc_now'] is not None else None, 0, '%'):>7}"
            f"{fm(p['ready_now']):>7}{fm(p['stock_wb']):>7}{fp(p['ad_pos_now']):>9}")

    if any(watch.values()):
        log("\n  Мои запросы (wb_watch.csv): позиция по дням, последние 7 дней (старые → новые):")
        qidx = {(r["nm"], r["q"]): r for r in queries}
        for nm, qs in watch.items():
            for q, target in qs.items():
                r = qidx.get((nm, q))
                spark = " ".join(fp(x) for x in (r["spark"][-7:] if r else [None] * 7))
                log(f"  {nm:>11} {cut(q, 34):34} цель {fp(target):>4}  {spark:32} сейчас {fp(r['now'] if r else None):>4}"
                    f"  было {fp(r['before'] if r else None):>4}  заказы 7д {fm(r['orders7'] if r else None)}")

    best = sorted([r for r in queries if r["orders10"] > 0], key=lambda r: -r["orders10"])[:15]
    if best:
        log("\n  Запросы, которые приносят заказы (10 дней):")
        log(f"  {'артикул':>11} {'запрос':34}{'класс':>6}{'в нед.':>8}{'сейчас':>7}{'было':>6}{'изм.':>6}{'заказы':>7}"
            f"{'vs конк.':>9}{'рекл. ₽':>9}{'рекл.поз':>9}")
        for r in best:
            log(f"  {r['nm']:>11} {cut(r['q'], 34):34}{klass(r['week_freq']):>6}{fm(r['week_freq']):>8}{fp(r['now']):>7}"
                f"{fp(r['before']):>6}{move(r['now'], r['before']):>6}{fm(r['orders10']):>7}"
                f"{(str(r['p_cart']) + '%') if r['p_cart'] is not None else '—':>9}{fm(r['ad_spend10']):>9}{fp(r['ad_pos']):>9}")

    if acts:
        log("\n  Сигналы и что сделать (по важности):")
        shown = defaultdict(int)
        last = None
        for a in acts:
            shown[a["действие"]] += 1
            if shown[a["действие"]] > 12:
                continue
            if a["действие"] != last:
                log(f"   [{a['действие']}]")
                last = a["действие"]
            log(f"     {str(a['артикул WB']):>11} {cut(a['артикул продавца'], 16):16} {a['что сделать']}")
        more = {k: v - 12 for k, v in shown.items() if v > 12}
        if more:
            log("   ещё в wb_positions_actions.csv: " + ", ".join(f"{k} — {v}" for k, v in more.items()))
    else:
        log("\n  Резких изменений нет.")
    if days_hist < NOW_DAYS + BEFORE_DAYS:
        log(f"  (История пока {days_hist} дн. — сравнение «сейчас/было» станет точным после {NOW_DAYS + BEFORE_DAYS} дней.)")

    prow = []
    for p in order:
        nm = p["nm"]
        prow.append({"кабинет": cab, "артикул WB": nm, "артикул продавца": vend(nm),
                     "название": (names.get(nm) or {}).get("title", ""),
                     "медиана сейчас": p["med_now"], "позиция сейчас": p["pos_now"], "позиция было": p["pos_before"],
                     "по средней": "да" if p["approx"] else "", "средняя сейчас": p["avg_now"],
                     "видимость %": round(p["vis_now"], 1) if p["vis_now"] is not None else "",
                     "переходы 7д": p["opens7"], "переходы пред. 7д": p["opens_p"], "корзины 7д": p["carts7"],
                     "заказы 7д": p["orders7"], "заказы пред. 7д": p["orders_p"],
                     "в корзину %": round(p["cc_now"] * 100, 1) if p["cc_now"] is not None else "",
                     "в заказ %": round(p["oc_now"] * 100, 1) if p["oc_now"] is not None else "",
                     "в корзину vs конкуренты %": round(p["p_cart"]) if p["p_cart"] is not None else "",
                     "спрос по запросам %": round(p["demand"] * 100) if p["demand"] is not None else "",
                     "доставка ч": round(p["ready_now"], 1) if p["ready_now"] else "",
                     "цена": round(p["price_now"]) if p["price_now"] else "",
                     "на WB шт": p["stock_wb"] if p["stock_wb"] is not None else "",
                     "реклама ₽/день": round(p["ad_now"], 2), "рекламная позиция": p["ad_pos_now"] or ""})
    qrow = []
    for r in sorted(queries, key=lambda r: (r["nm"], -r["orders10"], r["now"] or 9999)):
        qrow.append({"кабинет": cab, "артикул WB": r["nm"], "артикул продавца": vend(r["nm"]), "запрос": r["q"],
                     "класс": klass(r["week_freq"]), "запросов в неделю": r["week_freq"] or "",
                     "позиция сейчас": r["now"], "позиция было": r["before"],
                     "позиции 14 дней": " ".join(fp(x) for x in r["spark"]),
                     "переходы 7д": r["opens7"], "корзины 7д": r["carts7"], "заказы 7д": r["orders7"],
                     "заказы 10д": r["orders10"], "в корзину vs конкуренты %": r["p_cart"] if r["p_cart"] is not None else "",
                     "реклама 10д ₽": round(r["ad_spend10"], 2), "позиция в рекламе": r["ad_pos"] or "",
                     "впервые": r["first"] or "", "цель": (watch.get(r["nm"]) or {}).get(r["q"]) or ""})
    for rows_ in (prow, qrow):
        for r in rows_:
            for k, v in r.items():
                if isinstance(v, float):
                    r[k] = int(v) if v.is_integer() else round(v, 1)
    return prow, qrow, acts, {"cab": cab, "D": D, "prods": prods, "order": order, "queries": queries,
                              "acts": acts, "watch": watch, "names": names, "cab_pos": cab_pos, "jam_limit": jl}


# ---------------- страница с графиками ----------------

TICKS = [1, 2, 3, 5, 10, 20, 30, 50, 100, 200, 300, 500, 1000, 2000, 3000, 5000, 10000]


def svg_pos(days, series, w=1000, h=210):
    vals = [v for _, s, _ in series for v in s if v]
    if not vals:
        return '<div class="empty">Позиций за этот период нет — история копится с каждым запуском.</div>'
    lo, hi = math.log10(max(1, min(vals) * 0.8)), math.log10(max(vals) * 1.25)
    if hi - lo < 0.5:
        c = (hi + lo) / 2
        lo, hi = max(0, c - 0.25), c + 0.25
    L, R, T, B = 44, 12, 10, 22
    n = len(days)

    def X(i):
        return L + i * (w - L - R) / max(1, n - 1)

    def Yv(v):
        return T + (math.log10(v) - lo) / (hi - lo) * (h - T - B)

    out = [f'<div class="scroll"><svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="Позиции по дням">']
    for t in TICKS:
        if lo <= math.log10(t) <= hi:
            y = Yv(t)
            out.append(f'<line x1="{L}" x2="{w - R}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                       f'<text x="{L - 6}" y="{y + 4:.1f}" class="ax" text-anchor="end">{t}</text>')
    for i in sorted({0, n // 2, n - 1}):
        out.append(f'<text x="{X(i):.1f}" y="{h - 5}" class="ax" text-anchor="middle">{days[i]:%d.%m}</text>')
    for _, s, cls in series:
        d, pen = [], False
        for i, v in enumerate(s):
            if v:
                d.append(f"{'L' if pen else 'M'}{X(i):.1f},{Yv(v):.1f}")
                pen = True
            else:
                pen = False
        if d:
            out.append(f'<path d="{" ".join(d)}" class="{cls}"/>')
        pts = [(i, v) for i, v in enumerate(s) if v]
        if pts:
            i, v = pts[-1]
            out.append(f'<circle cx="{X(i):.1f}" cy="{Yv(v):.1f}" r="3" class="{cls} dot"/>')
    out.append("</svg></div>")
    return "".join(out)


def svg_bars(days, opens, orders, w=1000, h=100):
    mo, mr = max([x or 0 for x in opens] + [1]), max([x or 0 for x in orders] + [1])
    L, R, T, B = 44, 12, 8, 6
    n = len(days)
    bw = (w - L - R) / max(1, n)
    out = [f'<div class="scroll"><svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="Переходы и заказы по дням">',
           f'<text x="{L - 6}" y="{T + 8}" class="ax" text-anchor="end">{fm(mo)}</text>']
    for i, v in enumerate(opens):
        if v:
            bh = v / mo * (h - T - B)
            out.append(f'<rect x="{L + i * bw + 1:.1f}" y="{h - B - bh:.1f}" width="{max(1, bw - 2):.1f}" '
                       f'height="{bh:.1f}" class="bar"/>')
    d, pen = [], False
    for i, v in enumerate(orders):
        if v is not None:
            y = h - B - (v / mr) * (h - T - B)
            d.append(f"{'L' if pen else 'M'}{L + i * bw + bw / 2:.1f},{y:.1f}")
            pen = True
    if d:
        out.append(f'<path d="{" ".join(d)}" class="s-ord"/>')
    out.append("</svg></div>")
    return "".join(out)


def svg_spark(vals, w=130, h=26):
    v = [x for x in vals if x]
    if not v:
        return ""
    lo, hi = math.log10(max(1, min(v) * 0.9)), math.log10(max(v) * 1.1)
    if hi - lo < 0.2:
        c = (hi + lo) / 2
        lo, hi = c - 0.1, c + 0.1
    n = len(vals)
    pts, d, pen = [], [], False
    for i, x in enumerate(vals):
        if x:
            px, py = 2 + i * (w - 4) / max(1, n - 1), 2 + (math.log10(x) - lo) / (hi - lo) * (h - 4)
            d.append(f"{'L' if pen else 'M'}{px:.1f},{py:.1f}")
            pts.append((px, py))
            pen = True
        else:
            pen = False
    last = f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="2.2" class="s-med dot"/>' if pts else ""
    return f'<svg viewBox="0 0 {w} {h}" class="spark"><path d="{" ".join(d)}" class="s-med"/>{last}</svg>'


CSS = """
:root{--bg:#fbfaf7;--fg:#1f1e1c;--muted:#6d6b64;--line:#e6e3da;--card:#ffffff;--c1:#2f6db5;--c2:#9a988f;
--c3:#c4621d;--bar:#d9dde3;--good:#2f7d4f;--bad:#b83a3a;--warn:#a86a10;--chip:#f1efe8}
@media (prefers-color-scheme:dark){:root{--bg:#191917;--fg:#ecebe5;--muted:#a3a198;--line:#33322e;--card:#22211f;
--c1:#6fa8e8;--c2:#8e8c84;--c3:#f0a35e;--bar:#3a3d42;--good:#6cc492;--bad:#f08a8a;--warn:#e6b35a;--chip:#2c2b28}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:24px 16px 48px}h1{font-size:22px;font-weight:600;margin:0 0 4px}
h2{font-size:18px;font-weight:600;margin:32px 0 12px}h3{font-size:16px;font-weight:600;margin:0}
.sub{color:var(--muted);font-size:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 16px}
.kpis{display:flex;flex-wrap:wrap;gap:8px 20px;margin:10px 0 6px;font-size:13px;color:var(--muted)}.kpis b{color:var(--fg);font-weight:600;font-size:15px}
.up{color:var(--good)}.down{color:var(--bad)}.chart{width:100%;min-width:640px;height:auto;display:block}.spark{width:130px;height:26px;vertical-align:middle}
.grid{stroke:var(--line);stroke-width:1}.ax{fill:var(--muted);font-size:11px}
.s-med{fill:none;stroke:var(--c1);stroke-width:2}.s-avg{fill:none;stroke:var(--c2);stroke-width:1.5;stroke-dasharray:5 4}
.s-ad{fill:none;stroke:var(--c3);stroke-width:1.5;stroke-dasharray:2 3}.dot{stroke-dasharray:none}.s-med.dot{fill:var(--c1)}.s-avg.dot{fill:var(--c2)}.s-ad.dot{fill:var(--c3)}
.bar{fill:var(--bar)}.s-ord{fill:none;stroke:var(--c3);stroke-width:2}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--muted);margin:6px 0 2px}
.legend i{display:inline-block;width:18px;height:0;border-top:2px solid var(--c1);vertical-align:middle;margin-right:6px}
.legend i.avg{border-top:2px dashed var(--c2)}.legend i.ad{border-top:2px dotted var(--c3)}.legend i.ord{border-top-color:var(--c3)}
.legend i.bar{border:0;height:10px;width:10px;background:var(--bar)}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}th,td{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:middle}
th{color:var(--muted);font-weight:500;border-top:0}td.n,th.n{text-align:right;white-space:nowrap}.scroll{overflow-x:auto}
.acts{list-style:none;margin:0;padding:0}.acts li{padding:8px 0;border-top:1px solid var(--line);font-size:14px}.acts li:first-child{border-top:0}
.chip{display:inline-block;font-size:12px;padding:1px 8px;border-radius:999px;background:var(--chip);margin-right:8px;color:var(--fg)}
.p1{background:var(--bad);color:var(--card)}.p2{background:var(--warn);color:var(--card)}
.empty{color:var(--muted);font-size:13px;padding:12px 0}
"""


def build_html(sections):
    days = [Y - dt.timedelta(days=i) for i in range(CHART_DAYS - 1, -1, -1)]
    h = ['<!doctype html><html lang="ru"><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1">'
         f'<title>Позиции и воронка WB</title><style>{CSS}</style></head><body><div class="wrap">',
         f'<h1>Позиции и воронка Wildberries</h1><div class="sub">Данные по {Y:%d.%m.%Y}, обновлено '
         f'{dt.datetime.now():%d.%m.%Y %H:%M}. Позиция: чем меньше число, тем выше товар в выдаче; на графиках '
         'шкала логарифмическая (разница между 1 и 10 местом заметнее, чем между 300 и 310).</div>']
    for sec in sections:
        D, names = sec["D"], sec["names"]
        h.append(f'<h2>{html.escape(sec["cab"])}</h2>')
        cp = sec["cab_pos"]
        if cp["med_now"] is not None:
            p0 = D["P"].get(0, {})
            h.append('<div class="card"><h3>Весь кабинет</h3><div class="legend"><span><i></i>медианная позиция</span>'
                     '<span><i class="avg"></i>средняя</span></div>'
                     + svg_pos(days, [("м", val(p0, days, "median_pos"), "s-med"), ("с", val(p0, days, "avg_pos"), "s-avg")])
                     + '</div>')
        acts = [a for a in sec["acts"] if a["приоритет"] <= 3][:25]
        if acts:
            h.append('<div class="card"><h3>Сигналы</h3><ul class="acts">')
            for a in acts:
                h.append(f'<li><span class="chip p{a["приоритет"]}">{html.escape(a["действие"])}</span>'
                         f'<b>{a["артикул WB"]}</b> {html.escape(a["артикул продавца"])} — {html.escape(a["что сделать"])}</li>')
            h.append('</ul></div>')
        show = list(dict.fromkeys(list(sec["watch"]) + [p["nm"] for p in sec["order"]]))[:HTML_PRODUCTS]
        qby = defaultdict(list)
        for r in sec["queries"]:
            qby[r["nm"]].append(r)
        for nm in show:
            p = sec["prods"][nm]
            nmn = names.get(nm) or {}
            pp, f, ad = D["P"].get(nm, {}), D["F"].get(nm, {}), D["A"].get(nm, {})
            mv = move(p["pos_now"], p["pos_before"])
            mcls = "up" if mv.startswith("↑") else ("down" if mv.startswith("↓") else "")
            head = " · ".join(x for x in (str(nm), nmn.get("vendor", "")) if x)
            subl = " · ".join(x for x in (nmn.get("subject", ""), cut(nmn.get("title", ""), 80)) if x)
            h.append(f'<div class="card"><h3>{html.escape(head)}</h3>'
                     f'<div class="sub">{html.escape(subl) or "&nbsp;"}</div>'
                     '<div class="kpis">'
                     f'<span>медиана <b>{fp(p["med_now"])}</b> <span class="{mcls}">{mv}</span></span>'
                     f'<span>средняя <b>{fp(p["avg_now"])}</b></span>'
                     f'<span>видимость <b>{fm(p["vis_now"], 0, "%")}</b></span>'
                     f'<span>переходы 7 дн <b>{fm(p["opens7"])}</b> {chg(p["opens7"], p["opens_p"])}</span>'
                     f'<span>заказы 7 дн <b>{fm(p["orders7"])}</b> {chg(p["orders7"], p["orders_p"])}</span>'
                     f'<span>в корзину <b>{fm(p["cc_now"] * 100 if p["cc_now"] is not None else None, 1, "%")}</b></span>'
                     f'<span>в заказ <b>{fm(p["oc_now"] * 100 if p["oc_now"] is not None else None, 0, "%")}</b></span>'
                     f'<span>доставка <b>{(fm(p["ready_now"]) + " ч") if p["ready_now"] else "—"}</b></span>'
                     f'<span>на WB <b>{fm(p["stock_wb"])}</b></span></div>'
                     '<div class="legend"><span><i></i>медианная позиция</span><span><i class="avg"></i>средняя</span>'
                     '<span><i class="ad"></i>позиция в рекламе</span></div>'
                     + svg_pos(days, [("м", val(pp, days, "median_pos"), "s-med"), ("с", val(pp, days, "avg_pos"), "s-avg"),
                                      ("р", val(ad, days, "ad_pos"), "s-ad")])
                     + '<div class="legend"><span><i class="bar"></i>переходы в карточку</span>'
                       '<span><i class="ord"></i>заказы</span></div>'
                     + svg_bars(days, val(f, days, "opens"), val(f, days, "orders")))
            qs = sorted(qby.get(nm, []), key=lambda r: (-r["orders10"], -r["opens7"]))
            wq = set((sec["watch"].get(nm) or {}))
            qs = [r for r in qs if r["q"] in wq] + [r for r in qs if r["q"] not in wq][:10]
            if qs:
                h.append('<div class="scroll"><table><tr><th>запрос</th><th class="n">в неделю</th><th>14 дней</th>'
                         '<th class="n">сейчас</th><th class="n">было</th><th class="n">заказы 10 дн</th>'
                         '<th class="n">в корзину vs конк.</th><th class="n">реклама 10 дн</th></tr>')
                for r in qs:
                    mv = move(r["now"], r["before"])
                    mcls = "up" if mv.startswith("↑") else ("down" if mv.startswith("↓") else "")
                    star = " ★" if r["q"] in wq else ""
                    h.append(f'<tr><td>{html.escape(r["q"])}{star}</td><td class="n">{fm(r["week_freq"])} '
                             f'{klass(r["week_freq"])}</td><td>{svg_spark(r["spark"])}</td>'
                             f'<td class="n">{fp(r["now"])} <span class="{mcls}">{mv}</span></td>'
                             f'<td class="n">{fp(r["before"])}</td><td class="n">{fm(r["orders10"])}</td>'
                             f'<td class="n">{(str(r["p_cart"]) + "%") if r["p_cart"] is not None else "—"}</td>'
                             f'<td class="n">{fm(r["ad_spend10"])} ₽</td></tr>')
                h.append('</table></div>')
            h.append('</div>')
    h.append('<div class="sub">★ — запрос из вашего списка wb_watch.csv. «Сейчас» — медиана за 3 последних дня, '
             '«было» — за 7 дней до них. «В корзину vs конк.» — насколько чаще (+) или реже (−) кладут в корзину, '
             'чем у конкурентов по этому же запросу.</div></div></body></html>')
    (HERE / "wb_positions.html").write_text("".join(h), encoding="utf-8")


# ---------------- запуск ----------------

def main():
    report_only = "--report" in sys.argv
    bf = arg_value("--backfill")
    cabs = load_tokens(arg_cabinet())
    con = connect()
    log(f"Позиции и воронка WB: {len(cabs)} каб." + (" Только отчёт по сохранённой истории." if report_only else ""))
    allp, allq, alla, sections = [], [], [], []
    for cab, token in cabs.items():
        log(f"\n{'═' * 20} {cab} {'═' * 20}")
        if not report_only:
            first = not fetched(con, cab, "texts")
            window = int(bf) if (bf and bf.isdigit()) else FIRST_BACKFILL
            window = max(1, min(window, 365))
            if first or bf:
                log(f"  Догружаю историю за {window} дн.")
            try:
                collect(con, cab, token, window if (first or bf) else FIRST_BACKFILL)
            except ApiError as e:
                log(f"  ✗ {cab}: {e}")
        tracked = meta_get(con, f"tracked:{cab}", [])
        if not tracked:
            log("  Истории нет — запустите без --report.")
            continue
        p, q, a, sec = analyze(con, cab, tracked)
        allp += p
        allq += q
        alla += a
        sections.append(sec)
    cleanup(con)
    con.close()
    write_csv("wb_positions_products.csv", allp)
    write_csv("wb_positions_queries.csv", allq)
    write_csv("wb_positions_actions.csv", alla or [{"кабинет": "", "приоритет": "", "действие": "", "артикул WB": "",
                                                    "артикул продавца": "", "запрос": "", "что сделать": ""}])
    if sections:
        build_html(sections)
    save_log("wb_positions_report.txt")
    print("\nСохранено: wb_positions_report.txt, wb_positions.html (графики — открыть в браузере), "
          "wb_positions_products.csv, wb_positions_queries.csv, wb_positions_actions.csv; история — wb_history.db")


if __name__ == "__main__":
    main()

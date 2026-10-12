# -*- coding: utf-8 -*-
"""
Общий модуль для скриптов wb_daily.py, wb_unit.py, wb_queries.py, wb_positions.py.
Должен лежать в той же папке, что и они. Сам по себе не запускается.
"""
import csv
import datetime as dt
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

ADV = "https://advert-api.wildberries.ru"
ANA = "https://seller-analytics-api.wildberries.ru"
STAT = "https://statistics-api.wildberries.ru"
FIN = "https://finance-api.wildberries.ru"
COMMON = "https://common-api.wildberries.ru"

HERE = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
TOKENS_FILE = HERE / "wb_tokens.txt"
TODAY = dt.date.today()
YESTERDAY = TODAY - dt.timedelta(days=1)

S = requests.Session()
_out = []


def log(s=""):
    print(s)
    _out.append(str(s))


def save_log(name):
    (HERE / name).write_text("\n".join(_out), encoding="utf-8")


class ApiError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


_last = {}


def pace(token, key, interval):
    k = (token[-16:], key)
    wait = _last.get(k, 0) + interval - time.time()
    if wait > 0:
        time.sleep(wait)
    _last[k] = time.time()


CATEGORY_HINT = {ADV: "Продвижение", ANA: "Аналитика", STAT: "Статистика", FIN: "Финансы", COMMON: "любая",
                 "https://marketplace-api.wildberries.ru": "Маркетплейс",
                 "https://feedbacks-api.wildberries.ru": "Вопросы и отзывы"}


def call(method, url, token, params=None, body=None, what=""):
    for attempt in range(6):
        try:
            r = S.request(method, url, headers={"Authorization": token}, params=params, json=body, timeout=180)
        except requests.RequestException as e:
            if attempt < 3:
                time.sleep(5)
                continue
            raise ApiError(f"{what}: нет связи с WB ({e.__class__.__name__})")
        if r.status_code == 200:
            try:
                return r.json() if r.content else None
            except ValueError:
                return None
        if r.status_code == 204:
            return None
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("X-Ratelimit-Retry") or r.headers.get("Retry-After") or 20)
            except ValueError:
                wait = 20
            if wait > 180:
                raise ApiError(f"{what}: WB просит подождать {wait / 60:.0f} мин — нужен Персональный токен.", 429)
            time.sleep(wait + 1)
            continue
        if r.status_code >= 500 and attempt < 3:
            time.sleep(10)
            continue
        text = r.text[:300].replace("\n", " ")
        hint = ""
        if r.status_code in (401, 403):
            cat = next((v for k, v in CATEGORY_HINT.items() if url.startswith(k)), "")
            hint = f" Проверьте, что у токена есть категория «{cat}»." if cat else ""
        if r.status_code == 402:
            hint = " WB просит платную подписку (Джем) для этого отчёта."
        raise ApiError(f"{what}: ошибка {r.status_code}.{hint} Ответ WB: {text}", r.status_code)
    raise ApiError(f"{what}: WB не ответил после нескольких попыток")


def chunks(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def fm(v, d=0, suffix=""):
    if v is None:
        return "—"
    return f"{v:,.{d}f}".replace(",", " ") + suffix


def pct(a, b):
    return a / b * 100 if b else None


def cut(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


def load_tokens(only=None):
    if not TOKENS_FILE.exists():
        sys.exit(f"Нет файла {TOKENS_FILE}. Сначала запустите wb_ads_report.py — он создаст шаблон.")
    raw = TOKENS_FILE.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    cabs = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, tok = line.split("=", 1)
            tok = tok.strip().strip('"').strip("'")
            if len(tok) >= 20:
                cabs[name.strip()] = tok
    if only:
        cabs = {k: v for k, v in cabs.items() if k == only}
    if not cabs:
        sys.exit("В wb_tokens.txt нет подходящих токенов.")
    return cabs


def arg_cabinet():
    for i, a in enumerate(sys.argv):
        if a == "--cabinet" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def arg_int(default):
    for a in sys.argv[1:]:
        if a.isdigit():
            return int(a)
    return default


def write_csv(path, rows, header=None):
    """Пишет таблицу. Пустой результат тоже записывается (только заголовок), чтобы не остался старый файл
    с прошлыми данными: заголовок — header или из старого файла; если его взять неоткуда — старый файл удаляется."""
    if not rows:
        p = HERE / path
        cols = list(header or [])
        if not cols and p.exists():
            raw = p.read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1251")
            cols = next(csv.reader(text.splitlines()[:1], delimiter=";"), [])
        cols = [c for c in cols if c]
        if cols:
            with open(p, "w", encoding="utf-8-sig", newline="") as f:
                csv.writer(f, delimiter=";").writerow(cols)
        elif p.exists():
            p.unlink()
        return
    cols = list(rows[0].keys())
    for r in rows[1:]:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(HERE / path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow({k: (str(v).replace(".", ",") if isinstance(v, float) else v) for k, v in r.items()})


def read_csv(path):
    p = HERE / path
    if not p.exists():
        return []
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    rows = list(csv.DictReader(text.splitlines(), delimiter=";"))
    return rows


def to_float(s):
    try:
        return float(str(s).replace(" ", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


# ---------------- загрузка данных ----------------

def campaigns(token, statuses=(7, 9, 11), changed_since=None):
    """{id: настройки} кампаний; завершённые — только изменённые после changed_since."""
    pace(token, "count", 0.3)
    data = call("GET", ADV + "/adv/v1/promotion/count", token, what="Список кампаний") or {}
    ids = []
    for g in data.get("adverts") or []:
        st = g.get("status")
        for a in g.get("advert_list") or []:
            if st in (9, 11) and st in statuses:
                ids.append(a["advertId"])
            elif st == 7 and 7 in statuses and (not changed_since or str(a.get("changeTime") or "")[:10] >= changed_since):
                ids.append(a["advertId"])
    res = {i: {"id": i} for i in ids}
    for part in chunks(ids, 50):
        pace(token, "adverts", 0.3)
        try:
            d = call("GET", ADV + "/api/advert/v2/adverts", token, params={"ids": ",".join(map(str, part))},
                     what="Настройки кампаний") or {}
            for a in d.get("adverts") or []:
                res[a["id"]] = a
        except ApiError:
            pass
    return res


def ad_stats(token, ids, d1, d2):
    """Статистика рекламы: {(день, артикул): {views, clicks, sum, orders, sum_price}}, {(кампания, артикул): …}."""
    by_day_nm = defaultdict(lambda: defaultdict(float))
    by_camp_nm = defaultdict(lambda: defaultdict(float))
    a = d1
    while a <= d2:
        b = min(d2, a + dt.timedelta(days=30))
        for part in chunks(ids, 50):
            pace(token, "fullstats", 21)
            try:
                data = call("GET", ADV + "/adv/v3/fullstats", token,
                            params={"ids": ",".join(map(str, part)), "beginDate": a.isoformat(), "endDate": b.isoformat()},
                            what="Статистика рекламы")
            except ApiError as e:
                log(f"  ! {e}")
                continue
            for item in data or []:
                aid = item.get("advertId")
                for day in item.get("days") or []:
                    d = str(day.get("date") or "")[:10]
                    for app in day.get("apps") or []:
                        for n in app.get("nms") or []:
                            for k in ("views", "clicks", "sum", "orders", "shks", "sum_price"):
                                by_day_nm[(d, n.get("nmId"))][k] += num(n.get(k))
                                by_camp_nm[(aid, n.get("nmId"))][k] += num(n.get(k))
        a = b + dt.timedelta(days=1)
    return by_day_nm, by_camp_nm


def funnel(token, start, end, nm_ids=None):
    """Воронка по артикулам за период: заказы, выкупы, отмены, остатки."""
    res, offset = {}, 0
    parts = chunks(nm_ids, 100) if nm_ids else [[]]
    for part in parts:
        offset = 0
        while True:
            pace(token, "funnel", 21)
            body = {"selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()}, "nmIds": part,
                    "brandNames": [], "subjectIds": [], "tagIds": [], "skipDeletedNm": False,
                    "orderBy": {"field": "orderSum", "mode": "desc"}, "limit": 1000, "offset": offset}
            data = call("POST", ANA + "/api/analytics/v3/sales-funnel/products", token, body=body, what="Воронка продаж") or {}
            prods = (data.get("data") or {}).get("products") or []
            for p in prods:
                pr = p.get("product") or {}
                st = (p.get("statistic") or {}).get("selected") or {}
                res[pr.get("nmId")] = {
                    "title": pr.get("title"), "vendor": pr.get("vendorCode"), "brand": pr.get("brandName"),
                    "subject": pr.get("subjectName"), "stock_wb": (pr.get("stocks") or {}).get("wb"),
                    "stock_mp": (pr.get("stocks") or {}).get("mp"),
                    **{k: num(st.get(k)) for k in ("openCount", "cartCount", "orderCount", "orderSum", "buyoutCount",
                                                   "buyoutSum", "cancelCount", "cancelSum", "avgPrice")}}
            if len(prods) < 1000:
                break
            offset += 1000
    return res


def mature_buyout(token):
    """Выкуп по артикулам за «созревший» период (заказы 21–120 дней назад): {nm: доля}, средний."""
    f = funnel(token, TODAY - dt.timedelta(days=120), TODAY - dt.timedelta(days=21))
    tot_b = sum(v["buyoutSum"] for v in f.values())
    tot_r = sum(v["buyoutSum"] + v["cancelSum"] for v in f.values())
    avg = tot_b / tot_r if tot_r else None
    res = {}
    for nm, v in f.items():
        if v["buyoutCount"] + v["cancelCount"] >= 5 and v["buyoutSum"] + v["cancelSum"] > 0:
            res[nm] = v["buyoutSum"] / (v["buyoutSum"] + v["cancelSum"])
    return res, avg


def stat_rows(token, kind, date_from):
    """Заказы (kind="orders") или продажи/возвраты (kind="sales") из «Статистики», изменённые с date_from."""
    rows, cursor = [], date_from.isoformat()
    for _ in range(20):
        pace(token, "stat_" + kind, 61)
        data = call("GET", STAT + f"/api/v1/supplier/{kind}", token, params={"dateFrom": cursor, "flag": 0},
                    what="Заказы" if kind == "orders" else "Продажи") or []
        rows += data
        if len(data) < 80000:
            break
        cursor = data[-1].get("lastChangeDate") or cursor
    seen, uniq = set(), []
    for r in rows:          # при постраничной выгрузке строки на стыке могут повториться
        key = (r.get("srid"), r.get("saleID"), r.get("lastChangeDate"))
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def finance_rows(token, d1, d2, period="weekly"):
    """Детализация отчётов реализации за период (все строки, частями)."""
    rows, rrd = [], 0
    for _ in range(50):
        pace(token, "finance", 61)
        data = call("POST", FIN + "/api/finance/v1/sales-reports/detailed", token, what="Отчёт реализации",
                    body={"dateFrom": d1.isoformat(), "dateTo": d2.isoformat(), "limit": 100000, "rrdId": rrd,
                          "period": period})
        if not data:
            break
        rows += data
        rrd = data[-1].get("rrdId") or 0
        if len(data) < 100000:
            break
    return rows


JAM_KEY = "jam"          # все методы отчёта «Поисковые запросы» (Джем): 3 запроса в минуту
JAM_LIMIT = {}           # {токен: сколько запросов на товар принял WB} — 100 («Продвинутый»/«Премиальный») или 30


def search_texts(token, nm_ids, d1, d2, limit=100):
    """Поисковые запросы по товарам (аналог Вордстата WB): частота, медианная и средняя позиция, переходы,
    корзины, заказы, сравнение с конкурентами. До 100 запросов на товар с «Джем Продвинутый», до 30 — «Стандарт»."""
    res = []
    lim = min(limit, JAM_LIMIT.get(token[-16:], limit))
    for part in chunks(nm_ids, 50):
        body = {"currentPeriod": {"start": d1.isoformat(), "end": d2.isoformat()}, "nmIds": part,
                "topOrderBy": "openCard", "includeSubstitutedSKUs": True, "includeSearchTexts": True,
                "orderBy": {"field": "openCard", "mode": "desc"}, "limit": lim}
        pace(token, JAM_KEY, 21)
        try:
            data = call("POST", ANA + "/api/v2/search-report/product/search-texts", token, body=body,
                        what="Поисковые запросы")
        except ApiError as e:
            if e.status == 400 and lim > 30:      # тариф Джема не позволяет 100 — берём 30
                lim = 30
                body["limit"] = 30
                pace(token, JAM_KEY, 21)
                data = call("POST", ANA + "/api/v2/search-report/product/search-texts", token, body=body,
                            what="Поисковые запросы")
            else:
                raise
        JAM_LIMIT[token[-16:]] = lim
        res += ((data or {}).get("data") or {}).get("items") or []
    return res


def cluster_stats(token, pairs, d1, d2):
    res = defaultdict(dict)
    for part in chunks(pairs, 100):
        pace(token, "nqstats", 7)
        body = {"from": d1.isoformat(), "to": d2.isoformat(), "items": [{"advert_id": a, "nm_id": n} for a, n in part]}
        data = call("POST", ADV + "/adv/v0/normquery/stats", token, body=body, what="Статистика рекламных запросов") or {}
        for it in data.get("stats") or []:
            for s in it.get("stats") or []:
                if s.get("norm_query"):
                    d = res[(it.get("advert_id"), it.get("nm_id"))].setdefault(s["norm_query"].lower(), defaultdict(float))
                    for k in ("views", "clicks", "orders", "atbs", "spend"):
                        d[k] += num(s.get(k))
    return res


def load_unit(cab):
    """Юнит-экономика из wb_unit_economics.csv: {nm: строка}."""
    res = {}
    for r in read_csv("wb_unit_economics.csv"):
        if r.get("кабинет") == cab:
            try:
                res[int(r["артикул WB"])] = r
            except (KeyError, ValueError):
                pass
    return res

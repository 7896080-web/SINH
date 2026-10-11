# -*- coding: utf-8 -*-
"""
Реклама Wildberries по всем кабинетам: расход, заказы, оценка выкупов, товары,
поисковые кластеры. Только чтение — в кабинетах ничего не меняет.

Подготовка (один раз для каждого кабинета):
  Кабинет продавца WB → Настройки → Доступ к API → Создать токен.
  Тип — Персональный. Категории — «Продвижение» и «Аналитика». Отметьте «Только на чтение».
  При первом запуске скрипт создаст рядом с собой файл wb_tokens.txt —
  впишите туда токены (одна строка — один кабинет:  Название = токен).
  Токены никому не пересылайте.

Запуск:  python wb_ads_report.py        — за 30 дней по вчерашний день
         python wb_ads_report.py 14     — за 14 дней
Результат: вывод на экран + wb_ads_report.txt + таблицы для Excel:
           wb_ads_campaigns.csv, wb_ads_products.csv, wb_ads_clusters.csv

Как считается выкуп: WB в рекламной статистике показывает заказы, не выкупы.
Процент выкупа берётся по каждому артикулу из воронки продаж за «созревший» период
(заказы 21–120 дней назад, где почти все уже выкуплены или отменены) и применяется
к рекламным заказам. Если по артикулу мало данных — берётся средний выкуп кабинета.
"""
import csv
import datetime as dt
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

ADV = "https://advert-api.wildberries.ru"
ANA = "https://seller-analytics-api.wildberries.ru"
COMMON = "https://common-api.wildberries.ru"

TOKENS_FILE = Path(__file__).resolve().with_name("wb_tokens.txt")
TEMPLATE = """# Кабинеты Wildberries для wb_ads_report.py
# Одна строка — один кабинет:   Название = токен
# Названия можно менять. Строки без токена пропускаются.
# Токен: кабинет WB → Настройки → Доступ к API → Создать токен,
#        тип «Персональный», категории «Продвижение» и «Аналитика», «Только на чтение».
# Этот файл никому не пересылайте.
Караман 1 =
Караман 2 =
Караман 3 =
Яворская =
"""

DAYS = 30
for _a in sys.argv[1:]:
    if _a.isdigit():
        DAYS = max(1, min(int(_a), 90))
TODAY = dt.date.today()
D2 = TODAY - dt.timedelta(days=1)
D1 = D2 - dt.timedelta(days=DAYS - 1)
MATURE = (TODAY - dt.timedelta(days=120), TODAY - dt.timedelta(days=21))
MIN_RESOLVED = 5          # минимум выкупленных+отменённых шт., чтобы верить выкупу артикула
CLUSTER_PAIRS = 300       # сколько пар «кампания–артикул» (по расходу) смотреть в поисковых кластерах

STATUS = {9: "активна", 11: "пауза", 7: "завершена", 4: "готова", 8: "отменена", -1: "удалена"}
APP = {1: "сайт", 32: "Android", 64: "iOS"}

S = requests.Session()
out = []


def log(s=""):
    print(s)
    out.append(str(s))


class ApiError(Exception):
    pass


_last = {}


def pace(token, key, interval):
    """Пауза между запросами — у WB лимиты на каждый метод."""
    k = (token[-16:], key)
    wait = _last.get(k, 0) + interval - time.time()
    if wait > 0:
        time.sleep(wait)
    _last[k] = time.time()


def _hdr_num(r, name):
    try:
        return float(r.headers.get(name))
    except (TypeError, ValueError):
        return None


def call(method, url, token, params=None, body=None, what=""):
    for attempt in range(6):
        try:
            r = S.request(method, url, headers={"Authorization": token}, params=params, json=body, timeout=120)
        except requests.RequestException as e:
            if attempt < 3:
                time.sleep(5)
                continue
            raise ApiError(f"{what}: нет связи с WB ({e.__class__.__name__})")
        if r.status_code == 200:
            return r.json() if r.content else None
        if r.status_code == 204:
            return None
        if r.status_code == 429:
            wait = _hdr_num(r, "X-Ratelimit-Retry") or _hdr_num(r, "Retry-After") or 20
            if wait > 180:
                raise ApiError(f"{what}: WB просит подождать {wait / 60:.0f} мин. Похоже, токен «Базовый» — "
                               "создайте «Персональный», у него лимиты в десятки раз выше.")
            time.sleep(wait + 1)
            continue
        if r.status_code >= 500 and attempt < 3:
            time.sleep(10)
            continue
        text = r.text[:300].replace("\n", " ")
        if r.status_code == 401:
            hint = ""
            low = text.lower()
            if "scope" in low or "categor" in low:
                hint = " У токена нет нужной категории — добавьте «Продвижение» и «Аналитика»."
            elif "expired" in low:
                hint = " Срок токена истёк — создайте новый."
            raise ApiError(f"{what}: токен не принят (401).{hint} Ответ WB: {text}")
        raise ApiError(f"{what}: ошибка {r.status_code}. Ответ WB: {text}")
    raise ApiError(f"{what}: WB не ответил после нескольких попыток")


def chunks(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def windows(d1, d2, size):
    res, a = [], d1
    while a <= d2:
        b = min(d2, a + dt.timedelta(days=size - 1))
        res.append((a.isoformat(), b.isoformat()))
        a = b + dt.timedelta(days=1)
    return res


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


# ---------------- загрузка ----------------

def load_tokens():
    if not TOKENS_FILE.exists():
        TOKENS_FILE.write_text(TEMPLATE, encoding="utf-8")
        sys.exit(f"Создан файл {TOKENS_FILE}\nВпишите в него токены кабинетов (Название = токен) и запустите скрипт снова.")
    raw = TOKENS_FILE.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    cabs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, tok = line.split("=", 1) if "=" in line else ("", line)
        name, tok = name.strip(), tok.strip().strip('"').strip("'")
        if len(tok) < 20:
            continue
        cabs.append((name or f"Кабинет {len(cabs) + 1}", tok))
    if not cabs:
        sys.exit(f"В {TOKENS_FILE} нет ни одного токена. Впишите их в формате «Название = токен».")
    return cabs


def seller_info(token):
    try:
        return call("GET", COMMON + "/api/v1/seller-info", token, what="Данные продавца") or {}
    except ApiError:
        return {}


def campaign_list(token):
    pace(token, "count", 0.3)
    data = call("GET", ADV + "/adv/v1/promotion/count", token, what="Список кампаний") or {}
    res = {}
    for grp in data.get("adverts") or []:
        st, tp = grp.get("status"), grp.get("type")
        for a in grp.get("advert_list") or []:
            changed = str(a.get("changeTime") or "")[:10]
            if st in (9, 11) or (st == 7 and changed >= D1.isoformat()):
                res[a["advertId"]] = {"status": st, "type": tp}
    return res


def campaign_settings(token, ids):
    info = {}
    for part in chunks(ids, 50):
        pace(token, "adverts", 0.3)
        try:
            data = call("GET", ADV + "/api/advert/v2/adverts", token,
                        params={"ids": ",".join(map(str, part))}, what="Настройки кампаний") or {}
        except ApiError as e:
            log(f"  ! {e}")
            continue
        for a in data.get("adverts") or []:
            s = a.get("settings") or {}
            pl = s.get("placements") or {}
            info[a["id"]] = {"name": s.get("name"), "pay": s.get("payment_type"), "bid": a.get("bid_type"),
                             "search": pl.get("search"), "rec": pl.get("recommendations")}
    return info


def fullstats(token, ids):
    res = []
    for a, b in windows(D1, D2, 31):
        for part in chunks(ids, 50):
            pace(token, "fullstats", 21)
            try:
                data = call("GET", ADV + "/adv/v3/fullstats", token,
                            params={"ids": ",".join(map(str, part)), "beginDate": a, "endDate": b},
                            what="Статистика кампаний")
            except ApiError as e:
                log(f"  ! {e}")
                continue
            if isinstance(data, list):
                res += data
    return res


def funnel(token, start, end, what):
    res, offset = {}, 0
    while True:
        pace(token, "funnel", 21)
        body = {"selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
                "nmIds": [], "brandNames": [], "subjectIds": [], "tagIds": [], "skipDeletedNm": False,
                "orderBy": {"field": "orderSum", "mode": "desc"}, "limit": 1000, "offset": offset}
        data = call("POST", ANA + "/api/analytics/v3/sales-funnel/products", token, body=body, what=what) or {}
        prods = (data.get("data") or {}).get("products") or []
        for p in prods:
            pr = p.get("product") or {}
            st = (p.get("statistic") or {}).get("selected") or {}
            res[pr.get("nmId")] = {
                "title": pr.get("title"), "vendor": pr.get("vendorCode"), "brand": pr.get("brandName"),
                "subject": pr.get("subjectName"), "stock_wb": (pr.get("stocks") or {}).get("wb"),
                "stock_mp": (pr.get("stocks") or {}).get("mp"),
                "orderSum": num(st.get("orderSum")), "orderCount": num(st.get("orderCount")),
                "buyoutSum": num(st.get("buyoutSum")), "buyoutCount": num(st.get("buyoutCount")),
                "cancelSum": num(st.get("cancelSum")), "cancelCount": num(st.get("cancelCount")),
            }
        if len(prods) < 1000:
            return res
        offset += 1000


def clusters(token, pairs):
    """Статистика поисковых кластеров по парам (кампания, артикул)."""
    def fetch(d1, d2):
        rows = []
        for part in chunks(pairs, 100):
            pace(token, "normquery", 7)
            body = {"from": d1, "to": d2, "items": [{"advert_id": a, "nm_id": n} for a, n in part]}
            data = call("POST", ADV + "/adv/v0/normquery/stats", token, body=body, what="Поисковые кластеры") or {}
            for it in data.get("stats") or []:
                for s in it.get("stats") or []:
                    rows.append(dict(s, advert_id=it.get("advert_id"), nm_id=it.get("nm_id")))
        return rows

    if not pairs:
        return [], None
    try:
        return fetch(D1.isoformat(), D2.isoformat()), None
    except ApiError as e:
        if DAYS <= 7:
            log(f"  ! {e}")
            return [], None
        short = (D2 - dt.timedelta(days=6)).isoformat()
        try:
            return fetch(short, D2.isoformat()), f"кластеры — только за последние 7 дней ({e})"
        except ApiError as e2:
            log(f"  ! {e2}")
            return [], None


def expenses(token):
    res = []
    for a, b in windows(D1, D2, 31):
        pace(token, "upd", 1.1)
        try:
            data = call("GET", ADV + "/adv/v1/upd", token, params={"from": a, "to": b}, what="История затрат")
        except ApiError as e:
            log(f"  ! {e}")
            return None
        res += data or []
    return res


# ---------------- обработка одного кабинета ----------------

METRICS = ("views", "clicks", "sum", "atbs", "orders", "shks", "sum_price", "canceled")


def add(dst, src):
    for k in METRICS:
        dst[k] += num(src.get(k))


def analyze(cab_name, token):
    log(f"\n{'═' * 22} {cab_name} {'═' * 22}")
    print("  загружаю данные кабинета...")
    si = seller_info(token)
    if si:
        log(f"Продавец: {si.get('name') or ''}{' («' + si['tradeMark'] + '»)' if si.get('tradeMark') else ''}"
            f"{', ИНН ' + si['tin'] if si.get('tin') else ''}")

    camps = campaign_list(token)
    settings = campaign_settings(token, list(camps)) if camps else {}
    stats = fullstats(token, list(camps)) if camps else []

    c_tot = defaultdict(lambda: defaultdict(float))       # кампания → метрики
    cn_tot = defaultdict(lambda: defaultdict(float))      # (кампания, артикул) → метрики
    n_tot = defaultdict(lambda: defaultdict(float))       # артикул → метрики
    app_tot = defaultdict(lambda: defaultdict(float))     # платформа → метрики
    n_name, pos = {}, defaultdict(list)
    for item in stats:
        aid = item.get("advertId")
        add(c_tot[aid], item)
        for day in item.get("days") or []:
            for app in day.get("apps") or []:
                add(app_tot[app.get("appType")], app)
                for n in app.get("nms") or app.get("nm") or []:
                    nm = n.get("nmId")
                    add(cn_tot[(aid, nm)], n)
                    add(n_tot[nm], n)
                    n_name[nm] = n.get("name") or n_name.get(nm)
        for b in item.get("boosterStats") or []:
            if b.get("avg_position"):
                pos[b.get("nm")].append(num(b.get("avg_position")))

    print("  воронка продаж и выкупы...")
    try:
        fun_now = funnel(token, D1, D2, "Воронка продаж за период")
    except ApiError as e:
        log(f"  ! {e}")
        fun_now = {}
    try:
        fun_old = funnel(token, MATURE[0], MATURE[1], "Воронка продаж для расчёта выкупа")
    except ApiError as e:
        log(f"  ! {e}")
        fun_old = {}

    res_sum = sum(f["buyoutSum"] + f["cancelSum"] for f in fun_old.values())
    avg_buyout = sum(f["buyoutSum"] for f in fun_old.values()) / res_sum if res_sum else None

    def buyout(nm):
        f = fun_old.get(nm)
        if f and f["buyoutCount"] + f["cancelCount"] >= MIN_RESOLVED and f["buyoutSum"] + f["cancelSum"] > 0:
            return f["buyoutSum"] / (f["buyoutSum"] + f["cancelSum"]), True
        return avg_buyout, False

    def est_buyout_sum(pairs_iter):
        """Оценка выкупленной суммы рекламных заказов; None — если выкуп оценить нечем."""
        total = 0.0
        for nm, s_price in pairs_iter:
            b, _ = buyout(nm)
            if b is None:
                return None
            total += s_price * b
        return total

    print("  поисковые кластеры и списания...")
    pairs = sorted(((k, v["sum"]) for k, v in cn_tot.items() if v["sum"] > 0), key=lambda x: -x[1])
    cl_rows, cl_note = clusters(token, [k for k, _ in pairs[:CLUSTER_PAIRS]])
    upd = expenses(token)

    # ---- итоги ----
    tot = defaultdict(float)
    for v in c_tot.values():
        for k in METRICS:
            tot[k] += v[k]
    est_b = est_buyout_sum((nm, v["sum_price"]) for nm, v in n_tot.items())
    all_orders = sum(f["orderSum"] for f in fun_now.values())
    adv_nms = [nm for nm, v in n_tot.items() if v["sum"] > 0]
    adv_nm_orders = sum(fun_now[nm]["orderSum"] for nm in adv_nms if nm in fun_now)

    log(f"Период: {D1:%d.%m}–{D2:%d.%m.%Y} ({DAYS} дн.). Кампаний в работе за период: {len(camps)}, "
        f"с расходом: {sum(1 for v in c_tot.values() if v['sum'] > 0)}.")
    if not tot["sum"]:
        log("Расхода на рекламу за период нет.")
        if all_orders:
            log(f"  {'Все заказы кабинета за период':34}{fm(all_orders, 0, ' ₽'):>16}")
        return ({"кабинет": cab_name, "расход": 0.0, "заказы": 0.0, "выкупы": 0.0, "все заказы": all_orders},
                [], [], [])
    log(f"  {'Расход на рекламу':34}{fm(tot['sum'], 0, ' ₽'):>16}")
    if upd is not None and upd:
        src = defaultdict(float)
        for u in upd:
            src[u.get("paymentType") or "—"] += num(u.get("updSum"))
        log(f"  {'  списано по документам':34}{fm(sum(src.values()), 0, ' ₽'):>16}   "
            + ", ".join(f"{k}: {fm(v, 0)}" for k, v in sorted(src.items(), key=lambda x: -x[1])))
    log(f"  {'Показы / клики':34}{fm(tot['views']) + ' / ' + fm(tot['clicks']):>16}   CTR {fm(pct(tot['clicks'], tot['views']), 2, '%')}")
    log(f"  {'Цена клика':34}{fm(tot['sum'] / tot['clicks'] if tot['clicks'] else None, 1, ' ₽'):>16}")
    log(f"  {'Заказы с рекламы':34}{fm(tot['shks']) + ' шт':>16}   на {fm(tot['sum_price'], 0, ' ₽')}")
    log(f"  {'ДРР по заказам':34}{fm(pct(tot['sum'], tot['sum_price']), 1, '%'):>16}")
    if est_b is not None:
        log(f"  {'Выкуп рекламных заказов (оценка)':34}{fm(est_b, 0, ' ₽'):>16}   "
            f"≈{fm(pct(est_b, tot['sum_price']), 0, '%')} от заказов; средний выкуп кабинета {fm((avg_buyout or 0) * 100, 0, '%')}")
        log(f"  {'ДРР по выкупам (оценка)':34}{fm(pct(tot['sum'], est_b), 1, '%'):>16}")
    else:
        log("  Выкуп оценить не удалось — нет данных воронки продаж (нужна категория «Аналитика»).")
    if all_orders:
        log(f"  {'Все заказы кабинета за период':34}{fm(all_orders, 0, ' ₽'):>16}   реклама дала "
            f"{fm(pct(tot['sum_price'], all_orders), 0, '%')}; ДРР от всех заказов {fm(pct(tot['sum'], all_orders), 1, '%')}")
        if adv_nm_orders:
            log(f"  {'Заказы рекламируемых товаров':34}{fm(adv_nm_orders, 0, ' ₽'):>16}   из них с рекламы "
                f"{fm(pct(tot['sum_price'], adv_nm_orders), 0, '%')}")
    if app_tot:
        log("  Платформы: " + "; ".join(
            f"{APP.get(k, k)} — {fm(v['sum'], 0, ' ₽')}, {fm(v['shks'])} шт, ДРР {fm(pct(v['sum'], v['sum_price']), 1, '%')}"
            for k, v in sorted(app_tot.items(), key=lambda x: -x[1]["sum"])))

    # ---- кампании ----
    camp_rows = []
    for aid, v in c_tot.items():
        st = settings.get(aid, {})
        meta = camps.get(aid, {})
        nm_pairs = [(nm, cv["sum_price"]) for (a, nm), cv in cn_tot.items() if a == aid]
        eb = est_buyout_sum(nm_pairs)
        kind = ("единая" if st.get("bid") == "unified" else "ручная" if st.get("bid") == "manual" else
                f"тип {meta.get('type')}")
        pay = {"cpm": "показы", "cpc": "клики"}.get(st.get("pay"), "")
        camp_rows.append({
            "кабинет": cab_name, "ID": aid, "название": st.get("name") or f"кампания {aid}",
            "ставка": kind, "оплата": pay, "статус": STATUS.get(meta.get("status"), meta.get("status")),
            "поиск": {True: "да", False: "нет"}.get(st.get("search"), ""),
            "рекомендации": {True: "да", False: "нет"}.get(st.get("rec"), ""),
            "расход": round(v["sum"], 2), "показы": int(v["views"]), "клики": int(v["clicks"]),
            "CTR %": round(pct(v["clicks"], v["views"]) or 0, 2),
            "CPC": round(v["sum"] / v["clicks"], 2) if v["clicks"] else "",
            "корзины": int(v["atbs"]), "заказы шт": int(v["shks"]), "заказы ₽": round(v["sum_price"], 2),
            "ДРР заказы %": round(pct(v["sum"], v["sum_price"]), 1) if v["sum_price"] else "",
            "выкупы ₽ (оценка)": round(eb, 2) if eb is not None else "",
            "ДРР выкупы %": round(pct(v["sum"], eb), 1) if eb else "",
        })
    camp_rows.sort(key=lambda r: -r["расход"])
    if camp_rows:
        log("\nКампании (по расходу):")
        log(f"  {'название':34}{'тип':>16}{'статус':>11}{'расход':>10}{'клики':>8}{'CPC':>7}"
            f"{'заказы':>8}{'на сумму':>11}{'ДРР зак.':>9}{'ДРР вык.':>9}")
        for r in camp_rows:
            if not r["расход"]:
                continue
            log(f"  {cut(r['название'], 34):34}{cut(r['ставка'] + ' ' + r['оплата'], 16):>16}{str(r['статус']):>11}"
                f"{fm(r['расход']):>10}{fm(r['клики']):>8}{fm(r['CPC'] or None, 1):>7}{fm(r['заказы шт']):>8}"
                f"{fm(r['заказы ₽']):>11}{fm(r['ДРР заказы %'] or None, 1, '%'):>9}{fm(r['ДРР выкупы %'] or None, 1, '%'):>9}")

    # ---- товары ----
    prod_rows = []
    for nm, v in n_tot.items():
        f = fun_now.get(nm) or fun_old.get(nm) or {}
        b, own = buyout(nm)
        eb = v["sum_price"] * b if b is not None else None
        allo = (fun_now.get(nm) or {}).get("orderSum")
        prod_rows.append({
            "кабинет": cab_name, "артикул WB": nm, "артикул продавца": f.get("vendor") or "",
            "название": f.get("title") or n_name.get(nm) or "", "бренд": f.get("brand") or "",
            "предмет": f.get("subject") or "", "расход": round(v["sum"], 2), "показы": int(v["views"]),
            "клики": int(v["clicks"]), "CTR %": round(pct(v["clicks"], v["views"]) or 0, 2),
            "CPC": round(v["sum"] / v["clicks"], 2) if v["clicks"] else "", "корзины": int(v["atbs"]),
            "заказы рекл шт": int(v["shks"]), "заказы рекл ₽": round(v["sum_price"], 2),
            "все заказы ₽": round(allo, 2) if allo is not None else "",
            "доля рекламы %": round(pct(v["sum_price"], allo), 1) if allo else "",
            "выкуп %": round(b * 100, 1) if b is not None else "",
            "выкуп свой/средний": "свой" if own else "средний",
            "выкупы рекл ₽ (оценка)": round(eb, 2) if eb is not None else "",
            "ДРР заказы %": round(pct(v["sum"], v["sum_price"]), 1) if v["sum_price"] else "",
            "ДРР выкупы %": round(pct(v["sum"], eb), 1) if eb else "",
            "остаток WB шт": f.get("stock_wb") if f.get("stock_wb") is not None else "",
            "остаток свой шт": f.get("stock_mp") if f.get("stock_mp") is not None else "",
            "средняя позиция": round(sum(pos[nm]) / len(pos[nm]), 1) if pos.get(nm) else "",
        })
    prod_rows.sort(key=lambda r: -r["расход"])
    if prod_rows:
        log("\nТовары (топ-25 по расходу):")
        log(f"  {'артикул':>11} {'арт. продавца':16}{'бренд':12}{'расход':>9}{'заказы':>7}{'на сумму':>10}"
            f"{'доля рекл.':>11}{'выкуп':>7}{'ДРР зак.':>9}{'ДРР вык.':>9}{'ост. WB':>8}{'свой':>6}")
        for r in prod_rows[:25]:
            log(f"  {r['артикул WB']:>11} {cut(r['артикул продавца'], 16):16}{cut(r['бренд'], 12):12}"
                f"{fm(r['расход']):>9}{fm(r['заказы рекл шт']):>7}{fm(r['заказы рекл ₽']):>10}"
                f"{fm(r['доля рекламы %'] or None, 0, '%'):>11}{fm(r['выкуп %'] or None, 0, '%'):>7}"
                f"{fm(r['ДРР заказы %'] or None, 1, '%'):>9}{fm(r['ДРР выкупы %'] or None, 1, '%'):>9}"
                f"{fm(r['остаток WB шт'] if r['остаток WB шт'] != '' else None):>8}"
                f"{fm(r['остаток свой шт'] if r['остаток свой шт'] != '' else None):>6}")
        zero = [r for r in prod_rows if r["расход"] >= 500 and not r["заказы рекл шт"]]
        if zero:
            log(f"\nТовары с расходом от 500 ₽ и без единого заказа с рекламы: {len(zero)} шт., "
                f"{fm(sum(r['расход'] for r in zero), 0, ' ₽')}")
            for r in zero[:15]:
                log(f"  {r['артикул WB']:>11} {cut(r['артикул продавца'], 16):16}{cut(r['название'], 34):34}"
                    f"{fm(r['расход'], 0, ' ₽'):>10}{fm(r['клики']):>7} кликов   остаток WB {fm(r['остаток WB шт'] if r['остаток WB шт'] != '' else None)}, свой {fm(r['остаток свой шт'] if r['остаток свой шт'] != '' else None)}")
        nostock = [r for r in prod_rows if r["расход"] >= 300 and r["остаток WB шт"] == 0]
        if nostock:
            own = [r for r in nostock if (r["остаток свой шт"] or 0) > 0]
            log(f"\nРекламировались, а сейчас на складах WB их нет — товаров: {len(nostock)} "
                f"(за период на них ушло {fm(sum(r['расход'] for r in nostock), 0, ' ₽')}, "
                f"заказов с рекламы {fm(sum(r['заказы рекл шт'] for r in nostock))} шт.). "
                f"Из них есть на своём складе: {len(own)}.")
            low = sorted((r for r in prod_rows if r["заказы рекл шт"] >= 5 and r["остаток WB шт"] != ''
                          and r["остаток WB шт"] <= 10), key=lambda r: -r["заказы рекл шт"])
            if low:
                log("  Ходовые товары с рекламы (от 5 заказов), которых на WB осталось 10 шт. и меньше:")
                for r in low[:15]:
                    log(f"    {r['артикул WB']:>11} {cut(r['артикул продавца'], 22):22}{cut(r['название'], 28):28}"
                        f"{fm(r['заказы рекл шт']):>4} зак. с рекламы   на WB {fm(r['остаток WB шт'])}, свой {fm(r['остаток свой шт'] if r['остаток свой шт'] != '' else None)}")

    # ---- кластеры ----
    camp_name = {r["ID"]: r["название"] for r in camp_rows}
    cl_out = []
    for s in cl_rows:
        cl_out.append({
            "кабинет": cab_name, "кампания": camp_name.get(s.get("advert_id"), s.get("advert_id")),
            "ID кампании": s.get("advert_id"), "артикул WB": s.get("nm_id"), "кластер": s.get("norm_query"),
            "показы": s.get("views") if s.get("views") is not None else "", "клики": int(num(s.get("clicks"))),
            "CTR %": s.get("ctr") if s.get("ctr") is not None else "", "CPC": s.get("cpc") or "",
            "расход": round(num(s.get("spend")), 2), "корзины": int(num(s.get("atbs"))),
            "заказы": int(num(s.get("orders"))), "шт": int(num(s.get("shks"))),
            "средняя позиция": s.get("avg_pos") or "",
        })
    if cl_out:
        log(f"\nПоисковые кластеры{' (' + cl_note + ')' if cl_note else ''} — сведены по всему кабинету:")
        agg = defaultdict(lambda: {"spend": 0.0, "clicks": 0, "orders": 0, "nms": set(), "camps": set()})
        for c in cl_out:
            a = agg[c["кластер"]]
            a["spend"] += c["расход"]
            a["clicks"] += c["клики"]
            a["orders"] += c["заказы"]
            a["nms"].add(c["артикул WB"])
            a["camps"].add(c["ID кампании"])
        bad = sorted(((k, a) for k, a in agg.items() if a["spend"] > 0 and not a["orders"]), key=lambda x: -x[1]["spend"])
        spent = sum(a["spend"] for a in agg.values())
        log(f"  Кластеров: {len(agg)}, расход по ним {fm(spent, 0, ' ₽')}. "
            f"Без единого заказа: {len(bad)} на {fm(sum(a['spend'] for _, a in bad), 0, ' ₽')}")
        if bad:
            log("  Без заказов, топ-20 по расходу — кандидаты в минус-фразы (сначала проверьте смысл запроса):")
            for k, a in bad[:20]:
                log(f"    {cut(k, 42):42}{fm(a['spend'], 0, ' ₽'):>9}{fm(a['clicks']):>6} кл."
                    f"   артикулов {len(a['nms'])}, кампаний {len(a['camps'])}")
        good = sorted(((k, a) for k, a in agg.items() if a["orders"]), key=lambda x: (-x[1]["orders"], x[1]["spend"]))
        if good:
            log("  Лучшие по заказам (топ-10):")
            for k, a in good[:10]:
                log(f"    {cut(k, 42):42}{fm(a['orders']):>5} зак.{fm(a['spend'], 0, ' ₽'):>9}"
                    f"   цена заказа {fm(a['spend'] / a['orders'], 0, ' ₽')}")
    elif pairs:
        log("\nПоисковые кластеры: данных нет (бывает у кампаний только в рекомендациях).")

    summary = {"кабинет": cab_name, "расход": tot["sum"], "заказы": tot["sum_price"], "выкупы": est_b,
               "все заказы": all_orders}
    return summary, camp_rows, prod_rows, cl_out


def write_csv(name, rows):
    if not rows:
        return
    cols = list(rows[0].keys())
    with open(name, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow({k: (str(v).replace(".", ",") if isinstance(v, float) else v) for k, v in r.items()})


def main():
    cabs = load_tokens()
    log(f"Реклама Wildberries: {len(cabs)} каб., период {D1:%d.%m.%Y}–{D2:%d.%m.%Y} ({DAYS} дн.). Только чтение.")
    log("Суммы — как в кабинете WB. Заказы — по данным рекламной статистики WB; выкуп — оценка по истории артикула.")
    print("На каждый кабинет уходит 1–3 минуты: у WB строгие лимиты на частоту запросов.")
    summaries, all_c, all_p, all_cl = [], [], [], []
    for name, token in cabs:
        try:
            s, c, p, cl = analyze(name, token)
        except ApiError as e:
            log(f"  ✗ {name}: {e}")
            continue
        except Exception as e:  # чтобы один кабинет не останавливал остальные
            log(f"  ✗ {name}: непредвиденная ошибка {e.__class__.__name__}: {e}")
            continue
        summaries.append(s)
        all_c += c
        all_p += p
        all_cl += cl

    if summaries:
        log(f"\n{'═' * 22} Все кабинеты {'═' * 22}")
        log(f"  {'кабинет':20}{'расход':>11}{'заказы рекл.':>14}{'ДРР зак.':>10}{'выкупы (оц.)':>14}"
            f"{'ДРР вык.':>10}{'все заказы':>13}{'ДРР от всех':>13}")
        T = defaultdict(float)
        has_b = all(s["выкупы"] is not None for s in summaries)
        for s in summaries:
            for k in ("расход", "заказы", "все заказы"):
                T[k] += s[k] or 0
            T["выкупы"] += s["выкупы"] or 0
            log(f"  {cut(s['кабинет'], 20):20}{fm(s['расход']):>11}{fm(s['заказы']):>14}"
                f"{fm(pct(s['расход'], s['заказы']), 1, '%'):>10}{fm(s['выкупы']):>14}"
                f"{fm(pct(s['расход'], s['выкупы']) if s['выкупы'] else None, 1, '%'):>10}"
                f"{fm(s['все заказы'] or None):>13}{fm(pct(s['расход'], s['все заказы']), 1, '%'):>13}")
        log(f"  {'ИТОГО':20}{fm(T['расход']):>11}{fm(T['заказы']):>14}{fm(pct(T['расход'], T['заказы']), 1, '%'):>10}"
            f"{fm(T['выкупы'] if has_b else None):>14}{fm(pct(T['расход'], T['выкупы']) if has_b and T['выкупы'] else None, 1, '%'):>10}"
            f"{fm(T['все заказы'] or None):>13}{fm(pct(T['расход'], T['все заказы']), 1, '%'):>13}")

    write_csv("wb_ads_campaigns.csv", all_c)
    write_csv("wb_ads_products.csv", all_p)
    write_csv("wb_ads_clusters.csv", all_cl)
    Path("wb_ads_report.txt").write_text("\n".join(out), encoding="utf-8")
    print("\nСохранено: wb_ads_report.txt, wb_ads_campaigns.csv, wb_ads_products.csv, wb_ads_clusters.csv")


if __name__ == "__main__":
    main()

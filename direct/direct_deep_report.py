# -*- coding: utf-8 -*-
"""
Яндекс Директ API v5 — подробные отчёты за 30 дней с конверсиями по целям Метрики:
  1) площадки РСЯ            -> direct_placements.csv
  2) поисковые запросы       -> direct_queries.csv
  3) группы объявлений       -> direct_adgroups.csv
  4) устройства, пол, возраст -> direct_audience.csv
Итоговая сводка            -> direct_deep_report.txt
Расход — без НДС (как бюджеты в Директе); «реальная цена покупки» — расход ÷ соинвест (coinvest_multiplier).

Запуск: python direct_deep_report.py
"""
import csv
import io
import time

import requests

from yandex_token import yandex_token, crm_config
TOKEN = yandex_token()   # из настроек AWER CRM
CFG = crm_config()
CLIENT_LOGIN = CFG.get("direct_client_login", "")   # из настроек CRM, если задан
# соинвест Яндекса: реальные деньги = расход ÷ COINVEST. Не задан в CRM — ×2 до 31.12.2026, потом ×1 (без соинвеста)
COINVEST = float(CFG.get("coinvest_multiplier") or (2.0 if time.strftime("%Y-%m-%d") <= "2026-12-31" else 1.0))
PERIOD = "LAST_30_DAYS"

# Цели Метрики (счётчик 110246968)
GOALS = {
    "579065793": "покупка",          # Ecommerce: purchase
    "575272268": "оформление",       # Успешное оформление заказа
    "575272272": "доставлен",        # Заказ доставлен
}
ATTRIBUTION = "LSC"               # последний значимый переход

URL = "https://api.direct.yandex.com/json/v5/reports"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}", "Accept-Language": "ru",
    "processingMode": "auto", "returnMoneyInMicros": "false",
    "skipReportHeader": "true", "skipReportSummary": "true",
}
if CLIENT_LOGIN:
    HEADERS["Client-Login"] = CLIENT_LOGIN

out = []
def log(s=""):
    print(s)
    out.append(str(s))


def report(name, rtype, fields, with_goals=True):
    params = {
        "SelectionCriteria": {}, "FieldNames": fields, "ReportName": f"{name}_{int(time.time())}",
        "ReportType": rtype, "DateRangeType": PERIOD, "Format": "TSV", "IncludeVAT": "NO",
    }
    if with_goals:
        params["Goals"] = list(GOALS)
        params["AttributionModels"] = [ATTRIBUTION]
    for _ in range(60):
        r = requests.post(URL, json={"params": params}, headers=HEADERS, timeout=90)
        if r.status_code == 200:
            rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8")), delimiter="\t"))
            return rows
        if r.status_code in (201, 202):
            time.sleep(int(r.headers.get("retryIn", 5)))
            continue
        log(f"[{name}] ошибка {r.status_code}: {r.text[:400]}")
        return []
    log(f"[{name}] отчёт не успел сформироваться")
    return []


def num(v):
    try:
        return float(v) if v not in ("--", "", None) else 0.0
    except ValueError:
        return 0.0


def conv(row, goal):
    return num(row.get(f"Conversions_{goal}_{ATTRIBUTION}"))


def enrich(rows):
    for r in rows:
        for gid, gname in GOALS.items():
            r[gname] = conv(r, gid)
    return rows


def fmt(v):
    # числа — с запятой, чтобы Excel на русском понимал их как числа
    if isinstance(v, float):
        return f"{v:g}".replace(".", ",")
    if isinstance(v, str) and v.replace(".", "", 1).isdigit():
        return v.replace(".", ",")
    return v


def save(rows, fname):
    if not rows:
        return
    cols = [c for c in rows[0].keys() if not c.startswith("Conversions_")]
    with open(fname, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: fmt(v) for k, v in r.items() if k in cols})


def summary(rows, key, title, top=25):
    agg = {}
    for r in rows:
        k = r[key] if isinstance(key, str) else key(r)
        a = agg.setdefault(k, {"показы": 0, "клики": 0, "расход": 0.0, **{g: 0.0 for g in GOALS.values()}})
        a["показы"] += num(r.get("Impressions"))
        a["клики"] += num(r.get("Clicks"))
        a["расход"] += num(r.get("Cost"))
        for g in GOALS.values():
            a[g] += r[g]
    log(f"\n=== {title} (топ-{top} по расходу; расход кабинета без НДС) ===")
    log(f"название\tпоказы\tклики\tрасход\tпокупки\tоформления\tдоставлено\tреальная цена покупки (расход ÷ {COINVEST:g})")
    for k, a in sorted(agg.items(), key=lambda x: -x[1]["расход"])[:top]:
        cpa = f"{a['расход'] / COINVEST / a['покупка']:.0f}" if a["покупка"] else "—"
        log(f"{k}\t{a['показы']:.0f}\t{a['клики']:.0f}\t{a['расход']:.0f}\t{a['покупка']:.0f}\t"
            f"{a['оформление']:.0f}\t{a['доставлен']:.0f}\t{cpa}")
    return agg


goal_cols = ["Conversions"]

# 1. Площадки
pl = enrich(report("placements", "CUSTOM_REPORT",
                   ["CampaignName", "AdNetworkType", "Placement", "Impressions", "Clicks", "Cost"] + goal_cols))
save(pl, "direct_placements.csv")
net = [r for r in pl if r.get("AdNetworkType") == "AD_NETWORK"]
agg = summary(net, "Placement", "Площадки РСЯ")
waste = {k: a for k, a in agg.items() if a["покупка"] == 0 and a["оформление"] == 0 and a["расход"] >= 500}
if pl and not any(r["покупка"] or r["оформление"] for r in pl):
    # у ЕПК API Директа отдаёт конверсии нулями — список «плохих» площадок был бы ложным
    log("\nКонверсии (покупки/оформления) в API Директа везде нулевые — для единых перфоманс-кампаний (ЕПК)")
    log("API их не отдаёт. Площадки без покупок по этим данным не определить — смотрите покупки в Метрике")
    log("(metrika_direct_breakdown.py) или в Мастере отчётов Директа.")
elif waste:
    total_waste = sum(a["расход"] for a in waste.values())
    log(f"\nПлощадок с расходом от 500 ₽ и без единого оформления/покупки: {len(waste)}, расход {total_waste:.0f} ₽")
    for k, a in sorted(waste.items(), key=lambda x: -x[1]["расход"])[:40]:
        log(f"  {k}\t{a['расход']:.0f} ₽\t{a['клики']:.0f} кликов")

# 2. Поисковые запросы
q = enrich(report("queries", "SEARCH_QUERY_PERFORMANCE_REPORT",
                  ["CampaignName", "Query", "Impressions", "Clicks", "Cost"] + goal_cols))
save(q, "direct_queries.csv")
summary(q, "Query", "Поисковые запросы")

# 3. Группы объявлений
g = enrich(report("adgroups", "CUSTOM_REPORT",
                  ["CampaignName", "AdGroupName", "Impressions", "Clicks", "Cost"] + goal_cols))
save(g, "direct_adgroups.csv")
summary(g, lambda r: f"{r['CampaignName']} / {r['AdGroupName']}", "Группы объявлений", top=30)

# 4. Аудитория
au = enrich(report("audience", "CUSTOM_REPORT",
                   ["Device", "Gender", "Age", "Impressions", "Clicks", "Cost"] + goal_cols))
save(au, "direct_audience.csv")
summary(au, "Device", "Устройства")
summary(au, "Gender", "Пол")
summary(au, "Age", "Возраст")

with open("direct_deep_report.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("\nСохранено: direct_deep_report.txt и CSV-файлы")

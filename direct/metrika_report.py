# -*- coding: utf-8 -*-
"""
Яндекс Метрика API: счётчики, цели и основные отчёты за последние 30 дней.

Запуск:  python metrika_report.py
Результат: вывод в консоль + файл metrika_report.txt
"""
import requests

from yandex_token import yandex_token
TOKEN = yandex_token()   # из настроек AWER CRM
COUNTER_ID = ""                  # оставьте пустым — скрипт найдёт счётчик сайта сам
SITE = "awer-russia.ru"
DATE1, DATE2 = "30daysAgo", "yesterday"

API = "https://api-metrika.yandex.net"
HEADERS = {"Authorization": f"OAuth {TOKEN}"}

out = []
def log(s=""):
    print(s)
    out.append(str(s))


def get(path, params=None):
    r = requests.get(API + path, headers=HEADERS, params=params, timeout=60)
    if r.status_code != 200:
        try:
            msg = r.json().get("message")
        except Exception:
            msg = r.text[:300]
        raise RuntimeError(f"HTTP {r.status_code}: {msg}")
    return r.json()


def report(title, dimensions, metrics, filters=None, sort=None, limit=50):
    params = {"ids": COUNTER_ID, "date1": DATE1, "date2": DATE2,
              "dimensions": ",".join(dimensions), "metrics": ",".join(metrics),
              "accuracy": "full", "limit": limit, "lang": "ru"}
    if filters:
        params["filters"] = filters
    if sort:
        params["sort"] = sort
    log(f"\n=== {title} ===")
    try:
        data = get("/stat/v1/data", params)
    except RuntimeError as e:
        log(f"Не удалось получить отчёт: {e}")
        return
    names = [m.split(":")[-1] for m in metrics]
    log("\t".join([d.split(":")[-1] for d in dimensions] + names))
    for row in data.get("data", []):
        dims = [str(d.get("name")) for d in row["dimensions"]]
        vals = [f"{v:.2f}".rstrip("0").rstrip(".") for v in row["metrics"]]
        log("\t".join(dims + vals))
    totals = [f"{v:.2f}".rstrip("0").rstrip(".") for v in data.get("totals", [])]
    log("ИТОГО\t" + "\t".join(totals))


# 1. Счётчики
counters = get("/management/v1/counters").get("counters", [])
log(f"Доступно счётчиков: {len(counters)}")
for c in counters:
    log(f"  {c['id']} | {c.get('name')} | {c.get('site')} | статус {c.get('code_status')}")

if not COUNTER_ID:
    match = [c for c in counters if SITE in (c.get("site") or "")]
    if not match:
        raise SystemExit(f"Счётчик для {SITE} не найден. Впишите нужный ID в COUNTER_ID.")
    COUNTER_ID = str(match[0]["id"])
log(f"\nРаботаем со счётчиком {COUNTER_ID}")

# 2. Цели
goals = get(f"/management/v1/counter/{COUNTER_ID}/goals").get("goals", [])
log(f"\nЦелей: {len(goals)}")
for g in goals:
    log(f"  {g['id']} | {g.get('name')} | тип {g.get('type')}")

base = ["ym:s:visits", "ym:s:users", "ym:s:bounceRate", "ym:s:pageDepth", "ym:s:avgVisitDurationSeconds"]
goal_metrics = [f"ym:s:goal{g['id']}reaches" for g in goals[:8]]

# 3. Отчёты
report("Источники трафика", ["ym:s:lastsignTrafficSource"], base, sort="-ym:s:visits")
report("Рекламные системы", ["ym:s:lastsignAdvEngine"], base + goal_metrics, sort="-ym:s:visits")
report("UTM-метки: источник и кампания", ["ym:s:lastsignUTMSource", "ym:s:lastsignUTMCampaign"],
       base + goal_metrics, sort="-ym:s:visits")
report("Директ: визиты с рекламы по кампаниям", ["ym:s:lastsignDirectClickOrderName"],
       base + goal_metrics, filters="ym:s:lastsignTrafficSource=='ad'", sort="-ym:s:visits")
report("Устройства", ["ym:s:deviceCategory"], base, sort="-ym:s:visits")
report("Электронная коммерция по источникам", ["ym:s:lastsignTrafficSource"],
       ["ym:s:visits", "ym:s:ecommercePurchases", "ym:s:ecommerceRevenue"], sort="-ym:s:visits")

with open("metrika_report.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("\nСохранено в metrika_report.txt")

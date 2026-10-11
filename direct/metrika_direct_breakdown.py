# -*- coding: utf-8 -*-
"""
Покупки и доставленные заказы с рекламы Директа по площадкам, группам,
возрасту, полу и устройствам — по данным Метрики (последний значимый переход).
Запуск: python metrika_direct_breakdown.py  -> metrika_direct_breakdown.txt
"""
import requests

from yandex_token import yandex_token
TOKEN = yandex_token()   # из настроек AWER CRM
COUNTER_ID = "110246968"
DATE1, DATE2 = "30daysAgo", "yesterday"

GOAL_ORDER = "575272268"      # Успешное оформление заказа
GOAL_DELIVERED = "575272272"  # Заказ доставлен
METRICS = ["ym:s:visits", "ym:s:bounceRate", "ym:s:ecommercePurchases", "ym:s:ecommerceRevenue",
           f"ym:s:goal{GOAL_ORDER}reaches", f"ym:s:goal{GOAL_DELIVERED}reaches"]
HEAD = "визиты\tотказы%\tпокупки\tвыручка\tоформления\tдоставлено"

out = []
def log(s=""):
    print(s)
    out.append(str(s))


def report(title, dim, limit=60):
    params = {"ids": COUNTER_ID, "date1": DATE1, "date2": DATE2, "dimensions": dim,
              "metrics": ",".join(METRICS), "filters": "ym:s:lastsignTrafficSource=='ad'",
              "sort": "-ym:s:visits", "limit": limit, "accuracy": "full", "lang": "ru"}
    r = requests.get("https://api-metrika.yandex.net/stat/v1/data",
                     headers={"Authorization": f"OAuth {TOKEN}"}, params=params)
    log(f"\n=== {title} ===")
    if r.status_code != 200:
        log(f"ошибка {r.status_code}: {r.text[:300]}")
        return
    data = r.json()
    log(f"значение\t{HEAD}")
    for row in data.get("data", []):
        name = row["dimensions"][0].get("name")
        m = row["metrics"]
        log(f"{name}\t{m[0]:.0f}\t{m[1]:.0f}\t{m[2]:.0f}\t{m[3]:.0f}\t{m[4]:.0f}\t{m[5]:.0f}")
    t = data.get("totals", [])
    if t:
        log(f"ИТОГО\t{t[0]:.0f}\t{t[1]:.0f}\t{t[2]:.0f}\t{t[3]:.0f}\t{t[4]:.0f}\t{t[5]:.0f}")


report("Площадки", "ym:s:lastsignDirectPlatform", limit=80)
report("Группы объявлений", "ym:s:lastsignDirectBannerGroup")
report("Возраст", "ym:s:ageInterval")
report("Пол", "ym:s:gender")
report("Устройства", "ym:s:deviceCategory")

with open("metrika_direct_breakdown.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("\nСохранено в metrika_direct_breakdown.txt")

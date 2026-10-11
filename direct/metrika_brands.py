# -*- coding: utf-8 -*-
"""
Продажи с рекламы по брендам и товарам (электронная коммерция Метрики).
Токен и счётчик берутся из настроек AWER CRM.
Запуск: python metrika_brands.py [дней]  -> вывод + metrika_brands.txt  (по умолчанию 60 дней)
"""
import sys
from pathlib import Path

import requests

from yandex_token import yandex_token, crm_config

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 60
TOKEN = yandex_token()   # из настроек AWER CRM
COUNTER = crm_config().get("metrika_counter_id") or "110246968"
H = {"Authorization": f"OAuth {TOKEN}"}
out = []


def log(s=""):
    print(s)
    out.append(str(s))


METRIC_SETS = [
    ["ym:s:productImpressions", "ym:s:productBasketsQuantity", "ym:s:productPurchasedQuantity", "ym:s:productPurchasedPrice"],
    ["ym:s:productPurchasedQuantity", "ym:s:productPurchasedPrice"],
]
NAMES = {"ym:s:productImpressions": "просмотры", "ym:s:productBasketsQuantity": "в корзину",
         "ym:s:productPurchasedQuantity": "куплено шт.", "ym:s:productPurchasedPrice": "выручка ₽"}


def report(title, dim, limit=30, ad_only=True):
    for metrics in METRIC_SETS:
        params = {"ids": COUNTER, "date1": f"{DAYS}daysAgo", "date2": "yesterday", "dimensions": dim,
                  "metrics": ",".join(metrics), "sort": f"-{metrics[-1]}", "limit": limit,
                  "accuracy": "full", "lang": "ru"}
        if ad_only:
            params["filters"] = "ym:s:lastsignTrafficSource=='ad'"
        r = requests.get("https://api-metrika.yandex.net/stat/v1/data", headers=H, params=params, timeout=60)
        if r.status_code == 200:
            data = r.json()
            log(f"\n=== {title} ({DAYS} дн.) ===")
            log("значение\t" + "\t".join(NAMES[m] for m in metrics))
            for row in data.get("data", []):
                vals = "\t".join(f"{v:.0f}" for v in row["metrics"])
                log(f"{row['dimensions'][0].get('name')}\t{vals}")
            log("ИТОГО\t" + "\t".join(f"{v:.0f}" for v in data.get("totals", [])))
            return
        last = r.text[:300]
    log(f"\n=== {title}: Метрика не отдала отчёт — {last}")


report("Бренды, реклама", "ym:s:productBrand")
report("Товары, реклама (топ-30 по выручке)", "ym:s:productName")
report("Категории, реклама", "ym:s:productCategoryLevel2")
report("Бренды, весь сайт", "ym:s:productBrand", ad_only=False)

Path("metrika_brands.txt").write_text("\n".join(out), encoding="utf-8")
print("\nСохранено в metrika_brands.txt")

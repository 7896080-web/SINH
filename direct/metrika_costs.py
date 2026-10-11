# -*- coding: utf-8 -*-
"""
Расходы Яндекс Директа через API Метрики (без доступа к API Директа).
Запуск: python metrika_costs.py  -> вывод + metrika_costs.txt
"""
import requests

from yandex_token import yandex_token
TOKEN = yandex_token()   # из настроек AWER CRM
COUNTER_ID = "110246968"
DIRECT_LOGIN = "grdshop"

params = {
    "ids": COUNTER_ID, "date1": "30daysAgo", "date2": "yesterday",
    "direct_client_logins": DIRECT_LOGIN,
    "dimensions": "ym:ad:directOrder",
    "metrics": "ym:ad:clicks,ym:ad:RUBAdCost,ym:ad:visits",
    "accuracy": "full", "lang": "ru",
}
r = requests.get("https://api-metrika.yandex.net/stat/v1/data",
                 headers={"Authorization": f"OAuth {TOKEN}"}, params=params)
lines = []
if r.status_code != 200:
    lines.append(f"Ошибка {r.status_code}: {r.text[:500]}")
else:
    data = r.json()
    lines.append("кампания\tклики\tрасход_руб\tвизиты")
    for row in data.get("data", []):
        name = row["dimensions"][0].get("name")
        lines.append("\t".join([str(name)] + [str(round(v, 2)) for v in row["metrics"]]))
    lines.append("ИТОГО\t" + "\t".join(str(round(v, 2)) for v in data.get("totals", [])))

print("\n".join(lines))
with open("metrika_costs.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

# -*- coding: utf-8 -*-
"""
Яндекс Директ API v5: проверка авторизации, список запущенных кампаний
и статистика по ним за последние 30 дней.

Запуск:  pip install requests
         python direct_campaigns.py
Результат: вывод в консоль + файл direct_report.txt
Суммы — без НДС (как бюджеты в Директе).
"""
import json
import time
import requests

from yandex_token import yandex_token, crm_config
TOKEN = yandex_token()   # из настроек AWER CRM
CLIENT_LOGIN = crm_config().get("direct_client_login", "")   # из настроек CRM, если задан
SANDBOX = False          # True — песочница (если доступ к API ещё тестовый)

BASE = "https://api-sandbox.direct.yandex.com" if SANDBOX else "https://api.direct.yandex.com"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept-Language": "ru"}
if CLIENT_LOGIN:
    HEADERS["Client-Login"] = CLIENT_LOGIN

out = []
def log(s=""):
    print(s)
    out.append(str(s))


def call(service, method, params):
    r = requests.post(f"{BASE}/json/v5/{service}",
                      json={"method": method, "params": params}, headers=HEADERS, timeout=60)
    data = r.json()
    if "error" in data:
        e = data["error"]
        raise SystemExit(f"Ошибка {e.get('error_code')}: {e.get('error_string')} — {e.get('error_detail')}")
    log(f"[{service}.{method}] баллы: {r.headers.get('Units')}")
    return data["result"]


def money(v):
    return round(v / 1_000_000, 2) if v else 0


# 1. Кампании
res = call("campaigns", "get", {
    "SelectionCriteria": {},
    "FieldNames": ["Id", "Name", "Type", "State", "Status", "StatusPayment",
                   "StartDate", "DailyBudget", "Funds"],
    "TextCampaignFieldNames": ["BiddingStrategy"],
    "UnifiedCampaignFieldNames": ["BiddingStrategy"],
})
campaigns = res.get("Campaigns", [])
log(f"\nВсего кампаний: {len(campaigns)}")

running = [c for c in campaigns if c.get("State") == "ON"]
log(f"Запущено (State=ON): {len(running)}\n")

for c in campaigns:
    db = c.get("DailyBudget") or {}
    strat = (c.get("TextCampaign") or c.get("UnifiedCampaign") or {}).get("BiddingStrategy", {})
    log(f"{c['Id']} | {c['Name']} | {c['Type']} | {c['State']}/{c['Status']} | "
        f"старт {c.get('StartDate')} | дневной бюджет {money(db.get('Amount'))} | "
        f"стратегия поиск={strat.get('Search', {}).get('BiddingStrategyType')}, "
        f"сети={strat.get('Network', {}).get('BiddingStrategyType')}")

# 2. Статистика за 30 дней
body = {"params": {
    "SelectionCriteria": {},
    "FieldNames": ["CampaignId", "CampaignName", "Impressions", "Clicks", "Ctr",
                   "Cost", "AvgCpc", "BounceRate", "AvgImpressionPosition"],
    "ReportName": f"campaigns_30d_{int(time.time())}",
    "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
    "DateRangeType": "LAST_30_DAYS",
    "Format": "TSV",
    "IncludeVAT": "NO",
}}
rep_headers = dict(HEADERS, processingMode="auto", returnMoneyInMicros="false",
                   skipReportHeader="true", skipReportSummary="false")

log("\n=== Статистика за последние 30 дней (без НДС) ===")
for _ in range(120):   # не ждать бесконечно
    r = requests.post(f"{BASE}/json/v5/reports", json=body, headers=rep_headers, timeout=90)
    if r.status_code == 200:
        log(r.content.decode("utf-8"))
        break
    if r.status_code in (201, 202):
        time.sleep(int(r.headers.get("retryIn", 5)))
        continue
    log(f"Ошибка отчёта {r.status_code}: {r.text}")
    break
else:
    log("Отчёт не успел сформироваться за 120 попыток — запустите скрипт ещё раз позже.")

with open("direct_report.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("\nСохранено в direct_report.txt")

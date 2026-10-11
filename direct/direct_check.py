# -*- coding: utf-8 -*-
"""
Проверка кампаний Директа после изменений этапа 1:
статус, стратегия и недельный бюджет, запрещённые площадки, корректировки,
расход и клики по дням за последние 8 дней (идут ли показы после изменений).

Запуск: python direct_check.py  -> вывод + direct_check.txt
"""
import csv
import io
import json
import time
from pathlib import Path

import requests

from yandex_token import yandex_token, crm_config

CAMPAIGNS = {712220172: "ЕПК Сети КО", 712219913: "ЕПК Сети ТО"}
MUST_EXCLUDE = ["com.vkontakte.android", "com.vk.vkclient", "vk.com", "ok.ru", "ru.ok.android",
                "m.pogoda.yandex.ru", "pogoda.yandex.ru", "gismeteo.ru",
                "ru.yandex.mobile.weather", "ru.yandex.weatherplugin"]
REGIONS = {3: "ЦФО", 1: "Москва и МО", 213: "Москва", 17: "СЗФО", 10174: "СПб и ЛО", 40: "ПФО", 52: "УФО"}

cfg = crm_config()
HEADERS = {"Authorization": f"Bearer {yandex_token()}", "Accept-Language": "ru"}
if cfg.get("direct_client_login"):
    HEADERS["Client-Login"] = cfg["direct_client_login"]
API = "https://api.direct.yandex.com/json/v5/"
out = []


def log(s=""):
    print(s)
    out.append(str(s))


def call(service, method, params):
    r = requests.post(API + service, headers=HEADERS, json={"method": method, "params": params}, timeout=60)
    data = json.loads(r.content.decode("utf-8"))
    if "error" in data:
        e = data["error"]
        raise RuntimeError(f"{e.get('error_code')} {e.get('error_string')} — {e.get('error_detail')}")
    return data["result"]


cparams = {
    "SelectionCriteria": {"Ids": list(CAMPAIGNS)},
    "FieldNames": ["Id", "Name", "State", "Status", "StatusClarification", "ExcludedSites"],
    "TextCampaignFieldNames": ["BiddingStrategy", "PriorityGoals"],
}
try:
    camps = call("campaigns", "get", cparams).get("Campaigns", [])
except RuntimeError:
    cparams["TextCampaignFieldNames"] = ["BiddingStrategy"]
    camps = call("campaigns", "get", cparams).get("Campaigns", [])

mods = call("bidmodifiers", "get", {
    "SelectionCriteria": {"CampaignIds": list(CAMPAIGNS), "Levels": ["CAMPAIGN"]},
    "FieldNames": ["Id", "CampaignId", "Type"],
    "TabletAdjustmentFieldNames": ["BidModifier"],
    "RegionalAdjustmentFieldNames": ["RegionId", "BidModifier"],
    "DemographicsAdjustmentFieldNames": ["Age", "Gender", "BidModifier"],
    "DesktopOnlyAdjustmentFieldNames": ["BidModifier"],
}).get("BidModifiers", [])

for c in camps:
    cid = c["Id"]
    log(f"\n=== {CAMPAIGNS.get(cid, c['Name'])} ({cid}) ===")
    log(f"Состояние: {c.get('State')} / {c.get('Status')} — {c.get('StatusClarification')}")
    tc = c.get("TextCampaign") or {}
    strat = tc.get("BiddingStrategy") or {}
    for side in ("Search", "Network"):
        s = strat.get(side) or {}
        params = next((v for v in s.values() if isinstance(v, dict)), {}) or {}
        pretty = {k: (v / 1_000_000 if isinstance(v, int) and k in ("WeeklySpendLimit", "BidCeiling", "AverageCpa") else v)
                  for k, v in params.items()}
        log(f"Стратегия ({'поиск' if side == 'Search' else 'сети'}): {s.get('BiddingStrategyType')} {json.dumps(pretty, ensure_ascii=False)}")
    goals = ((tc.get("PriorityGoals") or {}).get("Items")) or []
    if goals:
        log("Ключевые цели: " + ", ".join(f"{g.get('GoalId')} (ценность {(g.get('Value') or 0) / 1_000_000:.0f} ₽)" for g in goals))
    excluded = ((c.get("ExcludedSites") or {}).get("Items")) or []
    missing = [s for s in MUST_EXCLUDE if s not in excluded]
    log(f"Запрещённых площадок: {len(excluded)}; из нашего списка " + ("все на месте ✓" if not missing else f"нет: {', '.join(missing)} ✗"))
    log("Корректировки:")
    for m in [m for m in mods if m.get("CampaignId") == cid]:
        t = m.get("Type")
        if t == "REGIONAL_ADJUSTMENT":
            r = m["RegionalAdjustment"]
            log(f"   регион {REGIONS.get(r['RegionId'], r['RegionId'])}: {r['BidModifier'] - 100:+d}%")
        elif t == "DEMOGRAPHICS_ADJUSTMENT":
            d = m["DemographicsAdjustment"]
            g = {"GENDER_MALE": "мужчины ", "GENDER_FEMALE": "женщины ", None: ""}[d.get("Gender")]
            log(f"   {g}{d['Age'].replace('AGE_', '').replace('_', '–')}: {d['BidModifier'] - 100:+d}%")
        elif t == "TABLET_ADJUSTMENT":
            log(f"   планшеты: {m['TabletAdjustment']['BidModifier'] - 100:+d}%")
        elif t == "DESKTOP_ONLY_ADJUSTMENT":
            log(f"   только десктопы: {m['DesktopOnlyAdjustment']['BidModifier'] - 100:+d}%")
        else:
            log(f"   {t}")


# Объявления: модерация и ссылки
try:
    ads = call("ads", "get", {"SelectionCriteria": {"CampaignIds": list(CAMPAIGNS)},
                              "FieldNames": ["Id", "AdGroupId", "CampaignId", "State", "Status", "StatusClarification"],
                              "TextAdFieldNames": ["Title", "Href"]}).get("Ads", [])
    log("\n=== Объявления ===")
    not_ok = [a for a in ads if a.get("Status") != "ACCEPTED" and a.get("State") != "ARCHIVED"]
    log(f"Всего: {len(ads)}; не прошли или на модерации: {len(not_ok)}")
    for a in not_ok:
        log(f"  {a['Id']} ({CAMPAIGNS.get(a['CampaignId'])}): {a.get('Status')} / {a.get('State')} — {a.get('StatusClarification')}")
    for a in ads:
        href = (a.get("TextAd") or {}).get("Href") or ""
        if "/collections/kurtki-awer" in href or "/collections/awer-kurtki-puhoviki" in href:
            log(f"  ✓ {a['Id']} ведёт на {href.split('?')[0]} — {a.get('Status')} / {a.get('State')}")
except RuntimeError as e:
    log(f"Объявления не получены: {e}")

# Расход по дням
def report(range_type):
    # только две кампании из заголовка отчёта (CAMPAIGNS), а не весь кабинет
    flt = [{"Field": "CampaignId", "Operator": "IN", "Values": [str(c) for c in CAMPAIGNS]}]
    body = {"params": {"SelectionCriteria": {"Filter": flt}, "FieldNames": ["Date", "CampaignName", "Impressions", "Clicks", "Cost"],
                       "ReportName": f"check_{range_type}_{int(time.time() * 1000)}", "ReportType": "CUSTOM_REPORT",
                       "DateRangeType": range_type, "Format": "TSV", "IncludeVAT": "NO"}}
    h = HEADERS | {"processingMode": "auto", "returnMoneyInMicros": "false",
                   "skipReportHeader": "true", "skipReportSummary": "true"}
    for _ in range(60):
        r = requests.post(API + "reports", json=body, headers=h, timeout=60)
        if r.status_code == 200:
            return list(csv.DictReader(io.StringIO(r.content.decode("utf-8")), delimiter="\t"))
        if r.status_code in (201, 202):
            time.sleep(int(r.headers.get("retryIn", 5)))
            continue
        log(f"Отчёт не получен: {r.status_code} {r.content.decode('utf-8', 'replace')[:300]}")
        return None
    return None


rows = (report("LAST_7_DAYS") or []) + (report("TODAY") or [])
if rows:
    days = {}
    for row in rows:
        d = days.setdefault(row["Date"], [0, 0, 0.0])
        d[0] += int(row["Impressions"]); d[1] += int(row["Clicks"]); d[2] += float(row["Cost"])
    log("\n=== По дням, обе кампании (без НДС); последняя строка — сегодня, неполный день ===")
    log("дата\tпоказы\tклики\tрасход")
    for day in sorted(days):
        i, cl, cost = days[day]
        log(f"{day}\t{i}\t{cl}\t{cost:.0f}")

Path("direct_check.txt").write_text("\n".join(out), encoding="utf-8")
print("\nСохранено в direct_check.txt")

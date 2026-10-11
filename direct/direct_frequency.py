# -*- coding: utf-8 -*-
"""
Сколько раз реклама Директа показывается одному человеку.
Охват (уникальные пользователи) и средняя частота показов — по кампаниям,
по неделям и по площадкам за последние 30 дней.

Токен берётся из настроек AWER CRM (раздел «Маркетинг»), вводить не нужно.
Запуск: python direct_frequency.py  -> вывод + direct_frequency.txt
"""
import csv
import io
import json
import time
from pathlib import Path

import requests

cfg = json.loads((Path.home() / "AWER CRM" / "config.json").read_text(encoding="utf-8"))
TOKEN = cfg.get("yandex_oauth_token", "")
LOGIN = cfg.get("direct_client_login", "")
if not TOKEN:
    raise SystemExit("В CRM не сохранён токен Яндекса (Маркетинг → Подключение к Яндексу).")

URL = "https://api.direct.yandex.com/json/v5/reports"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept-Language": "ru", "processingMode": "auto",
           "returnMoneyInMicros": "false", "skipReportHeader": "true", "skipReportSummary": "true"}
if LOGIN:
    HEADERS["Client-Login"] = LOGIN

out = []
def log(s=""):
    print(s)
    out.append(str(s))


def report(name, fields, rtype="REACH_AND_FREQUENCY_PERFORMANCE_REPORT"):
    # Охват и частота в CUSTOM_REPORT недоступны (Директ отвечает ошибкой 4000),
    # для них есть отдельный тип отчёта REACH_AND_FREQUENCY_PERFORMANCE_REPORT.
    body = {"params": {"SelectionCriteria": {}, "FieldNames": fields,
                       "ReportName": f"freq_{name}_{int(time.time() * 1000)}", "ReportType": rtype,
                       "DateRangeType": "LAST_30_DAYS", "Format": "TSV", "IncludeVAT": "YES"}}
    for _ in range(90):
        r = requests.post(URL, json=body, headers=HEADERS)
        if r.status_code == 200:
            return list(csv.DictReader(io.StringIO(r.content.decode("utf-8")), delimiter="\t"))
        if r.status_code in (201, 202):
            time.sleep(int(r.headers.get("retryIn", 5)))
            continue
        log(f"[{name}] Директ не отдал отчёт: HTTP {r.status_code} {r.content.decode('utf-8', 'replace')[:400]}")
        return None
    return None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def show(title, rows, key, top=None):
    log(f"\n=== {title} ===")
    log("строка\tпоказы\tохват (люди)\tсредняя частота\tклики")
    rows = sorted(rows, key=lambda r: -num(r.get("Impressions")))
    for r in rows[:top] if top else rows:
        log(f"{r.get(key)}\t{num(r.get('Impressions')):.0f}\t{num(r.get('ImpressionReach')):.0f}\t"
            f"{num(r.get('AvgImpressionFrequency')):.2f}\t{num(r.get('Clicks')):.0f}")


base = ["CampaignId", "Impressions", "ImpressionReach", "AvgImpressionFrequency", "Clicks"]

camp = report("campaigns", ["CampaignName"] + base)
if camp is not None and not camp:
    log("\nОтчёт об охвате и частоте пустой: Директ считает их только для охватных (медийных)")
    log("кампаний, а у вас единые перфоманс-кампании. Частоту по ним через API не получить.")
elif camp is None:
    log("\nПохоже, для этих кампаний Директ не считает охват и частоту через API.")
    log("Тогда смотрите в интерфейсе: Мастер отчётов → столбцы «Охват» и «Средняя частота показов».")
else:
    show("Кампании за 30 дней", camp, "CampaignName")
    weeks = report("weeks", ["Week", "CampaignName"] + base)
    if weeks:
        show("По неделям", sorted(weeks, key=lambda r: (r["Week"], r["CampaignName"])), "Week")
    pl = report("placements", ["Placement", "AdNetworkType"] + base)
    if pl:
        show("Площадки РСЯ — топ-40 по показам", [r for r in pl if r.get("AdNetworkType") == "AD_NETWORK"],
             "Placement", top=40)
        high = [r for r in pl if num(r.get("AvgImpressionFrequency")) >= 5 and num(r.get("Impressions")) >= 500]
        if high:
            log("\nПлощадки, где один человек в среднем видит рекламу 5+ раз за 30 дней:")
            for r in sorted(high, key=lambda r: -num(r.get("AvgImpressionFrequency"))):
                log(f"  {r['Placement']}\tчастота {num(r['AvgImpressionFrequency']):.1f}\t"
                    f"охват {num(r['ImpressionReach']):.0f}\tпоказы {num(r['Impressions']):.0f}")

with open("direct_frequency.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("\nСохранено в direct_frequency.txt")

# -*- coding: utf-8 -*-
"""
Директ + Метрика: «до» и «после» изменений этапа 1 (применены 06.10.2026 ~01:00).
  До:    29.09 – 05.10 (7 дней)
  После: 06.10 – вчера

Что показывает:
  1. Итоги в среднем за день: расход, показы, клики, цена клика, визиты, покупки,
     выручка, конверсия, доля рекламы и цена покупки в реальных деньгах (с учётом соинвеста).
  2. Каждая кампания: расход в день против недельного бюджета / 7, покупки, доля рекламы.
  3. Разбивка по дням.
  4. Сработали ли корректировки: распределение расхода по возрасту, устройствам,
     регионам и площадкам (отключённые ВК/ОК/погода должны уйти в ноль).
  5. Можно ли уже доверять разнице в покупках.

Суммы без НДС (так же, как бюджеты в Директе). Покупки и выручка — из Метрики
(оформленные заказы с рекламы, последний значимый переход) — только переходы из кампаний КО и ТО,
тех же, по которым считается расход. Покупки с другой рекламы показываются отдельной строкой.
Токен, счётчик и коэффициент соинвеста берутся из настроек AWER CRM (yandex_token.py).

Запуск: python direct_before_after.py   -> вывод + direct_before_after.txt
"""
import csv
import datetime as dt
import io
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

from yandex_token import crm_config

BEFORE = ("2026-09-29", "2026-10-05")
AFTER_FROM = "2026-10-06"
AFTER_TO = (dt.date.today() - dt.timedelta(days=1)).isoformat()
# Можно указать другое начало периода «после»: python direct_before_after.py 2026-10-10
for _arg in sys.argv[1:]:
    try:
        AFTER_FROM = dt.date.fromisoformat(_arg).isoformat()
    except ValueError:
        pass

CAMPAIGNS = {"712220172": "КО", "712219913": "ТО"}
WEEKLY_OLD = {"712220172": 47500, "712219913": 37500}
WEEKLY_NEW = {"712220172": 38000, "712219913": 32000}
EXCLUDED = {"com.vkontakte.android", "com.vk.vkclient", "vk.com", "ok.ru", "ru.ok.android",
            "m.pogoda.yandex.ru", "pogoda.yandex.ru", "gismeteo.ru", "ru.yandex.mobile.weather",
            "ru.yandex.weatherplugin"}
AGE_NAMES = {"AGE_0_17": "до 18", "AGE_18_24": "18–24", "AGE_25_34": "25–34", "AGE_35_44": "35–44",
             "AGE_45_54": "45–54", "AGE_55": "55+", "UNKNOWN": "не определён"}
DEVICE_NAMES = {"DESKTOP": "компьютеры", "MOBILE": "смартфоны", "TABLET": "планшеты", "SMART_TV": "Smart TV"}

# Токен в код не вписывать: он берётся из настроек AWER CRM или переменной окружения YANDEX_OAUTH_TOKEN.
cfg = crm_config()
TOKEN = os.environ.get("YANDEX_OAUTH_TOKEN") or cfg.get("yandex_oauth_token") or ""
COUNTER = str(cfg.get("metrika_counter_id") or "110246968")
MULT = float(cfg.get("coinvest_multiplier") or 2.0)
CLIENT_LOGIN = cfg.get("direct_client_login") or ""

DH = {"Authorization": f"Bearer {TOKEN}", "Accept-Language": "ru", "processingMode": "auto",
      "returnMoneyInMicros": "false", "skipReportHeader": "true", "skipReportSummary": "true"}
if CLIENT_LOGIN:
    DH["Client-Login"] = CLIENT_LOGIN
MH = {"Authorization": f"OAuth {TOKEN}"}

out = []


def log(s=""):
    print(s)
    out.append(str(s))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fm(v, d=0):
    if v is None:
        return "—"
    return f"{v:,.{d}f}".replace(",", " ")


def ch(b, a):
    if not b or a is None:
        return ""
    return f"{(a / b - 1) * 100:+.0f}%"


def pp(b, a):
    if b is None or a is None:
        return ""
    return f"{a - b:+.1f} п.п."


def period_of(date):
    if BEFORE[0] <= date <= BEFORE[1]:
        return "b"
    if AFTER_FROM <= date <= AFTER_TO:
        return "a"
    return None


def ndays(d1, d2):
    return (dt.date.fromisoformat(d2) - dt.date.fromisoformat(d1)).days + 1


ND = {"b": ndays(*BEFORE), "a": ndays(AFTER_FROM, AFTER_TO)}


# ---------- загрузка данных ----------

def direct(fields, name):
    """Отчёт Директа по дням за весь интервал (до + после), только две кампании."""
    body = {"params": {
        "SelectionCriteria": {"DateFrom": BEFORE[0], "DateTo": AFTER_TO,
                              "Filter": [{"Field": "CampaignId", "Operator": "IN",
                                          "Values": list(CAMPAIGNS)}]},
        "FieldNames": ["Date"] + fields,
        "ReportName": f"ba_{name}_{int(time.time() * 1000)}",
        "ReportType": "CUSTOM_REPORT", "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV", "IncludeVAT": "NO"}}
    for _ in range(120):
        r = requests.post("https://api.direct.yandex.com/json/v5/reports", json=body, headers=DH, timeout=90)
        if r.status_code == 200:
            text = r.content.decode("utf-8")
            return list(csv.DictReader(io.StringIO(text), delimiter="\t"))
        if r.status_code in (201, 202):
            time.sleep(int(r.headers.get("retryIn", 5)))
            continue
        log(f"[Директ, отчёт {name}] ошибка {r.status_code}: {r.content.decode('utf-8', 'replace')[:400]}")
        return []
    log(f"[Директ, отчёт {name}] не дождались готовности отчёта")
    return []


def campaign_names():
    """Точные названия КО и ТО из Директа: {название: "КО"/"ТО"}. Пусто — если Директ не ответил."""
    h = {k: v for k, v in DH.items() if k in ("Authorization", "Accept-Language", "Client-Login")}
    try:
        r = requests.post("https://api.direct.yandex.com/json/v5/campaigns", headers=h, timeout=60,
                          json={"method": "get", "params": {"SelectionCriteria": {"Ids": [int(c) for c in CAMPAIGNS]},
                                                            "FieldNames": ["Id", "Name"]}})
        camps = r.json().get("result", {}).get("Campaigns", [])
    except (requests.RequestException, ValueError) as e:
        log(f"[Директ, названия кампаний] ошибка: {e}")
        return {}
    return {c["Name"]: CAMPAIGNS[str(c["Id"])] for c in camps if str(c.get("Id")) in CAMPAIGNS and c.get("Name")}


NAMES = {}   # заполняется в main() из Директа


def short_campaign(name):
    if not name:
        return "без кампании"
    if NAMES:
        return NAMES.get(name.strip(), "другое")
    # Директ не отдал названия — угадываем по слову в названии (менее надёжно).
    for tag in ("КО", "ТО"):
        if f"- {tag} -" in name or f" {tag} " in f" {name} ":
            return tag
    return "другое"


def metrika():
    """Визиты, покупки, выручка с рекламы по дням (и по кампаниям, если Метрика отдаст)."""
    base = {"ids": COUNTER, "date1": BEFORE[0], "date2": AFTER_TO,
            "metrics": "ym:s:visits,ym:s:ecommercePurchases,ym:s:ecommerceRevenue",
            "filters": "ym:s:lastsignTrafficSource=='ad'", "accuracy": "full", "lang": "ru", "limit": 10000}
    for dims in ("ym:s:date,ym:s:lastsignDirectClickOrderName", "ym:s:date"):
        r = requests.get("https://api-metrika.yandex.net/stat/v1/data", headers=MH,
                         params=dict(base, dimensions=dims), timeout=90)
        if r.status_code != 200:
            log(f"[Метрика, {dims}] ошибка {r.status_code}: {r.text[:300]}")
            continue
        rows = []
        if len(dims.split(",")) == 1:
            log("ВНИМАНИЕ: Метрика не дала разбивку по кампаниям — покупки и выручка ниже по ВСЕЙ рекламе, "
                "а расход только по КО и ТО. Доля рекламы и цена покупки получатся лучше настоящих.")
        for row in r.json().get("data", []):
            d = row["dimensions"]
            date = d[0].get("name")
            camp = short_campaign(d[1].get("name")) if len(d) > 1 else "все"
            v, p, rev = row["metrics"]
            rows.append({"date": date, "camp": camp, "visits": v, "purch": p, "rev": rev})
        return rows
    return []


# ---------- расчёты ----------

def blank():
    return defaultdict(float)


def build(daily, mrows):
    tot = {"b": blank(), "a": blank()}
    camp = {"b": defaultdict(blank), "a": defaultdict(blank)}
    day = defaultdict(blank)
    for r in daily:
        p = period_of(r["Date"])
        cid = CAMPAIGNS.get(str(r.get("CampaignId")), "другое")
        for k_src, k in (("Cost", "cost"), ("Clicks", "clicks"), ("Impressions", "imp")):
            v = num(r.get(k_src))
            day[r["Date"]][k] += v
            if p:
                tot[p][k] += v
                camp[p][cid][k] += v
    ours = set(CAMPAIGNS.values()) | {"все"}   # «все» — только если Метрика не дала разбивку
    for r in mrows:
        p = period_of(r["date"])
        if r["camp"] in ours:
            for k in ("visits", "purch", "rev"):
                day[r["date"]][k] += r[k]
                if p:
                    tot[p][k] += r[k]
        if p and r["camp"] != "все":
            for k in ("visits", "purch", "rev"):
                camp[p][r["camp"]][k] += r[k]
    return tot, camp, day


def per_day(t, p):
    n = ND[p]
    cost, clicks, visits, purch, rev = t["cost"], t["clicks"], t["visits"], t["purch"], t["rev"]
    real = cost / MULT
    return {
        "cost": cost / n, "imp": t["imp"] / n, "clicks": clicks / n,
        "cpc": cost / clicks if clicks else None,
        "visits": visits / n, "purch": purch / n, "rev": rev / n,
        "cr": purch / visits * 100 if visits else None,
        "aov": rev / purch if purch else None,
        "drr": real / rev * 100 if rev else None,
        "cpa": real / purch if purch else None,
    }


def shares(rows, keyfn):
    res = {"b": defaultdict(float), "a": defaultdict(float)}
    for r in rows:
        p = period_of(r["Date"])
        if p:
            res[p][keyfn(r)] += num(r.get("Cost"))
    pct = {}
    for p in res:
        total = sum(res[p].values())
        pct[p] = {k: v / total * 100 for k, v in res[p].items()} if total else {}
    return pct


def dist_table(title, pct, top=None, order=None):
    log(f"\n{title}")
    keys = set(pct["b"]) | set(pct["a"])
    if order:
        keys = [k for k in order if k in keys] + sorted(k for k in keys if k not in order)
    else:
        keys = sorted(keys, key=lambda k: -(pct["a"].get(k, 0) + pct["b"].get(k, 0)))
    if top:
        keys = keys[:top]
    log(f"  {'':28}{'до':>9}{'после':>9}{'разница':>12}")
    for k in keys:
        b, a = pct["b"].get(k, 0.0), pct["a"].get(k, 0.0)
        log(f"  {str(k)[:28]:28}{fm(b, 1) + '%':>9}{fm(a, 1) + '%':>9}{pp(b, a):>12}")


def main():
    if not TOKEN:
        sys.exit("Нет токена Яндекса: сохраните его в AWER CRM (Маркетинг → Подключение к Яндексу) "
                 "или задайте переменную окружения YANDEX_OAUTH_TOKEN. В код токен не вписывать.")
    if AFTER_TO < AFTER_FROM:
        sys.exit("После изменений ещё не прошло ни одного полного дня.")

    log(f"Сравнение «до» {BEFORE[0]} – {BEFORE[1]} ({ND['b']} дн.) и «после» {AFTER_FROM} – {AFTER_TO} ({ND['a']} дн.)")
    log(f"Суммы без НДС. Реальные деньги = расход кабинета ÷ {MULT:g} (соинвест).")
    log("Загружаю отчёты Директа (обычно 1–3 минуты)...")

    daily = direct(["CampaignId", "Impressions", "Clicks", "Cost"], "daily")
    age = direct(["Age", "Cost"], "age")
    dev = direct(["Device", "Cost"], "device")
    geo = direct(["TargetingLocationName", "Cost"], "geo")
    pl = direct(["Placement", "Cost"], "placement")
    NAMES.update(campaign_names())
    if not NAMES:
        log("ВНИМАНИЕ: Директ не отдал названия кампаний — КО и ТО в Метрике определяю по слову в названии.")
    mrows = metrika()

    tot, camp, day = build(daily, mrows)
    b, a = per_day(tot["b"], "b"), per_day(tot["a"], "a")

    # 1. Итоги
    log("\n=== 1. Итоги — в среднем за день ===")
    log(f"{'Показатель':36}{'до':>12}{'после':>12}{'изменение':>12}")
    rows = [
        ("Расход кабинета, ₽", "cost", 0, "rel"),
        ("Показы", "imp", 0, "rel"),
        ("Клики", "clicks", 0, "rel"),
        ("Цена клика, ₽", "cpc", 1, "rel"),
        ("Визиты с рекламы", "visits", 0, "rel"),
        ("Покупки (оформлено)", "purch", 2, "rel"),
        ("Выручка (оформлено), ₽", "rev", 0, "rel"),
        ("Средний чек, ₽", "aov", 0, "rel"),
        ("Конверсия визита в покупку, %", "cr", 2, "pp"),
        ("Доля рекламы в реальных деньгах, %", "drr", 1, "pp"),
        ("Цена покупки в реальных деньгах, ₽", "cpa", 0, "rel"),
    ]
    for title, k, d, kind in rows:
        diff = ch(b[k], a[k]) if kind == "rel" else pp(b[k], a[k])
        log(f"{title:36}{fm(b[k], d):>12}{fm(a[k], d):>12}{diff:>12}")
    log(f"Всего покупок: до {tot['b']['purch']:.0f} за {ND['b']} дн., после {tot['a']['purch']:.0f} за {ND['a']} дн.")

    # 2. Кампании
    log("\n=== 2. По кампаниям — в среднем за день ===")
    log(f"{'':6}{'расход до':>11}{'бюджет/7':>10}{'расход после':>14}{'бюджет/7':>10}"
        f"{'покупки до':>12}{'после':>8}{'доля рекл. до':>15}{'после':>8}")
    for cid, nm in CAMPAIGNS.items():
        cb, ca = camp["b"].get(nm, blank()), camp["a"].get(nm, blank())
        drr_b = cb["cost"] / MULT / cb["rev"] * 100 if cb["rev"] else None
        drr_a = ca["cost"] / MULT / ca["rev"] * 100 if ca["rev"] else None
        log(f"{nm:6}{fm(cb['cost'] / ND['b']):>11}{fm(WEEKLY_OLD[cid] / 7):>10}"
            f"{fm(ca['cost'] / ND['a']):>14}{fm(WEEKLY_NEW[cid] / 7):>10}"
            f"{fm(cb['purch'] / ND['b'], 2):>12}{fm(ca['purch'] / ND['a'], 2):>8}"
            f"{(fm(drr_b, 1) + '%') if drr_b else '—':>15}{(fm(drr_a, 1) + '%') if drr_a else '—':>8}")
    for p, title in (("b", "до"), ("a", "после")):
        for nm, v in camp[p].items():
            if nm not in CAMPAIGNS.values() and v.get("purch"):
                log(f"  Не входят в итоги — покупки с другой рекламы {title} изменений ({nm}): "
                    f"{v['purch']:.0f} на {fm(v['rev'])} ₽")

    # 3. По дням
    log("\n=== 3. По дням ===")
    log(f"{'дата':12}{'расход':>9}{'клики':>7}{'цена клика':>12}{'визиты':>8}{'покупки':>9}{'выручка':>10}{'доля рекл.':>12}")
    for d in sorted(day):
        if not period_of(d):
            continue
        v = day[d]
        cpc = v["cost"] / v["clicks"] if v["clicks"] else None
        drr = v["cost"] / MULT / v["rev"] * 100 if v["rev"] else None
        mark = "  ← изменения" if d == AFTER_FROM else ""
        log(f"{d:12}{fm(v['cost']):>9}{fm(v['clicks']):>7}{fm(cpc, 1):>12}{fm(v['visits']):>8}"
            f"{fm(v['purch']):>9}{fm(v['rev']):>10}{(fm(drr, 1) + '%') if drr else '—':>12}{mark}")

    # 4. Корректировки
    log("\n=== 4. Сработали ли корректировки — доля расхода ===")
    dist_table("Возраст (до 24 лет должно уйти в ноль, 45–54 — заметно снизиться):",
               shares(age, lambda r: AGE_NAMES.get(r["Age"], r["Age"])), order=list(AGE_NAMES.values()))
    dist_table("Устройства (планшеты должны уйти в ноль):",
               shares(dev, lambda r: DEVICE_NAMES.get(r["Device"], r["Device"])), order=list(DEVICE_NAMES.values()))
    dist_table("Регионы (топ-10):", shares(geo, lambda r: r.get("TargetingLocationName") or "—"), top=10)
    pl_pct = shares(pl, lambda r: "ОТКЛЮЧЁННЫЕ: ВК/ОК/погода" if r.get("Placement") in EXCLUDED
                    else (r.get("Placement") if r.get("Placement") not in (None, "", "--") else "площадка не указана"))
    dist_table("Площадки (топ-12; отключённые ВК/ОК/погода должны уйти в ноль):", pl_pct, top=12)

    # 5. Достоверность
    log("\n=== 5. Можно ли уже доверять разнице в покупках ===")
    pb, pa = tot["b"]["purch"], tot["a"]["purch"]
    if tot["b"]["cost"] and pb:
        exp = pb / tot["b"]["cost"] * tot["a"]["cost"]
        lo, hi = max(0.0, exp - 2 * math.sqrt(exp)), exp + 2 * math.sqrt(exp)
        log(f"Если бы реклама работала как до изменений, на расход «после» пришлось бы ≈{exp:.0f} покупок")
        log(f"(обычный случайный разброс — примерно от {lo:.0f} до {hi:.0f}). По факту: {pa:.0f}.")
        if pa > hi:
            log("Покупок заметно больше, чем при прежней отдаче, — улучшение похоже на настоящее.")
        elif pa < lo:
            log("Покупок заметно меньше, чем при прежней отдаче, — стоит разобраться.")
        else:
            log("Разница пока в пределах случайных колебаний — выводы по покупкам делать рано, "
                "смотрим на цену клика, структуру расхода и визиты.")
    else:
        log("Мало данных для оценки.")

    Path("direct_before_after.txt").write_text("\n".join(out), encoding="utf-8")
    print("\nСохранено в direct_before_after.txt")


if __name__ == "__main__":
    main()

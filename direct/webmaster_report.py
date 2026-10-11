# -*- coding: utf-8 -*-
"""
Яндекс Вебмастер API v4: статус сайта, индексация, проблемы и поисковые запросы.
Запуск: python webmaster_report.py  -> вывод + webmaster_report.txt
"""
import datetime as dt
import requests

from yandex_token import yandex_token
TOKEN = yandex_token()   # из настроек AWER CRM
SITE = "awer-russia.ru"
DAYS = 30

API = "https://api.webmaster.yandex.net/v4"
HEADERS = {"Authorization": f"OAuth {TOKEN}"}

out = []
def log(s=""):
    print(s)
    out.append(str(s))


def get(path, params=None):
    r = requests.get(API + path, headers=HEADERS, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def save():
    with open("webmaster_report.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("\nСохранено в webmaster_report.txt")


# 1. Пользователь и сайты
uid = get("/user")["user_id"]
hosts = get(f"/user/{uid}/hosts").get("hosts", [])
log(f"Сайтов в Вебмастере: {len(hosts)}")
for h in hosts:
    log(f"  {h['host_id']} | {h.get('unicode_host_url')} | подтверждён: {h.get('verified')}")

host = next((h for h in hosts if SITE in (h.get("ascii_host_url") or "")), None)
if not host:
    log(f"\nСайт {SITE} не добавлен в Вебмастер этого аккаунта. Добавьте и подтвердите его на webmaster.yandex.ru.")
    save(); raise SystemExit
if not host.get("verified"):
    log(f"\nСайт {SITE} добавлен, но права не подтверждены. Подтвердите их на webmaster.yandex.ru.")
    save(); raise SystemExit

hid = host["host_id"]
base = f"/user/{uid}/hosts/{hid}"

# 2. Сводка
try:
    s = get(base + "/summary")
    log(f"\n=== Сводка ===")
    log(f"ИКС: {s.get('sqi')}")
    log(f"Страниц в поиске: {s.get('searchable_pages_count')}")
    log(f"Исключено страниц: {s.get('excluded_pages_count')}")
    log(f"Проблемы по важности: {s.get('site_problems')}")
except RuntimeError as e:
    log(f"Сводка недоступна: {e}")

# 3. Диагностика
try:
    d = get(base + "/diagnostics").get("problems", {})
    log("\n=== Диагностика (только актуальные проблемы) ===")
    found = False
    for name, p in d.items():
        if p.get("state") == "PRESENT":
            found = True
            log(f"  [{p.get('severity')}] {name}")
    if not found:
        log("  Актуальных проблем нет")
except RuntimeError as e:
    log(f"Диагностика недоступна: {e}")

# 4. Sitemap
try:
    sm = get(base + "/sitemaps").get("sitemaps", [])
    log(f"\n=== Sitemap: {len(sm)} ===")
    for m in sm:
        log(f"  {m.get('sitemap_url')} | URL: {m.get('urls_count')} | ошибок: {m.get('errors_count')}")
except RuntimeError as e:
    log(f"Sitemap недоступны: {e}")

# 5. Популярные поисковые запросы
date_to = dt.date.today() - dt.timedelta(days=1)
date_from = date_to - dt.timedelta(days=DAYS - 1)
params = [("order_by", "TOTAL_SHOWS"),
          ("query_indicator", "TOTAL_SHOWS"), ("query_indicator", "TOTAL_CLICKS"),
          ("query_indicator", "AVG_SHOW_POSITION"), ("query_indicator", "AVG_CLICK_POSITION"),
          ("date_from", date_from.isoformat()), ("date_to", date_to.isoformat()), ("limit", 500)]
try:
    q = get(base + "/search-queries/popular", params)
    rows = q.get("queries", [])
    log(f"\n=== Поисковые запросы {date_from} — {date_to}: {len(rows)} ===")
    log("запрос\tпоказы\tклики\tCTR,%\tср.позиция показа\tср.позиция клика")
    for r in rows:
        i = r.get("indicators", {})
        sh, cl = i.get("TOTAL_SHOWS") or 0, i.get("TOTAL_CLICKS") or 0
        ctr = round(cl / sh * 100, 1) if sh else 0
        pos = lambda v: round(v, 1) if v else "-"
        log(f"{r.get('query_text')}\t{int(sh)}\t{int(cl)}\t{ctr}\t"
            f"{pos(i.get('AVG_SHOW_POSITION'))}\t{pos(i.get('AVG_CLICK_POSITION'))}")
except RuntimeError as e:
    log(f"Запросы недоступны: {e}")

save()

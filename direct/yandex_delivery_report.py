# -*- coding: utf-8 -*-
"""
Яндекс Доставка (Доставка по России, API other-day): все заявки за период,
статусы, причины невыкупа и сводка по выкупу.

Запуск:  python yandex_delivery_report.py
Результат: delivery_orders.csv (открывается в Excel) + delivery_report.txt
"""
import csv
import datetime as dt
import time
from collections import Counter, defaultdict

import requests

from yandex_token import delivery_token
TOKEN = delivery_token()   # из yandex_delivery_token.txt
DATE_FROM = "2026-07-01"                    # с какой даты создания заявок выгружать
DATE_TO = None                              # None = по сегодня
CHUNK_DAYS = 7                              # шаг выгрузки

API = "https://b2b-authproxy.taxi.yandex.net/api/b2b/platform"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept-Language": "ru"}

REASONS = {
    "PICKUP_EXPIRED": "Не забрал из ПВЗ (истёк срок хранения)",
    "USER_CHANGED_MIND": "Получатель передумал / отказ",
    "SHOP_CANCELLED": "Отменён магазином",
    "DELIVERY_PROBLEMS": "Проблемы при доставке",
    "BROKEN_ITEM": "Брак",
    "ORDER_WAS_LOST": "Утерян",
    "DIMENSIONS_EXCEEDED": "Превышены габариты",
    "ORDER_IS_DAMAGED": "Повреждён",
    "EXTRA_RESCHEDULING": "Частые переносы клиентом",
}
PAY = {"already_paid": "Предоплата", "card_on_receipt": "При получении", "postpay": "Постоплата по ссылке"}
DONE_OK = {"DELIVERY_DELIVERED", "PARTICULARLY_DELIVERED"}
DONE_BAD = {"CANCELLED", "SORTING_CENTER_RETURN_RETURNED", "RETURN_TRANSPORTATION_STARTED",
            "RETURN_ARRIVED_DELIVERY", "RETURN_READY_FOR_PICKUP", "RETURN_RETURNED"}
# Известные промежуточные статусы (заявка ещё в работе). Список собран по документации other-day
# по памяти — всё, чего здесь нет, попадёт в «прочие/неизвестные статусы» и будет показано в отчёте.
IN_TRANSIT = {"DRAFT", "VALIDATING", "CREATED", "DELIVERY_PROCESSING_STARTED", "DELIVERY_TRACK_RECIEVED",
              "SORTING_CENTER_PROCESSING_STARTED", "SORTING_CENTER_TRACK_RECEIVED", "SORTING_CENTER_TRACK_LOADED",
              "DELIVERY_LOADED", "SORTING_CENTER_LOADED", "SORTING_CENTER_AT_START", "SORTING_CENTER_PREPARED",
              "SORTING_CENTER_TRANSMITTED", "DELIVERY_AT_START", "DELIVERY_AT_START_SORT", "DELIVERY_TRANSPORTATION",
              "DELIVERY_ARRIVED_PICKUP_POINT", "DELIVERY_TRANSMITTED_TO_RECIPIENT", "DELIVERY_STORAGE_PERIOD_EXTENDED",
              "DELIVERY_STORAGE_PERIOD_EXPIRED", "DELIVERY_UPDATED_BY_SHOP", "DELIVERY_UPDATED_BY_RECIPIENT",
              "DELIVERY_UPDATED_BY_DELIVERY", "DELIVERY_ATTEMPT_FAILED", "CONFIRMATION_CODE_RECEIVED",
              "SORTING_CENTER_RETURN_PREPARING", "SORTING_CENTER_RETURN_PREPARING_SENDER",
              "SORTING_CENTER_RETURN_ARRIVED", "SORTING_CENTER_RETURN_TRANSMITTED", "RETURN_PREPARING"}
UNKNOWN = "Прочие/неизвестные статусы"

out = []
def log(s=""):
    print(s)
    out.append(str(s))


def iso(d):
    # Даты в скрипте — московское время (полночь по Москве), а не UTC. Суффикс «Z» означал бы UTC
    # и сдвигал бы границы окон на 3 часа, поэтому указываем смещение Москвы +03:00.
    return d.strftime("%Y-%m-%dT%H:%M:%S.000000+03:00")


def post(path, body):
    r = requests.post(f"{API}/{path}", headers=HEADERS, json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def get(path, params):
    for attempt in range(3):
        r = requests.get(f"{API}/{path}", headers=HEADERS, params=params, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(2 + attempt * 3)
            continue
        raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:300]}")
    raise RuntimeError(f"{path}: слишком много запросов")


def ts(state):
    return dt.datetime.fromtimestamp(state["timestamp"]) if state.get("timestamp") else None


# ---------- 1. Выгрузка заявок ----------
start = dt.datetime.fromisoformat(DATE_FROM)
end = dt.datetime.fromisoformat(DATE_TO) if DATE_TO else dt.datetime.utcnow() + dt.timedelta(hours=3)   # «сейчас» по Москве
all_req = {}
failed = []   # окна, которые не удалось выгрузить
cur = start
while cur < end:
    nxt = min(cur + dt.timedelta(days=CHUNK_DAYS), end)
    try:
        data = post("requests/info", {"from": iso(cur), "to": iso(nxt)})
        for r in data.get("requests", []):
            all_req[r["request_id"]] = r
        print(f"{cur:%d.%m} — {nxt:%d.%m}: {len(data.get('requests', []))} заявок")
    except RuntimeError as e:
        print(f"{cur:%d.%m} — {nxt:%d.%m}: ошибка — {e}")
        failed.append(f"{cur:%d.%m.%Y} — {nxt:%d.%m.%Y}: {e}")
    cur = nxt

log(f"Всего заявок за период: {len(all_req)}")
if failed:
    log(f"\n!!! ВНИМАНИЕ: не выгружено окон — {len(failed)}. Данные неполные, % выкупа и суммы ниже")
    log("!!! посчитаны без заявок за эти периоды. Перезапустите скрипт позже. Не выгружены:")
    for w in failed:
        log(f"!!!   {w}")
if not all_req:
    raise SystemExit("Заявок нет — проверьте токен и период.")

# ---------- 2. История статусов и классификация ----------
rows = []
for i, (rid, r) in enumerate(all_req.items(), 1):
    req = r.get("request", {})
    state = r.get("state", {})
    status = state.get("status", "")
    items = req.get("items", []) or []
    amount = sum((it.get("billing_details", {}).get("unit_price", 0) or 0) * (it.get("count", 0) or 0)
                 for it in items) / 100
    # выкупленная часть: count − refused_count (поле отказанных единиц — то же, что ниже в «отказано_единиц»)
    amount_bought = sum((it.get("billing_details", {}).get("unit_price", 0) or 0)
                        * max((it.get("count", 0) or 0) - (it.get("refused_count", 0) or 0), 0)
                        for it in items) / 100
    units = sum(it.get("count", 0) or 0 for it in items)
    refused = sum(it.get("refused_count", 0) or 0 for it in items)
    fitting = any(it.get("fitting") for it in items)

    hist = []
    try:
        hist = get("request/history", {"request_id": rid}).get("state_history", [])
        time.sleep(0.15)
    except RuntimeError as e:
        print(f"  история {rid}: {e}")

    created = next((ts(h) for h in hist if h.get("status") == "CREATED"), None)
    arrived = next((ts(h) for h in hist if h.get("status") == "DELIVERY_ARRIVED_PICKUP_POINT"), None)
    delivered = next((ts(h) for h in hist if h.get("status") in DONE_OK), None)
    cancel = next((h for h in reversed(hist) if h.get("reason") in REASONS), None)
    cancel_reason = (cancel or {}).get("reason") or (state.get("reason") if state.get("reason") in REASONS else None)

    if status in DONE_OK:
        outcome = "Частично выкуплен" if status == "PARTICULARLY_DELIVERED" or refused else "Выкуплен"
    elif status in DONE_BAD:
        if cancel_reason:
            outcome = REASONS[cancel_reason]
        elif arrived:
            outcome = "Невыкуп после ПВЗ (причина не указана)"
        else:
            outcome = "Отмена до выдачи (причина не указана)"
    elif status == "VALIDATING_ERROR":
        outcome = "Ошибка создания"
    elif status in IN_TRANSIT:
        outcome = "В пути / в ПВЗ"
    else:
        outcome = UNKNOWN

    days_transit = None
    if created and (arrived or delivered):
        days_transit = ((arrived or delivered) - created).days
    days_at_pvz = None
    if arrived:
        end_pvz = delivered or (ts(cancel) if cancel else None)
        if end_pvz:
            days_at_pvz = (end_pvz - arrived).days

    rows.append({
        "LO": f"LO-{r.get('courier_order_id')}" if r.get("courier_order_id") else "",
        "номер_заказа_магазина": req.get("info", {}).get("operator_request_id", ""),
        "создан": created.strftime("%d.%m.%Y") if created else "",
        "итог": outcome,
        "текущий_статус": status,
        "описание_статуса": state.get("description", ""),
        "причина": cancel_reason or "",
        "оплата": PAY.get(req.get("billing_info", {}).get("payment_method"), req.get("billing_info", {}).get("payment_method", "")),
        "куда": "ПВЗ" if req.get("last_mile_policy") == "self_pickup" else "Курьер",
        "город": ((req.get("destination", {}).get("custom_location") or {}).get("details") or {}).get("locality", ""),
        "сумма_руб": round(amount),
        "сумма_выкуплено_руб": round(amount_bought),
        "единиц": units,
        "отказано_единиц": refused,
        "частичный_выкуп_разрешён": "да" if req.get("particular_items_refuse") else "нет",
        "примерка": "да" if fitting else "нет",
        "дней_до_ПВЗ_или_вручения": days_transit if days_transit is not None else "",
        "дней_в_ПВЗ": days_at_pvz if days_at_pvz is not None else "",
        "request_id": rid,
    })
    if i % 20 == 0:
        print(f"  обработано {i}/{len(all_req)}")

with open("delivery_orders.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter=";")
    w.writeheader()
    w.writerows(rows)

# ---------- 3. Сводка ----------
finished = [r for r in rows if r["итог"] not in ("В пути / в ПВЗ", "Ошибка создания", UNKNOWN)]
ok = lambda r: r["итог"] in ("Выкуплен", "Частично выкуплен")

def rate(lst):
    return f"{sum(ok(r) for r in lst)}/{len(lst)} = {sum(ok(r) for r in lst) / len(lst) * 100:.0f}%" if lst else "—"

log(f"\n=== Итоги по всем заявкам ===")
for k, v in Counter(r["итог"] for r in rows).most_common():
    log(f"  {k}: {v}")
unknown = Counter(r["текущий_статус"] or "(пусто)" for r in rows if r["итог"] == UNKNOWN)
if unknown:
    log(f"\n=== {UNKNOWN} — не входят в % выкупа, проверьте вручную ===")
    for k, v in unknown.most_common():
        log(f"  {k}: {v}")

log(f"\n=== Выкуп по завершённым заявкам: {rate(finished)} ===")
if failed:
    log(f"  (!) неполные данные: не выгружено окон — {len(failed)}, см. предупреждение в начале")
log(f"  Сумма выкупленных (без отказанных позиций): {sum(r['сумма_выкуплено_руб'] for r in finished if ok(r)):,.0f} ₽".replace(",", " "))
log(f"  Сумма невыкупленных (включая отказы в частичных выкупах): "
    f"{sum(r['сумма_руб'] - (r['сумма_выкуплено_руб'] if ok(r) else 0) for r in finished):,.0f} ₽".replace(",", " "))

def by(field, title):
    log(f"\n=== Выкуп по полю «{title}» ===")
    groups = defaultdict(list)
    for r in finished:
        groups[r[field]].append(r)
    for k, lst in sorted(groups.items(), key=lambda x: -len(x[1])):
        log(f"  {k or '—'}: {rate(lst)}")

by("оплата", "способ оплаты")
by("куда", "тип доставки")
by("примерка", "примерка")
by("частичный_выкуп_разрешён", "частичный выкуп")

log("\n=== Выкуп по сумме заказа ===")
bands = [(0, 5000), (5000, 10000), (10000, 20000), (20000, 10**9)]
for lo, hi in bands:
    lst = [r for r in finished if lo <= r["сумма_руб"] < hi]
    label = f"{lo:,}–{hi:,} ₽".replace(",", " ") if hi < 10**9 else f"от {lo:,} ₽".replace(",", " ")
    log(f"  {label}: {rate(lst)}")

log("\n=== Выкуп по сроку доставки (дней от создания до ПВЗ/вручения) ===")
for lo, hi in [(0, 3), (3, 5), (5, 8), (8, 100)]:
    lst = [r for r in finished if r["дней_до_ПВЗ_или_вручения"] != "" and lo <= r["дней_до_ПВЗ_или_вручения"] < hi]
    log(f"  {lo}–{hi - 1 if hi < 100 else '…'} дн.: {rate(lst)}")

pvz_bad = [r for r in finished if not ok(r) and r["дней_в_ПВЗ"] != ""]
if pvz_bad:
    avg = sum(r["дней_в_ПВЗ"] for r in pvz_bad) / len(pvz_bad)
    log(f"\nНевыкупленные заказы пролежали в ПВЗ в среднем {avg:.1f} дн.")

with open("delivery_report.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("\nСохранено: delivery_orders.csv и delivery_report.txt")

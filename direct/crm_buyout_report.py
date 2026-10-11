# -*- coding: utf-8 -*-
"""
Анализ выкупа по базе AWER CRM (данные Яндекс Кита).
Использует официальные поля Кита: статус заказа, refused_count по позициям,
purchased_price — так же, как сама CRM считает выручку.

База открывается ТОЛЬКО НА ЧТЕНИЕ, CRM можно не закрывать.

Запуск:  python crm_buyout_report.py
Результат: вывод в консоль + crm_buyout_report.txt + crm_buyout_orders.csv
"""
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

DB_FILE = Path.home() / "AWER CRM" / "kitcrm.db"
DATE_FROM = "2026-07-01"          # заказы, созданные с этой даты

COMPLETED = {"DELIVERED", "COMPLETED", "PARTIAL_REFUND", "FULL_REFUND"}
CANCELLED = {"CANCELLED", "DELIVERY_CANCELLED"}
LABELS = {
    "NEW": "Новый", "PENDING_PAYMENT": "Ожидает оплаты", "ORDER_PLACED": "Оформлен",
    "WAIT_FOR_CONFIRMATION": "Ждёт подтверждения", "CREATING_INITIAL_RECEIPT": "Создание чека",
    "SETUP_DELIVERY": "Настройка доставки", "WAIT_FOR_DELIVERY": "Ждёт доставки",
    "CANCELLATION_IN_PROGRESS": "Отмена в процессе", "DELIVERY_CANCELLED": "Доставка отменена",
    "FULL_REFUND": "Полный возврат", "PARTIAL_REFUND": "Частичный возврат",
    "CREATING_FINAL_RECEIPTS": "Создание финальных чеков", "DELIVERED": "Доставлен",
    "CANCELLED": "Отменён", "COMPLETED": "Завершён",
}
METHODS = {"PICKUP_POINT": "ПВЗ", "SELF_PICK_UP": "Самовывоз со склада", "COURIER": "Курьер"}

out = []
def log(s=""):
    print(s)
    out.append(str(s))


def money(v):
    return f"{v:,.0f} ₽".replace(",", " ")


def as_json(v):
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}


con = sqlite3.connect(f"file:{DB_FILE.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

orders = con.execute(
    """SELECT id, order_number, status, payment_method, total_final_price, purchased_price,
              promocode, kit_created_at, raw_payload, pickup_notified_chunks
       FROM orders WHERE kit_created_at >= ? ORDER BY kit_created_at""",
    (DATE_FROM,),
).fetchall()

items_by_order = defaultdict(list)
for it in con.execute("SELECT order_id, quantity, final_price, refused_count FROM order_items"):
    items_by_order[it["order_id"]].append(it)

rows = []
for o in orders:
    items = items_by_order.get(o["id"], [])
    units = sum(i["quantity"] or 0 for i in items)
    refused = sum(min(i["refused_count"] or 0, i["quantity"] or 0) for i in items)
    ordered_sum = float(o["total_final_price"] or 0) or float(
        sum((i["final_price"] or 0) * (i["quantity"] or 0) for i in items))
    status = o["status"] or ""

    if status in COMPLETED:
        if o["purchased_price"] is not None:
            bought_sum = float(o["purchased_price"])
        else:
            bought_sum = float(sum((i["final_price"] or 0) * max(0, (i["quantity"] or 0) - (i["refused_count"] or 0))
                                   for i in items))
        if status == "FULL_REFUND" or (units and refused >= units) or bought_sum <= 0:
            outcome = "Полный отказ"
        elif status == "PARTIAL_REFUND" or refused > 0 or bought_sum < ordered_sum - 1:
            outcome = "Частичный выкуп"
        else:
            outcome = "Полный выкуп"
    elif status in CANCELLED:
        outcome, bought_sum = "Отменён", 0.0
    else:
        outcome, bought_sum = "В работе", 0.0

    payload = as_json(o["raw_payload"])
    chunks = payload.get("delivery_chunks") or []
    method = ((chunks[0].get("delivery_info") or {}).get("method") if chunks else None) or ""
    notified = bool(as_json(o["pickup_notified_chunks"]))

    rows.append({
        "номер": o["order_number"], "создан": (o["kit_created_at"] or "")[:10],
        "статус_Кита": LABELS.get(status, status), "итог": outcome,
        "оплата": o["payment_method"] or "", "доставка": METHODS.get(method, method),
        "вещей": units, "отказано_вещей": refused,
        "сумма_заказа": round(ordered_sum), "выкуплено_на": round(bought_sum),
        "промокод": o["promocode"] or "", "письмо_о_ПВЗ": "да" if notified else "нет",
    })

with open("crm_buyout_orders.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["номер"], delimiter=";")
    w.writeheader()
    w.writerows(rows)

# ---------- Сводка ----------
log(f"Заказов с {DATE_FROM}: {len(rows)}")
log("\n=== Статусы Кита ===")
for k, v in Counter(r["статус_Кита"] for r in rows).most_common():
    log(f"  {k}: {v}")

log("\n=== Итоги ===")
for k, v in Counter(r["итог"] for r in rows).most_common():
    log(f"  {k}: {v}")

done = [r for r in rows if r["итог"] in ("Полный выкуп", "Частичный выкуп", "Полный отказ")]
closed = done + [r for r in rows if r["итог"] == "Отменён"]
bought = lambda r: r["итог"] in ("Полный выкуп", "Частичный выкуп")

def rate(lst):
    if not lst:
        return "—"
    n = sum(bought(r) for r in lst)
    s_ord = sum(r["сумма_заказа"] for r in lst)
    s_buy = sum(r["выкуплено_на"] for r in lst)
    by_money = f", по деньгам {s_buy / s_ord * 100:.0f}%" if s_ord else ""
    return f"{n}/{len(lst)} = {n / len(lst) * 100:.0f}% заказов{by_money}"

log(f"\n=== Выкуп (переданные в доставку и закрытые): {rate(done)} ===")
log(f"  С учётом отменённых до отправки: {rate(closed)}")
log(f"  Заказано на {money(sum(r['сумма_заказа'] for r in done))}, выкуплено на {money(sum(r['выкуплено_на'] for r in done))}")
part = [r for r in done if r["итог"] == "Частичный выкуп"]
if part:
    lost = sum(r["сумма_заказа"] - r["выкуплено_на"] for r in part)
    log(f"  Частичных выкупов: {len(part)}, не выкуплено в них на {money(lost)}")

def by(field, title):
    log(f"\n=== Выкуп по полю «{title}» ===")
    g = defaultdict(list)
    for r in done:
        g[r[field]].append(r)
    for k, lst in sorted(g.items(), key=lambda x: -len(x[1])):
        log(f"  {k or '—'}: {rate(lst)}")

by("оплата", "способ оплаты")
by("доставка", "способ доставки")

log("\n=== Выкуп по числу вещей в заказе ===")
for label, cond in [("1 вещь", lambda u: u == 1), ("2 вещи", lambda u: u == 2),
                    ("3 вещи", lambda u: u == 3), ("4 и больше", lambda u: u >= 4)]:
    log(f"  {label}: {rate([r for r in done if cond(r['вещей'])])}")

log("\n=== Выкуп по сумме заказа ===")
for lo, hi in [(0, 5000), (5000, 10000), (10000, 20000), (20000, 10**9)]:
    lst = [r for r in done if lo <= r["сумма_заказа"] < hi]
    label = f"{lo:,}–{hi:,} ₽".replace(",", " ") if hi < 10**9 else f"от {lo:,} ₽".replace(",", " ")
    log(f"  {label}: {rate(lst)}")

pvz = [r for r in done if r["доставка"] == "ПВЗ"]
if pvz:
    log("\n=== ПВЗ: было ли письмо о готовности к выдаче ===")
    for flag in ("да", "нет"):
        log(f"  письмо {flag}: {rate([r for r in pvz if r['письмо_о_ПВЗ'] == flag])}")

with_promo = [r for r in done if r["промокод"]]
if with_promo:
    log("\n=== Промокоды ===")
    for code, cnt in Counter(r["промокод"] for r in with_promo).most_common(10):
        log(f"  {code}: {cnt} заказов, {rate([r for r in with_promo if r['промокод'] == code])}")

with open("crm_buyout_report.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("\nСохранено: crm_buyout_report.txt и crm_buyout_orders.csv")

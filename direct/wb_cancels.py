# -*- coding: utf-8 -*-
"""
Отмены заказов Wildberries — сколько, у каких моделей и размеров, через сколько времени и КТО отменил.
Только чтение, в кабинете ничего не меняет.

Зачем: реклама платит за заказ ещё до того, как его отменят. При 30–40% отмен реальная цена заказа
с рекламы в полтора раза выше, чем в статистике WB. Понять причину — самый дешёвый способ поднять продажи.

Что показывает (по каждому кабинету, за последние N дней, по умолчанию 30):
  1. Сколько заказов отменено и на какую сумму.
  2. Кто отменил (только заказы с вашего склада, FBS; нужен доступ токена к «Маркетплейсу»):
       • вы (продавец) — сборочное задание отменено с вашей стороны: обычно товара/размера не оказалось на складе;
       • покупатель в первый час — передумал сразу после заказа (часто — увидел долгий срок доставки);
       • покупатель при получении — отказ на пункте выдачи;
       • WB — отменил сам: например, задание не собрали вовремя;
       • брак.
     Если доступа к «Маркетплейсу» нет — этот раздел пропускается, остальное работает.
  3. Через сколько после заказа отменяют: до 1 ч, 1–24 ч, 1–3 дня, 3–7 дней, позже.
  4. Отмены по складу (ваш склад FBS / склады WB), по моделям, размерам и регионам.
  5. «Примерка»: сколько отказов приходится на корзины, где одну модель взяли в нескольких размерах или цветах.

Запуск:  python wb_cancels.py                       (все товары, 30 дней)
         python wb_cancels.py 60                    (за 60 дней, не больше 90)
         python wb_cancels.py --models 27643,36446  (только эти модели — текст в артикуле продавца)
         python wb_cancels.py --cabinet "Яворская"
Нужны категории токена: «Статистика»; для раздела «кто отменил» — ещё «Маркетплейс».
Работает 1–3 минуты; можно запускать одновременно с wb_bids.py.
Результат: вывод на экран + wb_cancels_report.txt, wb_cancels.csv (все отменённые заказы)
"""
import datetime as dt
import sys
from collections import defaultdict

from wb_common import (YESTERDAY, ApiError, arg_cabinet, call, chunks, cut, fm, load_tokens, log, num, pace,
                       save_log, stat_rows, write_csv)

# ---------------- НАСТРОЙКИ ----------------
DEFAULT_DAYS = 30        # за сколько дней (не больше 90 — дальше «Статистика» WB заказы не отдаёт)
TOP = 15                 # сколько строк в таблицах по моделям / размерам / регионам
MIN_ORDERS = 10          # строку с меньшим числом заказов в таблицы не выводить (доля по 3 заказам ничего не значит)
# -------------------------------------------

MP = "https://marketplace-api.wildberries.ru"
WHO = {   # wbStatus / supplierStatus сборочного задания → кто отменил
    "seller": "вы (продавец) — отменили сборочное задание",
    "declined_by_client": "покупатель в первый час после заказа",
    "canceled_by_client": "покупатель при получении (отказ на пункте выдачи)",
    "canceled": "WB (например, задание не собрано вовремя)",
    "defect": "брак",
}
AGE = [(1, "до 1 часа"), (24, "1–24 часа"), (72, "1–3 дня"), (168, "3–7 дней"), (10 ** 9, "позже 7 дней")]


def days():
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a.isdigit() and not (i > 0 and args[i - 1] in ("--models", "--cabinet")):
            return min(int(a), 90)
    return DEFAULT_DAYS


def models():
    if "--models" in sys.argv:
        i = sys.argv.index("--models")
        if i + 1 < len(sys.argv):
            return [m.strip().lower() for m in sys.argv[i + 1].split(",") if m.strip()]
    return []


DAYS = days()


def ts(v):
    try:
        t = dt.datetime.fromisoformat(str(v or "")[:19])
        return t if t.year > 2000 else None
    except ValueError:
        return None


def fbs_status(token, d1):
    """{rid (= srid в «Статистике»): кто отменил} по сборочным заданиям FBS. None — нет доступа к «Маркетплейсу»."""
    ids, rid_of = [], {}
    start = dt.datetime.combine(d1 - dt.timedelta(days=1), dt.time())
    end = dt.datetime.now()
    while start < end:
        stop = min(start + dt.timedelta(days=30), end)
        nxt = 0
        for _ in range(200):
            pace(token, "mp", 0.3)
            data = call("GET", MP + "/api/v3/orders", token, what="Сборочные задания FBS",
                        params={"limit": 1000, "next": nxt, "dateFrom": int(start.timestamp()),
                                "dateTo": int(stop.timestamp())}) or {}
            orders = data.get("orders") or []
            for o in orders:
                if o.get("id") and o.get("rid"):
                    ids.append(o["id"])
                    rid_of[o["id"]] = o["rid"]
            nxt = data.get("next") or 0
            if len(orders) < 1000 or not nxt:
                break
        start = stop
    res = {}
    for part in chunks(ids, 1000):
        pace(token, "mp", 0.3)
        data = call("POST", MP + "/api/v3/orders/status", token, body={"orders": part},
                    what="Статусы сборочных заданий") or {}
        for s in data.get("orders") or []:
            rid = rid_of.get(s.get("id"))
            if not rid:
                continue
            if s.get("supplierStatus") == "cancel":
                res[rid] = "seller"
            elif s.get("wbStatus") in WHO:
                res[rid] = s["wbStatus"]
            else:
                res[rid] = "-" + str(s.get("wbStatus") or "")   # не отменено по данным сборочных заданий
    return res


def table(title, groups, key_name):
    rows = [(k, g) for k, g in groups.items() if g["n"] >= MIN_ORDERS]
    rows.sort(key=lambda x: -x[1]["c"])
    if not rows:
        return
    log(f"\n  {title} (где заказов ≥ {MIN_ORDERS}; по числу отмен):")
    log(f"    {key_name:28}{'заказов':>9}{'отмен':>7}{'доля':>7}{'сумма отмен ₽':>15}")
    for k, g in rows[:TOP]:
        log(f"    {cut(k, 28):28}{g['n']:>9}{g['c']:>7}{g['c'] / g['n']:>7.0%}{fm(g['sum']):>15}")


def run_cabinet(cab, token, mods):
    log(f"\n══════════ {cab} ══════════")
    d1 = YESTERDAY - dt.timedelta(days=DAYS - 1)
    print("  заказы из «Статистики» (WB отдаёт 1 раз в минуту)...")
    rows = stat_rows(token, "orders", d1)
    seen, orders = set(), []
    for r in rows:
        t = ts(r.get("date"))
        if not t or not (d1 <= t.date() <= YESTERDAY):
            continue
        key = r.get("srid") or (r.get("date"), r.get("nmId"), r.get("barcode"))
        if key in seen:
            continue
        seen.add(key)
        art = str(r.get("supplierArticle") or "")
        if mods and not any(m in art.lower() for m in mods):
            continue
        orders.append(r)
    if not orders:
        log("  Заказов за период нет.")
        return []
    canc = [r for r in orders if r.get("isCancel")]
    price = lambda r: num(r.get("priceWithDisc")) or num(r.get("totalPrice"))
    log(f"  Период {d1:%d.%m}–{YESTERDAY:%d.%m.%Y} ({DAYS} дн.)" + (f", модели: {', '.join(mods)}" if mods else ", все товары"))
    log(f"  Заказов {len(orders)}, отменено {len(canc)} ({len(canc) / len(orders):.0%}) "
        f"на {fm(sum(price(r) for r in canc))} ₽ из {fm(sum(price(r) for r in orders))} ₽.")

    # кто отменил — по сборочным заданиям FBS
    print("  статусы сборочных заданий FBS (кто отменил)...")
    who = None
    try:
        who = fbs_status(token, d1)
    except ApiError as e:
        log(f"\n  Кто отменил — недоступно: {e}")
        log("  Нужна категория токена «Маркетплейс». Остальные разделы — по «Статистике».")
    fbs = lambda r: "продав" in str(r.get("warehouseType") or "").lower()
    if who is not None:
        cnt, unknown, not_cancel = defaultdict(int), 0, 0
        for r in canc:
            w = who.get(r.get("srid"))
            if w is None:
                unknown += 1
            elif w.startswith("-"):
                not_cancel += 1
            else:
                cnt[w] += 1
        known = sum(cnt.values())
        log(f"\n  1. Кто отменил (заказы с вашего склада, найдено {known} из {sum(1 for r in canc if fbs(r))} отменённых FBS):")
        for w, n in sorted(cnt.items(), key=lambda x: -x[1]):
            log(f"    {WHO[w]:52}{n:>6}  {n / known:>5.0%}")
        if not_cancel:
            log(f"    (ещё {not_cancel} — в «Статистике» отменены, а в сборочных заданиях нет: возможно, статус ещё не обновился)")
        if unknown:
            log(f"    (ещё {unknown} — не нашлись среди сборочных заданий: заказы со складов WB или старше 3 месяцев)")
        if known:
            top = max(cnt, key=cnt.get)
            hint = {
                "seller": "Чаще отменяете вы — значит, остаток на вашем складе в WB не совпадает с реальным: "
                          "заказывают размер, которого нет. Сверить остатки FBS (особенно ходовые размеры).",
                "declined_by_client": "Чаще передумывает покупатель в первый час — обычно из-за долгого срока доставки "
                                      "по сравнению с конкурентами со складов WB. Рычаги FBS: срок сборки, склад ближе.",
                "canceled_by_client": "Чаще отказываются при получении — размер не подошёл, качество/фото не совпали "
                                      "с ожиданием. Смотреть отзывы и размерную сетку.",
                "canceled": "Чаще отменяет WB — сборочные задания не успевают собрать/сдать вовремя. "
                            "Проверить скорость сборки и график сдачи.",
                "defect": "Чаще брак — проверить партию.",
            }[top]
            log(f"    Вывод: {hint}")

    # через сколько отменяют
    ages = defaultdict(int)
    for r in canc:
        a, c = ts(r.get("date")), ts(r.get("cancelDate"))
        if a and c:
            h = (c - a).total_seconds() / 3600
            ages[next(lbl for lim, lbl in AGE if h < lim)] += 1
    if ages:
        tot = sum(ages.values())
        log(f"\n  2. Через сколько после заказа отменяют ({tot} с известной датой отмены):")
        for _, lbl in AGE:
            log(f"    {lbl:14}{ages[lbl]:>6}  {ages[lbl] / tot:>5.0%}  " + "█" * round(ages[lbl] / tot * 50))
        log("    До 1 часа — покупатель передумал сразу; 1–3 дня — обычно отмена сборки; позже — отказ при получении.")

    # «примерка»: одна модель в одной корзине (gNumber) в нескольких размерах или цветах
    model = lambda r: next((m for m in mods if m in str(r.get("supplierArticle") or "").lower()), None) or str(r.get("nmId"))
    baskets = defaultdict(list)
    for r in orders:
        if r.get("gNumber"):
            baskets[(r["gNumber"], model(r))].append(r)
    if baskets:
        kinds = {"один размер, один цвет": [], "несколько размеров": [], "несколько цветов, размер один": []}
        for items in baskets.values():
            sizes = {str(r.get("techSize")) for r in items}
            colors = {r.get("nmId") for r in items}
            k = ("несколько размеров" if len(sizes) > 1 else
                 "несколько цветов, размер один" if len(colors) > 1 else "один размер, один цвет")
            kinds[k] += items
        log("\n  3. Заказывают ли «на примерку» (одна модель в одной корзине покупателя):")
        log(f"    {'как заказали':32}{'вещей':>7}{'отказ':>7}{'доля':>7}{'доля всех отмен':>17}")
        for k, items in kinds.items():
            c = sum(1 for r in items if r.get("isCancel"))
            if items:
                log(f"    {k:32}{len(items):>7}{c:>7}{c / len(items):>7.0%}{c / len(canc) if canc else 0:>17.0%}")
        multi = kinds["несколько размеров"]
        if multi:
            log("    Если в корзинах «несколько размеров» отказов много — люди не уверены в размере и берут два-три на примерку:")
            log("    это лечится понятной размерной сеткой и подсказкой в карточке («маломерит / в размер»), а не ставками.")
        if not mods:
            log("    (без --models модель = артикул WB, поэтому «несколько цветов» не видно; запустите с --models 27643,36446)")
    else:
        log("\n  3. «Примерка»: WB не отдал номер корзины (gNumber) — раздел пропущен.")

    def group(fn):
        g = defaultdict(lambda: {"n": 0, "c": 0, "sum": 0.0})
        for r in orders:
            x = g[fn(r)]
            x["n"] += 1
            if r.get("isCancel"):
                x["c"] += 1
                x["sum"] += price(r)
        return g

    log("\n  4. Где отменяют чаще:")
    table("Склад", group(lambda r: "ваш склад (FBS)" if fbs(r) else f"WB: {r.get('warehouseName') or '?'}"), "склад")
    table("Артикулы продавца", group(lambda r: str(r.get("supplierArticle") or "?")), "артикул")
    table("Размеры", group(lambda r: str(r.get("techSize") or "?")), "размер")
    table("Регионы", group(lambda r: str(r.get("regionName") or r.get("oblastOkrugName") or "?")), "регион")
    log("\n  Доля отмен по маленьким группам случайна — смотрите на строки с большим числом заказов.")

    out = []
    for r in canc:
        a, c = ts(r.get("date")), ts(r.get("cancelDate"))
        w = (who or {}).get(r.get("srid"))
        out.append({"кабинет": cab, "дата заказа": str(r.get("date") or "")[:16].replace("T", " "),
                    "дата отмены": str(r.get("cancelDate") or "")[:16].replace("T", " ") if c else "",
                    "часов до отмены": round((c - a).total_seconds() / 3600, 1) if a and c else "",
                    "артикул WB": r.get("nmId"), "артикул продавца": r.get("supplierArticle"),
                    "размер": r.get("techSize"), "склад": r.get("warehouseName"),
                    "тип склада": r.get("warehouseType"), "регион": r.get("regionName"),
                    "цена ₽": round(price(r), 2),
                    "кто отменил": WHO.get(w, "") if w and not w.startswith("-") else ""})
    return out


def main():
    mods = models()
    cabs = load_tokens(arg_cabinet())
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — отмены заказов WB за {DAYS} дн. (только отчёт, в кабинете ничего не меняется)")
    allrows = []
    for cab, token in cabs.items():
        try:
            allrows += run_cabinet(cab, token, mods)
        except ApiError as e:
            log(f"  ✗ {cab}: {e}")
    write_csv("wb_cancels.csv", allrows, header=list(allrows[0]) if allrows else None)
    save_log("wb_cancels_report.txt")
    print("\nСохранено: wb_cancels_report.txt, wb_cancels.csv")


if __name__ == "__main__":
    main()

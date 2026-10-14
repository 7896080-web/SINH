# -*- coding: utf-8 -*-
"""
Ежедневный отчёт Wildberries: вчера против среднего за прошлую неделю + список действий.

Что внутри (по каждому кабинету):
  1. Итоги: заказы (шт, ₽), отмены, выкупы, возвраты, расход рекламы, ДРР, доля заказов с рекламы,
     оценка прибыли за вчера (если есть юнит-экономика из wb_unit.py).
  2. Товары: кто вырос, кто упал, расход рекламы, остаток (склады WB + свой склад FBS) и на сколько дней его хватит.
  3. Действия по важности:
       • товар закончится (WB + свой склад) в ближайшие 7 дней — сколько нужно ещё (на 21 день продаж);
       • реклама крутится на товар, которого нет нигде;
       • реклама дороже, чем окупается (по юнит-экономике);
       • резкое падение или рост заказов по товару;
       • убыточные товары (по юнит-экономике);
       • главное из отчёта по поисковым запросам (wb_queries.py);
       • сигналы по позициям в поиске и воронке (wb_positions.py — запускать перед этим отчётом).

Запуск:  python wb_daily.py                      (все кабинеты из wb_tokens.txt)
         python wb_daily.py --cabinet "Яворская"
Нужны категории токена: «Статистика», «Продвижение», «Аналитика».
Удобно запускать каждое утро (в Планировщике заданий Windows), результат — в wb_daily_report.txt.
"""
import datetime as dt
import math
from collections import defaultdict

from wb_common import (HERE, ApiError, YESTERDAY, ad_stats, arg_cabinet, campaigns, cut, fm, funnel, load_tokens,
                       load_unit, log, num, pct, read_csv, save_log, stat_rows, to_float, write_csv, each_cabinet)


def age_days(name):
    p = HERE / name
    return None if not p.exists() else (dt.datetime.now().timestamp() - p.stat().st_mtime) / 86400

# ======================== НАСТРОЙКИ ========================
STOCK_DAYS_ALERT = 7        # предупредить, если остатка (склады WB + свой склад FBS) хватит меньше чем на столько дней
STOCK_DAYS_TARGET = 21      # на сколько дней продаж держать запас
# Работаем по FBS (решение 12.10.2026): в наличии = склады WB + свой склад; поставки на WB не предлагаем.
DROP = 0.5                  # «падение» — вчера меньше половины среднего
JUMP = 2.0                  # «рост» — вчера больше двух средних
QUERIES_MAX_AGE = 8         # пункты из wb_queries_actions.csv старше стольких дней не показывать
# ===========================================================

Y = YESTERDAY
P1, P2 = Y - dt.timedelta(days=7), Y - dt.timedelta(days=1)     # прошлая неделя


def day(s):
    return str(s or "")[:10]


def analyze(cab, token):
    log(f"\n{'═' * 20} {cab} {'═' * 20}")
    print(f"  {cab}: заказы и продажи (WB отдаёт раз в минуту)...", flush=True)
    orders = stat_rows(token, "orders", P1)
    sales = stat_rows(token, "sales", P1)
    O = defaultdict(lambda: defaultdict(float))      # (день, артикул) → заказы
    info = {}
    for r in orders:
        d = day(r.get("date"))
        if not (P1.isoformat() <= d <= Y.isoformat()):
            continue
        nm = r.get("nmId")
        info.setdefault(nm, {"vendor": r.get("supplierArticle"), "subject": r.get("subject")})
        o = O[(d, nm)]
        o["orders"] += 1
        o["sum"] += num(r.get("priceWithDisc"))
        if r.get("isCancel"):
            o["cancel"] += 1
    for r in sales:
        d = day(r.get("date"))
        if not (P1.isoformat() <= d <= Y.isoformat()):
            continue
        nm = r.get("nmId")
        info.setdefault(nm, {"vendor": r.get("supplierArticle"), "subject": r.get("subject")})
        o = O[(d, nm)]
        if str(r.get("saleID") or "").startswith("S"):
            o["buyouts"] += 1
            o["buyout_sum"] += num(r.get("priceWithDisc"))
        elif str(r.get("saleID") or "").startswith("R"):
            o["returns"] += 1

    print(f"  {cab}: реклама...", flush=True)
    try:
        camps = campaigns(token, changed_since=P1.isoformat())
        ad_day, _ = ad_stats(token, list(camps), P1, Y) if camps else ({}, {})
    except ApiError as e:
        log(f"  ! {e} — без рекламы.")
        ad_day = {}
    for (d, nm), v in ad_day.items():
        o = O[(d, nm)]
        o["ad_spend"] += v["sum"]
        o["ad_orders"] += v["orders"]
        o["ad_sum"] += v["sum_price"]

    nms = sorted({nm for (_, nm) in O if nm})
    print(f"  {cab}: остатки...", flush=True)
    try:
        stock = funnel(token, Y, Y, nms) if nms else {}
    except ApiError as e:
        log(f"  ! {e} — без остатков.")
        stock = {}
    unit = load_unit(cab)

    def agg(days, nm=None):
        t = defaultdict(float)
        for (d, n), v in O.items():
            if d in days and (nm is None or n == nm):
                for k, x in v.items():
                    t[k] += x
        return t

    ydays = {Y.isoformat()}
    pdays = {(P1 + dt.timedelta(days=i)).isoformat() for i in range(7)}
    ty, tp = agg(ydays), agg(pdays)
    avg = {k: v / 7 for k, v in tp.items()}

    def row(title, k, d=0, unit_s=""):
        y, a = ty.get(k, 0), avg.get(k, 0)
        ch = f"{(y / a - 1) * 100:+.0f}%" if a else ""
        log(f"  {title:34}{fm(y, d, unit_s):>14}{fm(a, d, unit_s):>14}{ch:>9}")

    log(f"Вчера {Y:%d.%m.%Y} против среднего за {P1:%d.%m}–{P2:%d.%m}:")
    log(f"  {'':34}{'вчера':>14}{'в среднем':>14}")
    row("Заказы, шт", "orders")
    row("Заказы, ₽", "sum", 0, " ₽")
    row("Отмены, шт", "cancel")
    row("Выкупы, шт", "buyouts")
    row("Выкуплено, ₽", "buyout_sum", 0, " ₽")
    row("Возвраты, шт", "returns")
    row("Реклама, ₽", "ad_spend", 0, " ₽")
    drr_y, drr_a = pct(ty.get("ad_spend", 0), ty.get("sum", 0)), pct(avg.get("ad_spend", 0), avg.get("sum", 0))
    log(f"  {'ДРР от всех заказов':34}{fm(drr_y, 1, '%'):>14}{fm(drr_a, 1, '%'):>14}")
    log(f"  {'Заказы с рекламы, доля':34}{fm(pct(ty.get('ad_sum', 0), ty.get('sum', 0)), 0, '%'):>14}"
        f"{fm(pct(avg.get('ad_sum', 0), avg.get('sum', 0)), 0, '%'):>14}")
    if unit:
        prof, covered = 0.0, 0.0
        for (d, nm), v in O.items():
            if d in ydays and nm in unit and to_float(unit[nm].get("прибыль на заказ")) is not None:
                prof += v["orders"] * to_float(unit[nm]["прибыль на заказ"])
                covered += v["sum"]
        if ty.get("sum") and covered / ty["sum"] >= 0.5:
            log(f"  {'Прибыль с заказов вчера (оценка)':34}{fm(prof - ty.get('ad_spend', 0), 0, ' ₽'):>14}"
                f"   до рекламы {fm(prof, 0, ' ₽')}, по юнит-экономике на {covered / ty['sum']:.0%} заказов")
    if drr_y and drr_a and drr_y > drr_a * 1.5 and ty.get("ad_spend", 0) > 500:
        log(f"  ! ДРР вчера в {drr_y / drr_a:.1f} раза выше среднего — проверьте ставки и кампании.")

    # ---- по товарам ----
    prow = []
    for nm in nms:
        y, p = agg(ydays, nm), agg(pdays, nm)
        a_orders = p.get("orders", 0) / 7
        s = stock.get(nm) or {}
        wb = s.get("stock_wb")
        tot = None if (wb is None and s.get("stock_mp") is None) else (wb or 0) + (s.get("stock_mp") or 0)
        days_left = tot / a_orders if (tot is not None and a_orders > 0) else None
        u = unit.get(nm) or {}
        ad7, adord7 = p.get("ad_spend", 0) + y.get("ad_spend", 0), p.get("ad_orders", 0) + y.get("ad_orders", 0)
        prow.append({"кабинет": cab, "артикул WB": nm, "артикул продавца": (s.get("vendor") or (info.get(nm) or {}).get("vendor") or ""),
                     "название": s.get("title") or "", "заказы вчера": int(y.get("orders", 0)),
                     "заказы в среднем": round(a_orders, 2), "заказы вчера ₽": round(y.get("sum", 0), 2),
                     "реклама вчера ₽": round(y.get("ad_spend", 0), 2), "реклама 8 дн ₽": round(ad7, 2),
                     "заказов с рекламы 8 дн": int(adord7),
                     "цена заказа с рекламы": round(ad7 / adord7, 2) if adord7 else "",
                     "окупаемая цена заказа": u.get("окупаемая цена заказа", ""),
                     "в наличии шт": tot if tot is not None else "",
                     "на WB шт": wb if wb is not None else "", "свой склад шт": s.get("stock_mp") if s.get("stock_mp") is not None else "",
                     "хватит дней": round(days_left, 1) if days_left is not None else ""})
    prow.sort(key=lambda r: (-r["заказы вчера"], -r["заказы в среднем"]))
    log("\n  Товары (топ-15 по заказам вчера):")
    log(f"  {'артикул':>11} {'арт. продавца':20}{'вчера':>6}{'сред.':>6}{'изм.':>7}{'рекл. ₽':>9}{'в нал.':>7}{'дней':>6}")
    for r in prow[:15]:
        a = r["заказы в среднем"]
        ch = f"{(r['заказы вчера'] / a - 1) * 100:+.0f}%" if a else ""
        log(f"  {r['артикул WB']:>11} {cut(r['артикул продавца'], 20):20}{r['заказы вчера']:>6}{fm(a, 1):>6}{ch:>7}"
            f"{fm(r['реклама вчера ₽']):>9}{str(r['в наличии шт']):>7}{fm(r['хватит дней'] if r['хватит дней'] != '' else None):>6}")

    # ---- действия ----
    acts = []

    def act(prio, kind, r, text):
        acts.append({"кабинет": cab, "приоритет": prio, "действие": kind, "артикул WB": r.get("артикул WB", ""),
                     "артикул продавца": r.get("артикул продавца", ""), "что сделать": text})

    for r in prow:
        a, wb, tot = r["заказы в среднем"], r["на WB шт"], r["в наличии шт"]
        own = r["свой склад шт"]
        if tot != "" and a >= 0.5 and r["хватит дней"] != "" and r["хватит дней"] < STOCK_DAYS_ALERT:
            need = max(0, math.ceil(a * STOCK_DAYS_TARGET - tot))
            act(1, "мало остатка", r, f"в наличии {tot:.0f} шт (WB {wb if wb != '' else '?'}, свой склад "
                f"{own if own != '' else '?'}) — хватит на {r['хватит дней']:.0f} дн. при {a:.1f} заказа/день; "
                f"на {STOCK_DAYS_TARGET} дн. нужно ещё ≈{need} шт")
        if r["реклама вчера ₽"] > 0 and tot == 0:
            act(1, "реклама без остатка", r, f"вчера {fm(r['реклама вчера ₽'])} ₽ рекламы, а товара нет ни на WB, ни на своём "
                "складе — снять с рекламы (автопилот уберёт сам при остатке < 15 шт)")
        lim = to_float(r["окупаемая цена заказа"])
        cpo = to_float(r["цена заказа с рекламы"])
        if lim is not None and cpo and r["заказов с рекламы 8 дн"] >= 3 and cpo > lim:
            act(2, "реклама не окупается", r, f"заказ с рекламы {fm(cpo)} ₽ при окупаемых {fm(lim)} ₽ — снизить ставку "
                "(в wb_bids.py: \"max_cost_per_order\": \"auto\")")
        elif lim is not None and r["реклама 8 дн ₽"] > max(2 * lim, 500) and r["заказов с рекламы 8 дн"] == 0:
            act(2, "реклама не окупается", r, f"{fm(r['реклама 8 дн ₽'])} ₽ рекламы за 8 дней без заказов")
        if a >= 2 and r["заказы вчера"] < a * DROP and (tot == "" or tot > 0):
            act(2, "падение заказов", r, f"вчера {r['заказы вчера']} при среднем {a:.1f} — проверьте позицию, цену, отзывы, ставки")
        if a >= 1 and r["заказы вчера"] > a * JUMP:
            act(3, "рост заказов", r, f"вчера {r['заказы вчера']} при среднем {a:.1f} — проверьте остаток; можно поднять цену или ставку")
    for nm, u in unit.items():
        pu = to_float(u.get("прибыль на шт"))
        if pu is not None and pu < 0 and int(to_float(u.get("продано шт")) or 0) >= 3:
            act(3, "убыточный товар", {"артикул WB": nm, "артикул продавца": u.get("артикул продавца")},
                f"по юнит-экономике {fm(pu)} ₽ на проданной штуке — поднять цену или вывести")
    qa = age_days("wb_queries_actions.csv")
    q_rows = read_csv("wb_queries_actions.csv") if qa is not None and qa <= QUERIES_MAX_AGE else []
    for q in [x for x in q_rows if x.get("кабинет") == cab][:8]:
        act(4, "запрос: " + q.get("действие", ""), {"артикул WB": q.get("артикул WB"), "артикул продавца": q.get("артикул продавца")},
            f"«{q.get('запрос')}» ({q.get('класс')}, {q.get('запросов в неделю')}/нед) — {q.get('почему')}")
    pa = age_days("wb_positions_actions.csv")
    if pa is not None and pa <= 1.5:
        for x in [r for r in read_csv("wb_positions_actions.csv")
                  if r.get("кабинет") == cab and r.get("приоритет") in ("1", "2")][:10]:
            act(2 if x["приоритет"] == "1" else 3, "позиции: " + x.get("действие", ""),
                {"артикул WB": x.get("артикул WB"), "артикул продавца": x.get("артикул продавца")}, x.get("что сделать", ""))
    acts.sort(key=lambda x: x["приоритет"])
    if acts:
        log("\n  Что сделать сегодня:")
        last = None
        for x in acts[:40]:
            if x["действие"] != last:
                log(f"   [{x['действие']}]")
                last = x["действие"]
            log(f"     {str(x['артикул WB']):>11} {cut(x['артикул продавца'], 20):20} {x['что сделать']}")
    else:
        log("\n  Срочных действий нет.")
    if not unit:
        log("  (Юнит-экономики нет — запустите wb_unit.py, тогда отчёт покажет прибыль и окупаемость рекламы.)")
    elif (age_days("wb_unit_economics.csv") or 0) > 14:
        log(f"  (Юнит-экономике {age_days('wb_unit_economics.csv'):.0f} дн. — обновите: python wb_unit.py)")
    if pa is None:
        log("  (Позиции в поиске и воронку по дням отслеживает wb_positions.py — запускайте его каждое утро перед этим отчётом.)")
    elif pa > 1.5:
        log(f"  (Позициям {pa:.0f} дн. — обновите: python wb_positions.py)")
    if qa is None:
        log("  (Анализа поисковых запросов нет — запустите wb_queries.py раз в неделю.)")
    elif qa > QUERIES_MAX_AGE:
        log(f"  (Анализу запросов {qa:.0f} дн. — его пункты не показываю, они устарели. Обновите: python wb_queries.py)")
    return prow, acts


def main():
    cabs = load_tokens(arg_cabinet())
    log(f"Ежедневный отчёт WB: {len(cabs)} каб.")
    allp, alla = [], []
    for cab, res, err in each_cabinet(cabs, analyze):      # кабинеты опрашиваются одновременно
        if err is not None:
            log(f"  ✗ {cab}: {err}")
            continue
        p, a = res
        allp += p
        alla += a
    write_csv("wb_daily_products.csv", allp)
    write_csv("wb_daily_actions.csv", alla)
    save_log("wb_daily_report.txt")
    print("\nСохранено: wb_daily_report.txt, wb_daily_products.csv, wb_daily_actions.csv")


if __name__ == "__main__":
    main()

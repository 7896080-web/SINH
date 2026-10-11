# -*- coding: utf-8 -*-
"""
Поисковые запросы Wildberries (аналог Вордстата WB) по вашим товарам и что с ними делать.

Для каждого ходового артикула берёт до 100 главных запросов за 7 дней (30 — на тарифе «Джем Стандарт»):
  сколько раз запрос ищут за неделю (частотность), медианная и средняя позиция товара, переходы, корзины, заказы,
  конверсия по сравнению с конкурентами по этому же запросу, расход рекламы по запросу.
Делит запросы на ВЧ / СЧ / НЧ и выдаёт действие по каждому:
  • НЧ/СЧ продают, но товар далеко в выдаче → поднять ставку / передать в «лучшие запросы»;
  • ВЧ не конвертят хуже конкурентов → не покупать рекламой за клики, забирать трафик на НЧ;
  • ВЧ с конверсией не хуже конкурентов → кандидаты в кампанию «поиск за показы» (этап 2);
  • запросы не про вашу аудиторию (женские, детские) → минус-фразы;
  • запрос часто ищут, а товара почти не видно → SEO: слова запроса в название и характеристики.

Запуск:  python wb_queries.py                     (все кабинеты из wb_tokens.txt)
         python wb_queries.py --cabinet "Яворская"
Нужны категории токена: «Аналитика», «Продвижение» и подписка «Джем»: «Продвинутый»/«Премиальный» дают до 100
запросов на товар, «Стандарт» — до 30 (скрипт переключится сам). Ежедневная история позиций — wb_positions.py.
Результат: wb_queries_report.txt, wb_queries.csv, wb_queries_actions.csv
"""
import datetime as dt
from collections import defaultdict

from wb_common import (ApiError, YESTERDAY, arg_cabinet, campaigns, cluster_stats, cut, fm, funnel, load_tokens,
                       log, pct, save_log, search_texts, write_csv)

# ======================== НАСТРОЙКИ ========================
HF, MF = 10000, 1000       # запросов в неделю: ВЧ — от 10 000, СЧ — от 1 000, НЧ — меньше
TOP_NMS = 40               # сколько артикулов разбирать (по заказам за 30 дней + все рекламируемые)
QUERIES_PER_NM = 100       # 100 — «Джем Продвинутый/Премиальный»; на «Стандарте» скрипт сам возьмёт 30
NEGATIVE_WORDS = ["женск", "детск", "девочк", "мальчик", "для девушки", "подростк"]
FAR_POSITION = 20          # «далеко в выдаче» — дальше этой позиции
# ===========================================================

D2 = YESTERDAY
D1 = D2 - dt.timedelta(days=6)


def klass(freq):
    return "ВЧ" if freq >= HF else ("СЧ" if freq >= MF else "НЧ")


def cur(x, k="current"):
    return (x or {}).get(k) if isinstance(x, dict) else None


def analyze(cab, token):
    log(f"\n{'═' * 20} {cab} {'═' * 20}")
    print("  товары и реклама...")
    f30 = funnel(token, YESTERDAY - dt.timedelta(days=29), YESTERDAY)
    camps = campaigns(token, statuses=(9, 11))
    ad_nms = {s.get("nm_id") for c in camps.values() for s in c.get("nm_settings") or []}
    top = [nm for nm, v in sorted(f30.items(), key=lambda x: -x[1]["orderCount"]) if v["orderCount"] > 0][:TOP_NMS]
    nms = list(dict.fromkeys(top + sorted(n for n in ad_nms if n)))[:TOP_NMS + 20]
    if not nms:
        log("  Нет товаров с заказами или рекламой.")
        return [], []
    print(f"  поисковые запросы по {len(nms)} товарам...")
    try:
        items = search_texts(token, nms, D1, D2, QUERIES_PER_NM)
    except ApiError as e:
        log(f"  ✗ {e}")
        if e.status in (402, 403):
            log("  Отчёт по поисковым запросам WB может требовать подписку «Джем». Без неё смотрите рекламные "
                "кластеры в wb_ads_report.py.")
        return [], []
    pairs = [(cid, s.get("nm_id")) for cid, c in camps.items() for s in c.get("nm_settings") or [] if s.get("nm_id") in set(nms)]
    try:
        cl = cluster_stats(token, pairs, D1, D2) if pairs else {}
    except ApiError as e:
        log(f"  ! {e} — без расходов рекламы по запросам.")
        cl = {}
    ad_q = defaultdict(lambda: defaultdict(float))       # (nm, запрос) → реклама
    for (cid, nm), qs in cl.items():
        for q, s in qs.items():
            for k, v in s.items():
                ad_q[(nm, q)][k] += v

    rows, actions = [], []
    by_class = defaultdict(lambda: defaultdict(float))
    for it in items:
        q = (it.get("text") or "").strip()
        nm = it.get("nmId")
        freq = it.get("weekFrequency") or cur(it.get("frequency")) or 0
        k = klass(freq)
        avg_pos = cur(it.get("avgPosition"))
        pos = cur(it.get("medianPosition")) or avg_pos       # медиана устойчивее к регионам и персонализации
        opens, carts, orders = cur(it.get("openCard")) or 0, cur(it.get("addToCart")) or 0, cur(it.get("orders")) or 0
        p_cart = cur(it.get("openToCart"), "percentile")
        p_order = cur(it.get("cartToOrder"), "percentile")
        p_ord_total = cur(it.get("orders"), "percentile")
        vis = cur(it.get("visibility"))
        a = ad_q.get((nm, q.lower()), {})
        spend, a_clicks, a_orders = a.get("spend", 0), a.get("clicks", 0), a.get("orders", 0)
        bc = by_class[k]
        bc["queries"] += 1
        bc["opens"] += opens
        bc["orders"] += orders
        bc["spend"] += spend
        bc["pos_sum"] += (pos or 0) * opens
        neg = any(w in q.lower() for w in NEGATIVE_WORDS)
        conv_ok = (p_cart is not None and p_cart >= 0) or (p_order is not None and p_order >= 0)
        conv_bad = (p_cart is not None and p_cart <= -30) and (p_order is None or p_order <= 0)
        act, why, prio = "", "", 9
        if neg:
            act, prio = "минус-фраза", 1
            why = "запрос не про вашу аудиторию" + (f"; реклама {fm(spend, 0, ' ₽')}" if spend else "")
        elif spend >= 300 and a_orders == 0 and a_clicks >= 30:
            act, prio = "минус-фраза", 2
            why = f"реклама {fm(spend, 0, ' ₽')}, {int(a_clicks)} кликов, 0 заказов" + (
                " — для карточки этот ВЧ слишком дорог; из рекламы убрать, органический показ останется" if k == "ВЧ" else "")
        elif k in ("НЧ", "СЧ") and orders >= 1 and pos and pos > FAR_POSITION:
            act, prio = "поднять ставку", 3
            why = f"{k}: {int(orders)} заказ(ов) даже с позиции {pos} — поднять в топ-10 дешевле, чем ВЧ"
        elif k == "ВЧ" and opens >= 20 and orders == 0 and conv_bad:
            act, prio = "не покупать ВЧ", 4
            why = f"конверсия в корзину на {abs(p_cart)}% хуже конкурентов — трафик дорогой и не продаёт; брать НЧ"
        elif k == "ВЧ" and conv_ok and pos and pos > 10:
            act, prio = "кандидат «за показы»", 5
            why = f"конверсия не хуже конкурентов, позиция {pos}: в кампанию «поиск за показы» (этап 2)"
        elif freq >= MF and (pos is None or pos > 100) and opens < 5:
            act, prio = "SEO", 6
            why = f"ищут {fm(freq)} раз/нед, а товар почти не виден — если запрос про ваш товар, добавьте его слова в название/характеристики"
        elif orders >= 2 and pos and pos <= 10:
            act, prio = "держать", 8
            why = f"продаёт с позиции {pos}"
        row = {"кабинет": cab, "артикул WB": nm, "артикул продавца": it.get("vendorCode"), "запрос": q, "класс": k,
               "запросов в неделю": freq, "медианная позиция": pos, "средняя позиция": avg_pos, "видимость %": vis, "переходы": opens, "корзины": carts,
               "заказы": orders, "в корзину vs конкуренты %": p_cart, "в заказ vs конкуренты %": p_order,
               "заказы vs конкуренты %": p_ord_total, "реклама ₽": round(spend, 2), "реклама клики": int(a_clicks),
               "реклама заказы": int(a_orders), "действие": act, "почему": why}
        rows.append(row)
        if act and act != "держать":
            actions.append((prio, -freq, row))

    # ---- сводка ВЧ/СЧ/НЧ ----
    log(f"Запросы за {D1:%d.%m}–{D2:%d.%m}: {len(rows)} по {len({r['артикул WB'] for r in rows})} товарам. "
        f"ВЧ — от {fm(HF)} поисков в неделю, СЧ — от {fm(MF)}, НЧ — меньше.")
    tot_o = sum(v["orders"] for v in by_class.values())
    tot_s = sum(v["spend"] for v in by_class.values())
    log(f"  {'класс':6}{'запросов':>10}{'переходы':>10}{'заказы':>8}{'доля заказов':>14}{'реклама ₽':>11}{'доля рекламы':>14}{'позиция':>9}")
    for k in ("ВЧ", "СЧ", "НЧ"):
        v = by_class.get(k)
        if not v:
            continue
        log(f"  {k:6}{fm(v['queries']):>10}{fm(v['opens']):>10}{fm(v['orders']):>8}{fm(pct(v['orders'], tot_o), 0, '%'):>14}"
            f"{fm(v['spend']):>11}{fm(pct(v['spend'], tot_s), 0, '%'):>14}{fm(v['pos_sum'] / v['opens'] if v['opens'] else None):>9}")
    hf, lf = by_class.get("ВЧ") or {}, by_class.get("НЧ") or {}
    if hf and lf and tot_s:
        r_hf = pct(hf.get("orders", 0), tot_o) or 0
        s_hf = pct(hf.get("spend", 0), tot_s) or 0
        if s_hf > r_hf + 15:
            log(f"  → ВЧ забирают {s_hf:.0f}% рекламы, а дают {r_hf:.0f}% заказов: перекос в дорогие запросы — переносить бюджет в НЧ/СЧ.")

    actions.sort(key=lambda x: (x[0], x[1]))
    if actions:
        log("\n  Что сделать (по важности):")
        last = None
        for prio, _, r in actions[:40]:
            if r["действие"] != last:
                log(f"   [{r['действие']}]")
                last = r["действие"]
            log(f"     {r['артикул WB']:>11} {cut(r['запрос'], 38):38} {r['класс']} {fm(r['запросов в неделю']):>8}/нед  {r['почему']}")
    return rows, [r for _, _, r in actions]


def main():
    cabs = load_tokens(arg_cabinet())
    log(f"Поисковые запросы WB: {len(cabs)} каб., последние 7 дней.")
    all_rows, all_act = [], []
    for cab, token in cabs.items():
        try:
            rows, act = analyze(cab, token)
        except ApiError as e:
            log(f"  ✗ {cab}: {e}")
            continue
        all_rows += rows
        all_act += act
    write_csv("wb_queries.csv", all_rows)
    write_csv("wb_queries_actions.csv", all_act)
    save_log("wb_queries_report.txt")
    print("\nСохранено: wb_queries_report.txt, wb_queries.csv, wb_queries_actions.csv")


if __name__ == "__main__":
    main()

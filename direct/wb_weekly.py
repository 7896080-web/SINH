# -*- coding: utf-8 -*-
"""
Недельные отчёты Wildberries о реализации — как идут дела неделя к неделе. Только чтение, в кабинете ничего не меняет.

Что показывает (по каждому кабинету):
  1. Последние недели (отчёты о реализации, как в кабинете WB «Финансовые отчёты»): продажи, к перечислению
     за товар, логистика, хранение, штрафы, удержания, итого к оплате — и изменение к прошлой неделе.
  2. Последняя неделя против предыдущей по детализации: выкуплено и возвращено штук, логистика на 1 продажу,
     сколько удержано за рекламу (оплата с «Баланса»).
  3. Товары: топ по продажам последней недели с прошлой неделей рядом; кто сильнее всего вырос и упал.

Отчёт за неделю WB формирует в понедельник (иногда к вечеру или во вторник). Если последней недели ещё нет —
скрипт покажет последнюю готовую и скажет об этом.
Отчёт о реализации — это ВЫКУПЫ и возвраты (деньги), а не заказы: заказы недели частично попадут в следующие отчёты.

Запуск:  python wb_weekly.py                     (последние 6 недель)
         python wb_weekly.py 10                  (10 недель)
         python wb_weekly.py --cabinet "Яворская"
Нужна категория токена «Финансы». WB отдаёт эти отчёты 1 раз в минуту — скрипт работает 2–4 минуты.
Можно запускать одновременно с wb_bids.py: это другой раздел API, лимиты не пересекаются.
Результат: вывод на экран + wb_weekly_report.txt, wb_weekly.csv
"""
import datetime as dt
from collections import defaultdict

from wb_common import (FIN, TODAY, ApiError, arg_cabinet, arg_int, call, cut, finance_rows, fm, load_tokens, log,
                       num, pace, pct, save_log, write_csv)

# ---------------- НАСТРОЙКИ ----------------
WEEKS = arg_int(6)        # сколько последних недель показать
TOP = 15                  # сколько товаров в топе
MOVERS = 7                # сколько товаров «выросли» / «упали»
# -------------------------------------------

LIST_FIELDS = [("retail", "retailAmountSum"), ("for_pay", "forPaySum"), ("delivery", "deliveryServiceSum"),
               ("storage", "paidStorageSum"), ("acceptance", "paidAcceptanceSum"), ("penalty", "penaltySum"),
               ("deduction", "deductionSum"), ("additional", "additionalPaymentSum"), ("bank", "bankPaymentSum")]


def d10(v):
    return str(v or "")[:10]


def week_key(r):
    """Неделя строки — с понедельника по воскресенье. WB режет неделю на границе месяца на два отчёта
    (например, 28–30.09 и 01–04.10) — здесь они складываются обратно в одну неделю."""
    try:
        d = dt.date.fromisoformat(d10(r.get("dateFrom")))
    except ValueError:
        return (d10(r.get("dateFrom")), d10(r.get("dateTo")))
    mon = d - dt.timedelta(days=d.weekday())
    return (mon.isoformat(), (mon + dt.timedelta(days=6)).isoformat())


def change(new, old):
    if not old:
        return ""
    return f"{(new - old) / abs(old) * 100:+.0f}%"


def week_list(token, d1, d2):
    """{(начало, конец): суммы} по списку отчётов реализации (основной + по выкупам складываются)."""
    pace(token, "finance_list", 61)
    data = call("POST", FIN + "/api/finance/v1/sales-reports/list", token, what="Список отчётов реализации",
                body={"dateFrom": d1.isoformat(), "dateTo": d2.isoformat(), "limit": 1000, "offset": 0,
                      "period": "weekly"}) or []
    weeks = defaultdict(lambda: defaultdict(float))
    last_to = ""
    for r in data if isinstance(data, list) else []:
        w = weeks[week_key(r)]
        last_to = max(last_to, d10(r.get("dateTo")))
        for k, f in LIST_FIELDS:
            w[k] += num(r.get(f))
        w["reports"] += 1
    return weeks, last_to


def detail(rows):
    """Детализация по неделям: {(начало, конец): {"total": {...}, "nm": {nm: {...}}, "info": {nm: (арт, название)}}}."""
    res = defaultdict(lambda: {"total": defaultdict(float), "nm": defaultdict(lambda: defaultdict(float)),
                               "ded": defaultdict(float)})
    info = {}
    for r in rows:
        wk = res[week_key(r)]
        t = wk["total"]
        nm = r.get("nmId") or 0
        doc = (r.get("docTypeName") or "").lower()
        q = num(r.get("quantity"))
        a = wk["nm"][nm]
        if nm:
            info.setdefault(nm, (r.get("vendorCode") or "", r.get("title") or r.get("subjectName") or ""))
        sign = 1 if doc.startswith("продаж") else -1 if doc.startswith("возврат") else 0
        if sign:
            a["sold" if sign > 0 else "returned"] += q
            a["retail"] += sign * num(r.get("retailAmount"))
            a["for_pay"] += sign * num(r.get("forPay"))
        if sign > 0:
            # средняя СПП (скидка WB покупателю) и итоговая комиссия WB, взвешенные по продажам
            w = num(r.get("retailAmount"))
            t["w"] += w
            t["spp_w"] += num(r.get("spp")) * w
            t["kvw_w"] += num(r.get("kvw")) * w
        logi = num(r.get("deliveryService")) + num(r.get("rebillLogisticCost"))
        a["logistics"] += logi
        t["logistics"] += logi
        ded = num(r.get("deduction"))
        reason = f"{r.get('bonusTypeName') or ''} {r.get('sellerOperName') or ''}".lower()
        if ded and ("продвиж" in reason or "реклам" in reason):
            t["ads"] += ded
        if ded:
            wk["ded"][cut(r.get("bonusTypeName") or r.get("sellerOperName") or "без названия", 60)] += ded
        if sign:
            t["sold" if sign > 0 else "returned"] += q
        t["retail"] += sign * num(r.get("retailAmount"))
        t["for_pay"] += sign * num(r.get("forPay"))
    return res, info


def analyze(cab, token):
    log(f"\n{'═' * 20} {cab} {'═' * 20}")
    # недели WB — с понедельника по воскресенье; последняя полная неделя кончается в прошлое воскресенье
    last_sunday = TODAY - dt.timedelta(days=TODAY.weekday() + 1)
    d1 = last_sunday - dt.timedelta(weeks=WEEKS) + dt.timedelta(days=1)
    print("  список отчётов о реализации (WB отдаёт 1 раз в минуту)...")
    try:
        weeks, last_to = week_list(token, d1, TODAY - dt.timedelta(days=1))
    except ApiError as e:
        log(f"  ! Список отчётов: {e}")
        weeks, last_to = {}, ""

    rows_csv = []
    keys = sorted(weeks)[-WEEKS:]
    if keys:
        log(f"\n  1. Недели по отчётам о реализации (как в кабинете WB, «Финансовые отчёты»), ₽:")
        log(f"  {'неделя':13}{'продажи':>11}{'к переч.':>11}{'логист.':>10}{'хранен.':>9}{'штрафы':>8}"
            f"{'удерж.':>9}{'к оплате':>11}{'комис.%':>8}{'логист.%':>9}{'к опл. Δ':>9}")
        prev = None
        for k in keys:
            w = weeks[k]
            log(f"  {k[0][8:10]}.{k[0][5:7]}–{k[1][8:10]}.{k[1][5:7]}  {fm(w['retail']):>11}{fm(w['for_pay']):>11}"
                f"{fm(w['delivery']):>10}{fm(w['storage']):>9}{fm(w['penalty']):>8}{fm(w['deduction']):>9}"
                f"{fm(w['bank']):>11}{fm(pct(w['retail'] - w['for_pay'], w['retail']), 0, '%'):>8}"
                f"{fm(pct(w['delivery'], w['retail']), 0, '%'):>9}"
                f"{change(w['bank'], prev['bank']) if prev else '':>9}")
            prev = w
        if last_to < last_sunday.isoformat():
            log(f"  ! Отчёта за прошлую неделю ({last_sunday - dt.timedelta(days=6):%d.%m}–"
                f"{last_sunday:%d.%m}) ещё нет — WB обычно формирует его в понедельник–вторник. "
                "Запустите позже.")
        log("  «комис.%» — сколько WB оставил себе от продаж (продажи − к перечислению); «логист.%» — логистика от продаж.")
        log("  «удерж.» — прочие удержания WB (в т.ч. оплата рекламы с «Баланса»); «к оплате» — что WB перечислит на счёт.")
    else:
        log("  Список отчётов пуст — беру детализацию за последние 2 недели.")

    # детализация двух последних недель
    if len(keys) >= 2:
        dd1, dd2 = dt.date.fromisoformat(keys[-2][0]), dt.date.fromisoformat(keys[-1][1])
    elif keys:
        dd1, dd2 = dt.date.fromisoformat(keys[-1][0]), dt.date.fromisoformat(keys[-1][1])
    else:
        dd2 = last_sunday
        dd1 = dd2 - dt.timedelta(days=13)
    print("  детализация двух последних недель (1 раз в минуту, может занять 2–3 мин)...")
    try:
        det, info = detail(finance_rows(token, dd1, dd2))
    except ApiError as e:
        log(f"  ! Детализация: {e}")
        return rows_csv
    wk = sorted(det)[-2:]
    if not wk:
        log("  Детализация пуста.")
        return rows_csv
    for w in wk:
        det[w]["total"]["commission"] = det[w]["total"]["retail"] - det[w]["total"]["for_pay"]
    cur = det[wk[-1]]
    old = det[wk[0]] if len(wk) == 2 else None
    name = lambda k: f"{k[0][8:10]}.{k[0][5:7]}–{k[1][8:10]}.{k[1][5:7]}"

    log(f"\n  2. Неделя {name(wk[-1])}" + (f" против {name(wk[0])}" if old else "") + " (детализация):")
    def line(title, key, per=None, unit=" ₽"):
        c = cur["total"][key] / (cur["total"][per] or 1) if per else cur["total"][key]
        o = (old["total"][key] / (old["total"][per] or 1) if per else old["total"][key]) if old else None
        log(f"    {title:38}{fm(c, 0, unit):>14}" + (f"{fm(o, 0, unit):>14}{change(c, o):>8}" if old else ""))
    line("Выкуплено", "sold", unit=" шт")
    line("Возвратов", "returned", unit=" шт")
    line("Продажи (за вычетом возвратов)", "retail")
    line("Комиссия WB (продажи − к перечислению)", "commission")
    line("СПП — скидка WB покупателю, в среднем", "spp_w", per="w", unit="%")
    line("Комиссия WB (кВВ без НДС), в среднем", "kvw_w", per="w", unit="%")
    line("Логистика всего", "logistics")
    line("Логистика на 1 выкуп", "logistics", per="sold")
    line("Удержано за рекламу (с «Баланса»)", "ads")
    if cur["total"]["ads"] or (old and old["total"]["ads"]):
        log(f"    Реклама от продаж: {fm(pct(cur['total']['ads'], cur['total']['retail']), 1, '%')}"
            + (f" (неделей раньше {fm(pct(old['total']['ads'], old['total']['retail']), 1, '%')})" if old else "")
            + ". Реклама, оплаченная со «Счёта» (картой), сюда не попадает — её смотрите в wb_ads_report.py.")

    if cur["ded"] or (old and old["ded"]):
        log("    Из чего состоят удержания (₽, эта неделя / прошлая):")
        for k in sorted(set(cur["ded"]) | (set(old["ded"]) if old else set()), key=lambda k: -abs(cur["ded"].get(k, 0)))[:10]:
            log(f"      {cut(k, 60):60}{fm(cur['ded'].get(k, 0)):>12}" + (f"{fm(old['ded'].get(k, 0)):>12}" if old else ""))

    nms = set(cur["nm"]) | (set(old["nm"]) if old else set())
    nms.discard(0)
    for nm in nms:
        c, o = cur["nm"].get(nm) or defaultdict(float), (old["nm"].get(nm) if old else None) or defaultdict(float)
        rows_csv.append({
            "кабинет": cab, "артикул WB": nm, "артикул продавца": (info.get(nm) or ("", ""))[0],
            "название": (info.get(nm) or ("", ""))[1],
            "неделя": name(wk[-1]), "выкуплено шт": int(c["sold"]), "возвратов шт": int(c["returned"]),
            "продажи ₽": round(c["retail"], 2), "к перечислению ₽": round(c["for_pay"], 2),
            "логистика ₽": round(c["logistics"], 2),
            "прошлая неделя": name(wk[0]) if old else "", "выкуплено шт (пр.)": int(o["sold"]),
            "продажи ₽ (пр.)": round(o["retail"], 2), "изменение продаж ₽": round(c["retail"] - o["retail"], 2),
        })
    rows_csv.sort(key=lambda r: -r["продажи ₽"])

    log(f"\n  3. Товары — топ-{TOP} по продажам недели {name(wk[-1])}:")
    log(f"    {'артикул':>11} {'арт. продавца':18}{'выкуп':>6}{'возвр':>6}{'продажи':>10}{'пр. нед.':>10}{'Δ':>7}{'логист/шт':>10}")
    for r in rows_csv[:TOP]:
        log(f"    {r['артикул WB']:>11} {cut(r['артикул продавца'], 18):18}{r['выкуплено шт']:>6}{r['возвратов шт']:>6}"
            f"{fm(r['продажи ₽']):>10}{fm(r['продажи ₽ (пр.)']):>10}{change(r['продажи ₽'], r['продажи ₽ (пр.)']):>7}"
            f"{fm(r['логистика ₽'] / r['выкуплено шт']) if r['выкуплено шт'] else '':>10}")
    if old:
        up = sorted((r for r in rows_csv if r["изменение продаж ₽"] > 0), key=lambda r: -r["изменение продаж ₽"])[:MOVERS]
        down = sorted((r for r in rows_csv if r["изменение продаж ₽"] < 0), key=lambda r: r["изменение продаж ₽"])[:MOVERS]
        for title, lst in (("Выросли сильнее всего", up), ("Упали сильнее всего", down)):
            log(f"\n    {title}:")
            if not lst:
                log("      нет")
            for r in lst:
                log(f"      {r['артикул WB']:>11} {cut(r['артикул продавца'], 18):18} {cut(r['название'], 28):28} "
                    f"{fm(r['продажи ₽ (пр.)'])} → {fm(r['продажи ₽'])} ₽ ({fm(r['изменение продаж ₽'], 0, ' ₽')}), "
                    f"выкуплено {r['выкуплено шт (пр.)']} → {r['выкуплено шт']} шт")
    log("\n  Выводы по одной неделе предварительные: выкупы приходят с задержкой после заказа, "
        "на них влияют остатки и сезон, а не только реклама.")
    return rows_csv


def main():
    cabs = load_tokens(arg_cabinet())
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — недельные отчёты WB о реализации, последние {WEEKS} нед. "
        "(только чтение, в кабинете ничего не меняется)")
    allrows = []
    for cab, token in cabs.items():
        try:
            allrows += analyze(cab, token)
        except ApiError as e:
            log(f"  ✗ {cab}: {e}")
    write_csv("wb_weekly.csv", allrows, header=list(allrows[0]) if allrows else None)
    save_log("wb_weekly_report.txt")
    print("\nСохранено: wb_weekly_report.txt, wb_weekly.csv")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
Юнит-экономика Wildberries по каждому артикулу — по отчётам реализации (реальные удержания WB).

Считает на 1 проданную (выкупленную) единицу:
  цена продажи → к перечислению от WB (после комиссии и эквайринга) → минус логистика (включая
  возвраты и невыкупы), хранение, приёмка, штрафы → минус себестоимость и налоги = прибыль.
Затем прибыль на 1 ЗАКАЗ (с учётом процента выкупа) и сколько можно платить рекламе за заказ:
  «окупаемость» — вся прибыль с заказа; «лимит» — её доля AD_PROFIT_SHARE (по умолчанию половина).
Лимит пишется в wb_unit_economics.csv — автопилот wb_bids.py берёт его, если в правиле
указано "max_cost_per_order": "auto".

Себестоимость — из файла wb_cost.csv (создаётся при первом запуске: впишите себестоимость
в последнюю колонку). Пока её нет — берётся DEFAULT_COGS_SHARE от цены продажи (помечено «оценка»).

Запуск:  python wb_unit.py            — за 35 дней
         python wb_unit.py 60         — за 60 дней
         python wb_unit.py --cabinet "Яворская"
Нужны категории токена: «Финансы», «Продвижение», «Аналитика».
Результат: wb_unit_report.txt, wb_unit_economics.csv (строки других кабинетов при --cabinet сохраняются)
"""
import datetime as dt
from collections import defaultdict

from wb_common import (ApiError, TODAY, ad_stats, arg_cabinet, arg_int, campaigns, cut, finance_rows, fm,
                       funnel, load_tokens, log, mature_buyout, num, pct, read_csv, save_log, to_float, write_csv,
                       each_cabinet)

# ======================== НАСТРОЙКИ ========================
TAX_SHARE = 0.105         # налоги от выручки: УСН «доходы» 6% + НДС 5% ≈ 10,5% — уточните у бухгалтера
DEFAULT_COGS_SHARE = 0.37 # себестоимость, если её нет в wb_cost.csv, — доля от цены продажи
AD_PROFIT_SHARE = 0.5     # какую часть прибыли с заказа можно отдать рекламе (лимит для автопилота)
# ===========================================================

DAYS = arg_int(35)
D2 = TODAY - dt.timedelta(days=1)
D1 = D2 - dt.timedelta(days=DAYS - 1)


def load_costs():
    by_nm, by_vendor = {}, {}
    for r in read_csv("wb_cost.csv"):
        c = to_float(r.get("себестоимость"))
        if not c:
            continue
        if r.get("артикул WB", "").strip().isdigit():
            by_nm[int(r["артикул WB"])] = c
        if r.get("артикул продавца"):
            by_vendor[r["артикул продавца"].strip()] = c
    return by_nm, by_vendor


def merge_unit_rows(old_rows, new_rows, done_cabs):
    """Строки кабинетов из done_cabs заменяются новыми, строки остальных кабинетов остаются как были
    (запуск с --cabinet или сбой одного кабинета не стирает чужую юнит-экономику)."""
    keep = [{k: v for k, v in r.items() if k} for r in old_rows if r.get("кабинет") not in done_cabs]
    return keep + list(new_rows)


def ensure_cost_template(products):
    rows = read_csv("wb_cost.csv")
    existing = {r.get("артикул WB") for r in rows}
    added = 0
    for nm, p in sorted(products.items(), key=lambda x: str(x[1].get("vendor"))):
        if str(nm) not in existing:
            rows.append({"кабинет": p.get("cab", ""), "артикул WB": str(nm), "артикул продавца": p.get("vendor") or "",
                         "название": p.get("title") or "", "себестоимость": ""})
            added += 1
    if added:
        # свои колонки пользователя (если дописал) сохраняются; себестоимость не трогаем
        base = ("кабинет", "артикул WB", "артикул продавца", "название", "себестоимость")
        write_csv("wb_cost.csv", [{**{k: r.get(k) or "" for k in base},
                                   **{k: v for k, v in r.items() if k and k not in base}} for r in rows])
    return added


def analyze(cab, token, costs_nm, costs_vendor):
    log(f"\n{'═' * 20} {cab} {'═' * 20}")
    print(f"  {cab}: отчёт реализации (WB отдаёт 1 раз в минуту)...", flush=True)
    rows = finance_rows(token, D1, D2)
    if not rows:
        log("  Отчёт реализации за период пуст — возможно, отчёты за последние дни ещё не сформированы.")
        return []
    A = defaultdict(lambda: defaultdict(float))
    info = {}
    general = defaultdict(float)
    ad_ded = 0.0
    for r in rows:
        nm = r.get("nmId") or 0
        doc = (r.get("docTypeName") or "").lower()
        q = num(r.get("quantity"))
        a = A[nm]
        if nm:
            info.setdefault(nm, {"vendor": r.get("vendorCode"), "title": r.get("title"), "subject": r.get("subjectName")})
        if doc.startswith("продаж"):
            a["sold"] += q
            a["retail"] += num(r.get("retailAmount"))
            a["for_pay"] += num(r.get("forPay"))
        elif doc.startswith("возврат"):
            a["returned"] += q
            a["retail"] -= num(r.get("retailAmount"))
            a["for_pay"] -= num(r.get("forPay"))
        a["logistics"] += num(r.get("deliveryService")) + num(r.get("rebillLogisticCost"))
        a["storage"] += num(r.get("paidStorage"))
        a["acceptance"] += num(r.get("paidAcceptance"))
        a["penalty"] += num(r.get("penalty"))
        ded = num(r.get("deduction"))
        reason = f"{r.get('bonusTypeName') or ''} {r.get('sellerOperName') or ''}".lower()
        if ded and ("продвиж" in reason or "реклам" in reason):
            ad_ded += ded
        else:
            a["deduction"] += ded
        a["other"] += num(r.get("additionalPayment"))

    # общие расходы без артикула (хранение, удержания) распределяем пропорционально выручке
    g = A.pop(0, defaultdict(float))
    overhead = g["logistics"] + g["storage"] + g["acceptance"] + g["penalty"] + g["deduction"] - g["other"]
    tot_retail = sum(max(v["retail"], 0) for v in A.values())

    print(f"  {cab}: выкуп по артикулам и реклама...", flush=True)
    try:
        buy, buy_avg = mature_buyout(token)
    except ApiError as e:
        log(f"  ! {e} — выкуп возьму 40% для всех.")
        buy, buy_avg = {}, 0.4
    try:
        # реклама — за тот же период, что покрывают отчёты реализации (последняя неделя часто ещё не готова)
        p1 = max(min((str(r.get("dateFrom") or "")[:10] for r in rows if r.get("dateFrom")), default=D1.isoformat()), D1.isoformat())
        p2 = min(max((str(r.get("dateTo") or "")[:10] for r in rows if r.get("dateTo")), default=D2.isoformat()), D2.isoformat())
        a1, a2 = dt.date.fromisoformat(p1), dt.date.fromisoformat(p2)
        if (a1, a2) != (D1, D2):
            log(f"  Отчёты реализации покрывают {a1:%d.%m}–{a2:%d.%m} — рекламу беру за тот же период.")
        camps = campaigns(token, changed_since=a1.isoformat())
        _, by_cn = ad_stats(token, list(camps), a1, a2) if camps else ({}, {})
    except ApiError as e:
        log(f"  ! {e} — без рекламы.")
        by_cn = {}
    ad = defaultdict(lambda: defaultdict(float))
    for (aid, nm), v in by_cn.items():
        for k in ("sum", "orders", "sum_price"):
            ad[nm][k] += v[k]

    res = []
    T = defaultdict(float)
    for nm, v in A.items():
        net = v["sold"] - v["returned"]
        share = max(v["retail"], 0) / tot_retail if tot_retail else 0
        alloc = overhead * share
        costs = v["logistics"] + v["storage"] + v["acceptance"] + v["penalty"] + v["deduction"] + alloc - v["other"]
        payout = v["for_pay"] - costs
        price = v["retail"] / net if net > 0 else None
        cogs_u = costs_nm.get(nm) or costs_vendor.get((info.get(nm) or {}).get("vendor") or "")
        est = cogs_u is None
        if est and price:
            cogs_u = price * DEFAULT_COGS_SHARE
        cogs = (cogs_u or 0) * max(net, 0)
        tax = TAX_SHARE * max(v["retail"], 0)
        profit = payout - cogs - tax
        b = buy.get(nm)                       # 0 — товар реально не выкупают; «нет данных» — None
        b = buy_avg if b is None else b
        b = 0.4 if b is None else b
        pu = profit / net if net > 0 else None
        po = pu * b if pu is not None else None
        s = ad[nm]
        cpo_fact = s["sum"] / s["orders"] if s["orders"] else None
        lim = max(po, 0) * AD_PROFIT_SHARE if po is not None else None
        for k, val in (("retail", v["retail"]), ("for_pay", v["for_pay"]), ("costs", costs), ("cogs", cogs),
                       ("tax", tax), ("profit", profit), ("ads", s["sum"]), ("sold", v["sold"]), ("returned", v["returned"])):
            T[k] += val
        res.append({
            "кабинет": cab, "артикул WB": nm, "артикул продавца": (info.get(nm) or {}).get("vendor") or "",
            "название": (info.get(nm) or {}).get("title") or "", "продано шт": int(v["sold"]), "возвратов шт": int(v["returned"]),
            "выручка ₽": round(v["retail"], 2), "цена ₽": round(price, 2) if price else "",
            "к перечислению на шт": round(v["for_pay"] / net, 2) if net > 0 else "",
            "логистика+хранение+прочее на шт": round(costs / net, 2) if net > 0 else "",
            "себестоимость шт": round(cogs_u, 2) if cogs_u else "", "себестоимость": "оценка" if est else "из файла",
            "налог на шт": round(tax / net, 2) if net > 0 else "",
            "прибыль на шт": round(pu, 2) if pu is not None else "", "маржа %": round(pct(profit, v["retail"]), 1) if v["retail"] > 0 else "",
            "выкуп %": round(b * 100, 1), "прибыль на заказ": round(po, 2) if po is not None else "",
            "окупаемая цена заказа": round(max(po, 0), 2) if po is not None else "",
            "лимит цены заказа": round(lim, 2) if lim is not None else "",
            "реклама ₽": round(s["sum"], 2), "заказов с рекламы": int(s["orders"]),
            "цена заказа с рекламы факт": round(cpo_fact, 2) if cpo_fact else "",
            "прибыль после рекламы ₽": round(profit - s["sum"], 2),
            "дата расчёта": TODAY.isoformat(),
        })
    res.sort(key=lambda r: -r["выручка ₽"])

    log(f"Период {D1:%d.%m}–{D2:%d.%m.%Y} ({DAYS} дн.), по отчётам реализации WB.")
    log(f"  {'Продано / возвратов':40}{fm(T['sold']) + ' / ' + fm(T['returned']) + ' шт':>22}")
    log(f"  {'Выручка (реализовано)':40}{fm(T['retail'], 0, ' ₽'):>22}")
    log(f"  {'К перечислению от WB (после комиссии)':40}{fm(T['for_pay'], 0, ' ₽'):>22}  {fm(pct(T['for_pay'], T['retail']), 0, '%')} от выручки")
    log(f"  {'Логистика, хранение, штрафы, удержания':40}{fm(T['costs'], 0, ' ₽'):>22}  {fm(pct(T['costs'], T['retail']), 0, '%')}")
    log(f"  {'Себестоимость':40}{fm(T['cogs'], 0, ' ₽'):>22}  {fm(pct(T['cogs'], T['retail']), 0, '%')}")
    log(f"  {'Налоги':40}{fm(T['tax'], 0, ' ₽'):>22}  {fm(pct(T['tax'], T['retail']), 0, '%')}")
    log(f"  {'Прибыль до рекламы':40}{fm(T['profit'], 0, ' ₽'):>22}  {fm(pct(T['profit'], T['retail']), 0, '%')}")
    log(f"  {'Реклама (статистика кампаний)':40}{fm(T['ads'], 0, ' ₽'):>22}  {fm(pct(T['ads'], T['retail']), 0, '%')}"
        + (f"; удержано за продвижение в отчёте {fm(ad_ded, 0, ' ₽')}" if ad_ded else ""))
    log(f"  {'ПРИБЫЛЬ ПОСЛЕ РЕКЛАМЫ':40}{fm(T['profit'] - T['ads'], 0, ' ₽'):>22}  {fm(pct(T['profit'] - T['ads'], T['retail']), 0, '%')}")
    n_est = sum(1 for r in res if r["себестоимость"] == "оценка" and r["продано шт"])
    if n_est:
        log(f"  ! Себестоимость для {n_est} артикулов — оценка ({DEFAULT_COGS_SHARE:.0%} от цены). Впишите реальную в wb_cost.csv.")

    log(f"\n  Артикулы (топ-30 по выручке), суммы на 1 проданную штуку:")
    log(f"  {'артикул':>11} {'арт. продавца':18}{'прод.':>6}{'цена':>8}{'от WB':>8}{'логист.':>8}{'себест.':>8}{'налог':>7}"
        f"{'прибыль':>9}{'выкуп':>6}{'на заказ':>9}{'лимит':>7}{'рекл/зак':>9}")
    for r in res[:30]:
        log(f"  {r['артикул WB']:>11} {cut(r['артикул продавца'], 18):18}{r['продано шт']:>6}"
            f"{fm(to_float(r['цена ₽'])):>8}{fm(to_float(r['к перечислению на шт'])):>8}"
            f"{fm(to_float(r['логистика+хранение+прочее на шт'])):>8}"
            f"{fm(to_float(r['себестоимость шт'])) + ('*' if r['себестоимость'] == 'оценка' else ' '):>8}"
            f"{fm(to_float(r['налог на шт'])):>7}{fm(to_float(r['прибыль на шт'])):>9}{fm(r['выкуп %'], 0, '%'):>6}"
            f"{fm(to_float(r['прибыль на заказ'])):>9}{fm(to_float(r['лимит цены заказа'])):>7}"
            f"{fm(to_float(r['цена заказа с рекламы факт'])):>9}")
    log("  * — себестоимость оценочная. «лимит» — сколько рекламы можно платить за 1 заказ, оставляя себе "
        f"{1 - AD_PROFIT_SHARE:.0%} прибыли.")

    loss = [r for r in res if r["продано шт"] >= 3 and to_float(r["прибыль на шт"]) is not None and to_float(r["прибыль на шт"]) < 0]
    if loss:
        log(f"\n  Убыточные даже без рекламы ({len(loss)}): поднять цену, снять с рекламы или вывести из ассортимента:")
        for r in loss[:15]:
            log(f"    {r['артикул WB']:>11} {cut(r['артикул продавца'], 20):20} {cut(r['название'], 30):30} "
                f"прибыль {fm(to_float(r['прибыль на шт']))} ₽/шт, логистика {fm(to_float(r['логистика+хранение+прочее на шт']))} ₽/шт")
    over = [r for r in res if to_float(r["цена заказа с рекламы факт"]) and to_float(r["окупаемая цена заказа"]) is not None
            and to_float(r["цена заказа с рекламы факт"]) > to_float(r["окупаемая цена заказа"]) and r["заказов с рекламы"] >= 3]
    if over:
        log(f"\n  Реклама съедает всю прибыль ({len(over)}): заказ с рекламы дороже, чем приносит прибыли:")
        for r in over[:15]:
            log(f"    {r['артикул WB']:>11} {cut(r['артикул продавца'], 20):20} заказ с рекламы {fm(to_float(r['цена заказа с рекламы факт']))} ₽, "
                f"окупаемо до {fm(to_float(r['окупаемая цена заказа']))} ₽")
    return res


def main():
    cabs = load_tokens(arg_cabinet())
    costs_nm, costs_vendor = load_costs()
    log(f"Юнит-экономика WB: {len(cabs)} каб., {DAYS} дн. Налоги {TAX_SHARE:.1%} от выручки, "
        f"себестоимость по умолчанию {DEFAULT_COGS_SHARE:.0%} от цены.")
    allrows, products, done = [], {}, set()
    # кабинеты опрашиваются одновременно
    for cab, rows, err in each_cabinet(cabs, lambda c, t: analyze(c, t, costs_nm, costs_vendor)):
        if err is not None:
            log(f"  ✗ {cab}: {err} — прежняя юнит-экономика кабинета в wb_unit_economics.csv сохранена.")
            continue
        if not rows:
            log(f"  Прежняя юнит-экономика кабинета «{cab}» в wb_unit_economics.csv сохранена.")
            continue
        done.add(cab)
        allrows += rows
        for r in rows:
            products[r["артикул WB"]] = {"cab": cab, "vendor": r["артикул продавца"], "title": r["название"]}
    write_csv("wb_unit_economics.csv", merge_unit_rows(read_csv("wb_unit_economics.csv"), allrows, done))
    added = ensure_cost_template(products)
    if added:
        log(f"\nВ wb_cost.csv добавлено артикулов без себестоимости: {added}. Откройте файл в Excel, впишите "
            "себестоимость за 1 шт. в последнюю колонку и запустите скрипт снова.")
    save_log("wb_unit_report.txt")
    print("\nСохранено: wb_unit_report.txt, wb_unit_economics.csv, wb_cost.csv")


if __name__ == "__main__":
    main()

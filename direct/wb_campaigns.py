# -*- coding: utf-8 -*-
"""
Снимок рекламных кампаний Wildberries: какие кампании есть, какого типа, на какие товары, со ставками,
бюджетами, дневными лимитами, минус-фразами и результатом за 7 дней. Только чтение — в кабинете ничего не меняет.
Каждый запуск сохраняет снимок с датой в папку wb_snapshots\\ — так фиксируется состояние «как было» на день.

Что показывает (по каждому кабинету):
  1. Кампании: статус, тип ставки (единая / ручная), оплата (за показы / за клики), места (поиск / рекомендации),
     даты создания и запуска, остаток бюджета, дневной лимит, расход и заказы за 7 дней.
  2. Товары каждой кампании: артикул, артикул продавца, предмет, ставка, остаток WB/свой склад, минус-фраз.
  3. На что обратить внимание: один товар в нескольких активных кампаниях на одном месте показа, реклама товаров
     без остатка, активные кампании без дневного лимита, расход без заказов.

Запуск:  python wb_campaigns.py                        (только активные кампании)
         python wb_campaigns.py --all                  (плюс «пауза», «готова к запуску» и завершённые за 30 дней)
         python wb_campaigns.py --cabinet "Яворская"
Нужны категории токена: «Продвижение», «Аналитика».
Результат: wb_campaigns_report.txt, wb_campaigns.csv, wb_campaigns_products.csv,
           wb_snapshots\\campaigns_<кабинет>_<дата_время>.json (полный снимок для сравнения потом)
"""
import datetime as dt
import json
import sys
from collections import defaultdict

from wb_common import (ADV, HERE, TODAY, YESTERDAY, ApiError, ad_stats, arg_cabinet, call, chunks, cut, fm, funnel,
                       load_tokens, log, num, pace, save_log, write_csv)

STATUS = {4: "готова к запуску", 9: "активна", 11: "пауза", 7: "завершена"}
PAY = {"cpm": "за показы", "cpc": "за клики"}
BID = {"unified": "единая ставка", "manual": "ручная ставка"}
SNAP_DIR = HERE / "wb_snapshots"


def rub(kop):
    return None if kop is None else num(kop) / 100


def list_campaigns(token, with_finished):
    pace(token, "count", 0.3)
    data = call("GET", ADV + "/adv/v1/promotion/count", token, what="Список кампаний") or {}
    since = (TODAY - dt.timedelta(days=30)).isoformat()
    ids, status = [], {}
    for g in data.get("adverts") or []:
        st = g.get("status")
        for a in g.get("advert_list") or []:
            if st == 9 or (with_finished and (st in (4, 11) or (st == 7 and str(a.get("changeTime") or "")[:10] >= since))):
                ids.append(a["advertId"])
                status[a["advertId"]] = st
    camps = {}
    for part in chunks(ids, 50):
        pace(token, "adverts", 0.3)
        d = call("GET", ADV + "/api/advert/v2/adverts", token, params={"ids": ",".join(map(str, part))},
                 what="Настройки кампаний") or {}
        for a in d.get("adverts") or []:
            if a.get("id") in status:
                a["_status"] = status[a["id"]]
                camps[a["id"]] = a
    return camps


def budgets(token, ids):
    res = {}
    for part in chunks(ids, 50):
        pace(token, "budget", 3.1)
        try:
            d = call("POST", ADV + "/api/advert/v2/budget", token, body={"advertIds": part}, what="Бюджеты") or {}
        except ApiError as e:
            log(f"  ! {e}")
            continue
        for b in d.get("adverts") or []:
            res[b.get("advertId")] = b.get("total")
    return res


def limits(token, ids):
    res = {}
    for part in chunks(ids, 100):
        pace(token, "limits", 12.5)
        try:
            d = call("GET", ADV + "/api/advert/v0/daily-limits", token, params={"advertIds": ",".join(map(str, part))},
                     what="Дневные лимиты") or {}
        except ApiError as e:
            log(f"  ! {e}")
            continue
        for a in d.get("adverts") or []:
            res[a.get("advertId")] = a
    return res


def minus_counts(token, pairs):
    res = {}
    for part in chunks(pairs, 100):
        pace(token, "nq", 0.25)
        try:
            d = call("POST", ADV + "/adv/v0/normquery/get-minus", token,
                     body={"items": [{"advert_id": a, "nm_id": n} for a, n in part]}, what="Минус-фразы") or {}
        except ApiError as e:
            log(f"  ! {e}")
            return None
        for it in d.get("items") or []:
            if isinstance(it.get("norm_queries"), list):
                res[(it.get("advert_id"), it.get("nm_id"))] = len(it["norm_queries"])
    return res


def run_cabinet(cab, token, with_finished):
    log(f"\n══════════ {cab} ══════════")
    camps = list_campaigns(token, with_finished)
    if not camps:
        log("  Кампаний нет.")
        return [], []
    ids = list(camps)
    nms = sorted({s.get("nm_id") for a in camps.values() for s in a.get("nm_settings") or [] if s.get("nm_id")})
    log(f"  Кампаний: {len(ids)}, товаров в них: {len(nms)}. Загружаю бюджеты, лимиты, минус-фразы, статистику…")
    bud = budgets(token, [i for i in ids if camps[i]["_status"] in (4, 9, 11)])
    lim = limits(token, [i for i in ids if camps[i]["_status"] in (4, 9, 11)])
    pairs = [(i, s.get("nm_id")) for i, a in camps.items() for s in a.get("nm_settings") or []]
    mc = minus_counts(token, pairs)
    d1 = YESTERDAY - dt.timedelta(days=6)
    stat_ids = [i for i in ids if camps[i]["_status"] in (7, 9, 11)]
    try:
        _, by_camp = ad_stats(token, stat_ids, d1, YESTERDAY) if stat_ids else ({}, {})
    except ApiError as e:
        log(f"  ! {e}")
        by_camp = {}
    try:
        info = funnel(token, YESTERDAY, YESTERDAY, nms) if nms else {}
    except ApiError as e:
        log(f"  ! {e}")
        info = {}

    c_rows, p_rows = [], []
    nm_places = defaultdict(list)     # (nm, место) → активные кампании
    warn = []
    order = sorted(ids, key=lambda i: ({9: 0, 11: 1, 4: 2, 7: 3}.get(camps[i]["_status"], 9), str(camps[i]["settings"].get("name"))))
    log(f"\n=== 1. Кампании ===")
    for i in order:
        a = camps[i]
        st = a.get("settings") or {}
        pl = st.get("placements") or {}
        places = [p for p in ("search", "recommendations") if pl.get(p)] or (["поиск+рекомендации"] if a.get("bid_type") == "unified" else [])
        places_ru = ", ".join({"search": "поиск", "recommendations": "рекомендации"}.get(p, p) for p in places) or "—"
        ts = a.get("timestamps") or {}
        s7 = defaultdict(float)
        for (aid, _nm), v in by_camp.items():
            if aid == i:
                for k in ("views", "clicks", "sum", "orders"):
                    s7[k] += v[k]
        l = lim.get(i) or {}
        lim_txt = (fm(rub(l.get("dailyLimit"))) + " ₽") if l.get("enabled") else ("выключен" if l else "—")
        nm_list = [s.get("nm_id") for s in a.get("nm_settings") or []]
        row = {"кабинет": cab, "ID": i, "название": st.get("name"), "статус": STATUS.get(a["_status"], a["_status"]),
               "тип ставки": BID.get(a.get("bid_type"), a.get("bid_type")), "оплата": PAY.get(st.get("payment_type"), st.get("payment_type")),
               "места": places_ru, "создана": str(ts.get("created") or "")[:10], "запущена": str(ts.get("started") or "")[:10],
               "товаров": len(nm_list), "бюджет ₽": bud.get(i, ""), "дневной лимит": lim_txt,
               "расход 7 дн ₽": round(s7["sum"]), "клики 7 дн": round(s7["clicks"]), "заказы 7 дн": round(s7["orders"]),
               "заказ ₽": round(s7["sum"] / s7["orders"]) if s7["orders"] else ""}
        c_rows.append(row)
        log(f"\n  ▶ {st.get('name')} (ID {i}) — {row['статус']}; {row['тип ставки']}, {row['оплата']}, {places_ru}")
        log(f"    создана {row['создана'] or '—'}, запущена {row['запущена'] or '—'}; бюджет {fm(bud.get(i))} ₽; "
            f"дневной лимит {lim_txt}; за 7 дн: расход {fm(s7['sum'])} ₽, клики {fm(s7['clicks'])}, заказы {fm(s7['orders'])}"
            + (f", заказ {fm(s7['sum'] / s7['orders'])} ₽" if s7["orders"] else ""))
        if a["_status"] == 9 and not l.get("enabled") and st.get("payment_type") == "cpc":
            warn.append(f"«{st.get('name')}»: активна, оплата за клики, дневной лимит выключен.")
        if a["_status"] == 9 and s7["sum"] >= 1000 and not s7["orders"]:
            warn.append(f"«{st.get('name')}»: за 7 дней {fm(s7['sum'])} ₽ без заказов.")
        log(f"    {'артикул':>11} {'арт. продавца':18}{'предмет':16}{'ставка ₽':>15}{'WB/свой':>10}{'минус':>7}")
        for s in a.get("nm_settings") or []:
            nm = s.get("nm_id")
            b = s.get("bids_kopecks") or {}
            nf = info.get(nm) or {}
            bid_txt = " / ".join(f"{fm(rub(b[k]), 2)}" for k in ("search", "recommendations") if b.get(k) is not None) or "—"
            stock = f"{'' if nf.get('stock_wb') is None else int(nf['stock_wb'])}/{'' if nf.get('stock_mp') is None else int(nf['stock_mp'])}"
            mcount = "" if mc is None else mc.get((i, nm), "нет")
            log(f"    {nm:>11} {cut(nf.get('vendor'), 18):18}{cut((s.get('subject') or {}).get('name') or nf.get('subject'), 16):16}"
                f"{bid_txt:>15}{stock:>10}{str(mcount):>7}")
            p_rows.append({"кабинет": cab, "кампания": st.get("name"), "ID кампании": i, "статус": row["статус"],
                           "артикул WB": nm, "артикул продавца": nf.get("vendor") or "", "название": nf.get("title") or "",
                           "предмет": (s.get("subject") or {}).get("name") or nf.get("subject") or "",
                           "ставка поиск ₽": rub(b.get("search")) if b.get("search") is not None else "",
                           "ставка рекомендации ₽": rub(b.get("recommendations")) if b.get("recommendations") is not None else "",
                           "остаток WB": nf.get("stock_wb", ""), "остаток свой": nf.get("stock_mp", ""),
                           "минус-фраз": mcount})
            if a["_status"] == 9:
                for p in (("search", "recommendations") if a.get("bid_type") == "unified" else places):
                    nm_places[(nm, {"search": "поиск", "recommendations": "рекомендации"}.get(p, p))].append(st.get("name"))
                if (nf.get("stock_wb") or 0) + (nf.get("stock_mp") or 0) <= 0 and nf:
                    warn.append(f"«{st.get('name')}»: товар {nm} ({nf.get('vendor')}) без остатка ни на WB, ни на своём складе.")
    for (nm, p), names in nm_places.items():
        if len(names) > 1:
            warn.append(f"Товар {nm} в {len(names)} активных кампаниях на одном месте ({p}): {', '.join(names)} — "
                        "по правилу «один товар, одно место — одна кампания» оставить одну.")
    if mc is not None and not mc:
        log("\n  Минус-фраз WB не вернул ни по одной кампании — у кампаний нет минус-фраз или их тип их не поддерживает.")
    log("\n=== 2. На что обратить внимание ===")
    for w in warn or ["Ничего подозрительного."]:
        log(f"  • {w}")

    SNAP_DIR.mkdir(exist_ok=True)
    snap = SNAP_DIR / f"campaigns_{cab}_{dt.datetime.now():%Y%m%d_%H%M}.json"
    snap.write_text(json.dumps({"cabinet": cab, "at": dt.datetime.now().isoformat(timespec="seconds"),
                                "campaigns": c_rows, "products": p_rows,
                                "raw": {str(k): v for k, v in camps.items()}}, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    log(f"\n  Снимок сохранён: {snap.relative_to(HERE)}")
    return c_rows, p_rows


def main():
    with_finished = "--all" in sys.argv
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — снимок рекламных кампаний WB (только чтение)")
    c_all, p_all = [], []
    for cab, token in load_tokens(arg_cabinet()).items():
        try:
            c, p = run_cabinet(cab, token, with_finished)
        except ApiError as e:
            log(f"  ✗ {cab}: {e}")
            continue
        c_all += c
        p_all += p
    write_csv("wb_campaigns.csv", c_all, header=["кабинет", "ID", "название"] if not c_all else None)
    write_csv("wb_campaigns_products.csv", p_all, header=["кабинет", "кампания", "артикул WB"] if not p_all else None)
    save_log("wb_campaigns_report.txt")
    print("\nСохранено: wb_campaigns_report.txt, wb_campaigns.csv, wb_campaigns_products.csv, wb_snapshots\\")


if __name__ == "__main__":
    main()

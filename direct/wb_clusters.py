# -*- coding: utf-8 -*-
"""
Поисковые кластеры рекламных кампаний WB — что покупает каждая кампания и какие кластеры стоит исключить.
Только чтение: в кабинете ничего не меняет.

Что известно из документации WB (API «Продвижение», раздел «Поисковые кластеры»):
  • статистика по кластерам (/adv/v0/normquery/stats) — для кампаний за показы и за клики; у кампаний за клики
    WB не отдаёт показы, CTR и CPM (только клики, корзины, заказы, расход, средняя позицию) — а на практике по
    кампаниям за клики данных часто нет совсем;
  • списки кластеров товара (/adv/v0/normquery/list): активные, исключённые и архивные — по которым было ≥ 100 показов;
  • исключение кластеров = минус-фразы (/adv/v0/normquery/get-minus, set-minus) — у кампаний за показы
    (единая и ручная ставка); запись заменяет весь список товара;
  • ставки на отдельные кластеры (/adv/v0/normquery/get-bids, /api/advert/v1/normquery/bids) — только у кампаний
    за показы с РУЧНОЙ ставкой.

Что показывает (по каждому кабинету, за последние N дней, по умолчанию 14):
  1. Для каждой кампании: тип оплаты и ставки, сколько кластеров активно / исключено, отдаёт ли WB статистику.
  2. По каждому товару — кластеры с наибольшим расходом: показы, клики, CTR, корзины, заказы, расход, цена заказа,
     средняя позиция, статус (активен / исключён), ставка на кластер (если ручная).
  3. Кандидаты на исключение: расход без заказов не меньше допустимой цены заказа, много кликов без заказов,
     слова «не наша аудитория» (женский, детский…). Запросы с брендом (awer, авер, mcl) не предлагаются.

Запуск:  python wb_clusters.py                       (все кабинеты, 14 дней)
         python wb_clusters.py 30                    (за 30 дней)
         python wb_clusters.py --cabinet "Ребрик"
         python wb_clusters.py --campaign 40863019   (одна кампания)
Нужна категория токена «Продвижение».
Результат: вывод на экран + wb_clusters_report.txt, wb_clusters.csv (все кластеры)
"""
import datetime as dt
import sys
from collections import defaultdict

from wb_common import ADV, YESTERDAY, ApiError, arg_cabinet, call, campaigns, chunks, cut, fm, load_tokens, log, num, \
    pace, save_log, write_csv

# ---------------- НАСТРОЙКИ ----------------
DEFAULT_DAYS = 14
TOP_PER_NM = 8                  # сколько кластеров показать на товар
MAX_COST_PER_ORDER = 600        # расход без заказа больше этого — кандидат на исключение, ₽
MIN_CLICKS_NO_ORDER = 30        # или столько кликов без единого заказа
PROTECT = ["awer", "авер", "mcl"]
MINUS_WORDS = ["женск", "детск", "девочк", "мальчик", "для девушки", "подростк"]
# -------------------------------------------


def arg(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


def days():
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a.isdigit() and not (i > 0 and args[i - 1] in ("--cabinet", "--campaign")):
            return int(a)
    return DEFAULT_DAYS


DAYS = days()


def nq_lists(token, pairs):
    res = {}
    for part in chunks(pairs, 100):
        pace(token, "nq", 0.25)
        data = call("POST", ADV + "/adv/v0/normquery/list", token, what="Кластеры товаров",
                    body={"items": [{"advertId": a, "nmId": n} for a, n in part]}) or {}
        for it in data.get("items") or []:
            q = it.get("normQueries") or {}
            res[(it.get("advertId"), it.get("nmId"))] = {k: list(q.get(k) or []) for k in ("active", "excluded", "archived")}
    return res


def nq_minus(token, pairs):
    res = {}
    for part in chunks(pairs, 100):
        pace(token, "nq", 0.25)
        data = call("POST", ADV + "/adv/v0/normquery/get-minus", token, what="Минус-фразы",
                    body={"items": [{"advert_id": a, "nm_id": n} for a, n in part]}) or {}
        for it in data.get("items") or []:
            if isinstance(it.get("norm_queries"), list):
                res[(it.get("advert_id"), it.get("nm_id"))] = list(it["norm_queries"])
    return res


def nq_stats(token, pairs, d1, d2):
    res = defaultdict(dict)
    for part in chunks(pairs, 100):
        pace(token, "nqstats", 7)
        data = call("POST", ADV + "/adv/v0/normquery/stats", token, what="Статистика кластеров",
                    body={"from": d1.isoformat(), "to": d2.isoformat(),
                          "items": [{"advert_id": a, "nm_id": n} for a, n in part]}) or {}
        for it in data.get("stats") or []:
            for s in it.get("stats") or []:
                q = s.get("norm_query")
                if q:
                    res[(it.get("advert_id"), it.get("nm_id"))][q] = {
                        "views": s.get("views"), "clicks": num(s.get("clicks")), "atbs": num(s.get("atbs")),
                        "orders": num(s.get("orders")), "spend": num(s.get("spend")), "ctr": s.get("ctr"),
                        "avg_pos": s.get("avg_pos")}
    return res


def nq_bids(token, pairs):
    res = {}
    for part in chunks(pairs, 100):
        pace(token, "nq", 0.25)
        data = call("POST", ADV + "/adv/v0/normquery/get-bids", token, what="Ставки кластеров",
                    body={"items": [{"advert_id": a, "nm_id": n} for a, n in part]}) or {}
        for b in data.get("bids") or []:
            res[(b.get("advert_id"), b.get("nm_id"), b.get("norm_query"))] = num(b.get("bid"))
    return res


def run_cabinet(cab, token, only):
    log(f"\n══════════ {cab} ══════════")
    camps = campaigns(token, statuses=(9, 11))
    if only:
        camps = {k: v for k, v in camps.items() if str(k) == str(only)}
    if not camps:
        log("  Кампаний нет.")
        return []
    d2 = YESTERDAY
    d1 = d2 - dt.timedelta(days=DAYS - 1)
    out = []
    for cid, a in sorted(camps.items()):
        st = a.get("settings") or {}
        pay = st.get("payment_type")
        unified = a.get("bid_type") == "unified"
        nms = [s.get("nm_id") for s in a.get("nm_settings") or [] if s.get("nm_id")]
        if not nms:
            continue
        pairs = [(cid, n) for n in nms]
        kind = ("за клики" if pay == "cpc" else "за показы") + (", единая ставка" if unified else ", ручная ставка")
        log(f"\n  ▶ {st.get('name')} (ID {cid}) — {kind}, товаров {len(nms)}")
        errors = []

        def safe(fn, *a):
            try:
                return fn(token, *a)
            except ApiError as e:
                errors.append(str(e))
                return {}
        lists = safe(nq_lists, pairs)
        minus = safe(nq_minus, pairs)
        stats = safe(nq_stats, pairs, d1, d2)
        bids = safe(nq_bids, pairs) if (pay == "cpm" and not unified) else {}
        if errors and pay != "cpc":
            log("    ✗ " + "; ".join(errors))
        n_act = sum(len(v["active"]) for v in lists.values())
        n_exc = sum(len(set(v["excluded"]) | set(minus.get(p, []))) for p, v in lists.items())
        n_stat = sum(len(v) for v in stats.values())
        log(f"    Кластеров: активных {n_act}, исключённых {n_exc}; со статистикой за {DAYS} дн.: {n_stat}"
            + (f"; ставок на кластеры: {len(bids)}" if bids else ""))
        if not lists and not stats:
            log("    WB не отдал по этой кампании ни списков кластеров, ни статистики"
                + (" — у кампаний за клики так бывает: управлять запросами WB не даёт." if pay == "cpc" else
                   " — возможно, показов ещё меньше 100 на кластер."))
            continue
        cand = []
        for nm in nms:
            p = (cid, nm)
            excl = set((lists.get(p) or {}).get("excluded") or []) | set(minus.get(p, []))
            rows = sorted(stats.get(p, {}).items(), key=lambda x: -x[1]["spend"])
            for q, s in rows:
                status = "исключён" if q in excl else "активен"
                cpo = s["spend"] / s["orders"] if s["orders"] else None
                bad = ""
                ql = q.lower()
                if status == "активен" and not any(w in ql for w in PROTECT):
                    if any(w in ql for w in MINUS_WORDS):
                        bad = "не наша аудитория"
                    elif not s["orders"] and s["spend"] >= MAX_COST_PER_ORDER:
                        bad = f"расход {fm(s['spend'])} ₽ без заказов"
                    elif not s["orders"] and s["clicks"] >= MIN_CLICKS_NO_ORDER:
                        bad = f"{int(s['clicks'])} кликов без заказов"
                if bad:
                    cand.append((nm, q, bad, s))
                out.append({"кабинет": cab, "кампания": cid, "название": st.get("name"), "оплата": pay, "артикул WB": nm,
                            "кластер": q, "статус": status, "показы": s["views"] if s["views"] is not None else "",
                            "клики": int(s["clicks"]), "CTR %": s["ctr"] if s["ctr"] is not None else "",
                            "корзины": int(s["atbs"]), "заказы": int(s["orders"]), "расход ₽": round(s["spend"], 2),
                            "цена заказа ₽": round(cpo, 2) if cpo else "", "ср. позиция": s["avg_pos"] or "",
                            "ставка на кластер ₽": bids.get((cid, nm, q), ""), "кандидат на исключение": bad})
            if rows:
                log(f"    {nm}: кластеров {len(rows)}, исключено {len(excl)}; топ по расходу:")
                for q, s in rows[:TOP_PER_NM]:
                    cpo = s["spend"] / s["orders"] if s["orders"] else None
                    log(f"      {cut(q, 34):34} {('показы ' + fm(s['views']) + ', ') if s['views'] is not None else ''}"
                        f"клики {fm(s['clicks'])}, заказы {fm(s['orders'])}, расход {fm(s['spend'])} ₽"
                        f"{', заказ ' + fm(cpo) + ' ₽' if cpo else ''}, поз. {fm(s['avg_pos']) if s['avg_pos'] else '—'}"
                        f"{' — исключён' if q in excl else ''}")
        if cand:
            log(f"    Кандидаты на исключение ({len(cand)}):")
            for nm, q, why, s in sorted(cand, key=lambda x: -x[3]["spend"])[:20]:
                log(f"      {nm} «{q}» — {why}")
            if pay == "cpc":
                log("    (кампания за клики: исключать кластеры WB здесь не даёт — только сменой товаров или ставки)")
    return out


def main():
    cabs = load_tokens(arg_cabinet())
    only = arg("--campaign")
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — поисковые кластеры рекламы WB за {DAYS} дн. (только отчёт, ничего не меняет)")
    rows = []
    for cab, token in cabs.items():
        try:
            rows += run_cabinet(cab, token, only)
        except ApiError as e:
            log(f"  ✗ {cab}: {e}")
    write_csv("wb_clusters.csv", rows, header=list(rows[0]) if rows else None)
    save_log("wb_clusters_report.txt")
    print("\nСохранено: wb_clusters_report.txt, wb_clusters.csv")


if __name__ == "__main__":
    main()

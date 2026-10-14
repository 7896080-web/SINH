# -*- coding: utf-8 -*-
"""
Самые успешные поисковые запросы WB — библиотека для SEO карточек похожих товаров. Только чтение, к WB не обращается:
работает по wb_history.db (её каждый день пополняет wb_positions.py — «Джем» по запросам и реклама по кластерам).

Что делает:
  1. Успешные запросы по каждой категории товара (предмет WB: «Куртки», «Джемперы»…) за DAYS дней, по всем кабинетам
     вместе — те же модели в разных кабинетах учат друг друга: запрос, по которому товары категории оформили не меньше
     MIN_ORDERS заказов при конверсии из перехода в заказ не хуже SUCCESS_CONV от средней по категории. По каждому:
     частота в неделю, заказы, конверсия, доля заказов без рекламы, цена заказа с рекламы, лучший товар и его позиция.
  2. Библиотека wb_seo_library.csv — копится: когда запрос впервые стал успешным, когда был им последний раз, лучшие
     цифры. Запрос, который перестал быть успешным, остаётся с отметкой «был успешным» (сезон, остатки).
  3. Задачи для SEO (wb_seo_tasks.csv): товары той же категории с остатком, которые по успешному запросу не продают
     (запроса нет среди их запросов или позиция дальше FAR_POS) — какие слова запроса добавить в название, описание и
     характеристики (слов нет в названии товара; описание и характеристики через API без токена «Контент» не видны —
     проверьте их сами).

Запуск:  python wb_seo_queries.py              (последние 30 дней)
         python wb_seo_queries.py 60
По расписанию — после wb_positions.py (6:00), см. wb_run.py. Результат: wb_seo_report.txt, wb_seo_library.csv,
wb_seo_tasks.csv (таблицы — для Excel).
"""
import csv
import datetime as dt
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# вывод в консоль Windows (cp1251) не должен падать на ₽ ✓: непечатаемое заменяется «?»
try:
    if sys.stdout is not None and (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
        sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

# ---------------- НАСТРОЙКИ ----------------
DAYS = next((int(a) for a in sys.argv[1:] if a.isdigit()), 30)
MIN_ORDERS = 5            # заказов по запросу в категории за период — меньше: не судим
SUCCESS_CONV = 0.8        # конверсия переход → заказ не ниже 80% средней по категории
TOP_PER_SUBJECT = 25      # сколько лучших запросов категории показывать
FAR_POS = 50              # товар «не виден» по запросу — медианная позиция дальше этой
MIN_STOCK = 15            # задачи SEO — только для товаров, которых в наличии (WB + свой склад) не меньше
NEGATIVE = ["женск", "детск", "девочк", "мальчик", "для девушки", "подростк"]   # не наша аудитория
STOP = {"для", "мужской", "мужская", "мужские", "мужчин", "мужчины", "муж", "men", "и", "с", "на", "в", "по"}
COLORS = ["черн", "бел", "син", "сер", "беж", "корич", "хаки", "зелен", "красн", "голуб", "бордо", "графит", "молоч",
          "темно", "светло", "оливк", "горчич", "песоч", "индиго", "антрацит", "бирюз", "мятн", "желт", "оранж"]
# -------------------------------------------

HERE = Path(__file__).resolve().parent
DB = HERE / "wb_history.db"
LIB = HERE / "wb_seo_library.csv"
TASKS = HERE / "wb_seo_tasks.csv"
REPORT = HERE / "wb_seo_report.txt"
out = []


def log(s=""):
    print(s)
    out.append(str(s))


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def med(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


def words(text):
    return [w for w in re.findall(r"[a-zа-яё0-9]+", (text or "").lower()) if len(w) >= 3 and w not in STOP]


def stems(text):
    """Слова текста без окончаний (первые 5 букв) — «куртка», «куртки», «куртку» совпадают."""
    return {w[:5] for w in words(text)}


def missing_words(query, title):
    have = stems(title)
    res = []
    for w in words(query):
        if w[:5] not in have and w not in res:
            res.append(w)
    return res


def write_csv(path, rows, fields):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow({k: (str(v).replace(".", ",") if isinstance(v, float) else v) for k, v in r.items()})


def load():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    since = (dt.date.today() - dt.timedelta(days=DAYS)).isoformat()
    prod = {(c, nm): {"vendor": v or "", "title": t or "", "subject": s or "без категории"}
            for c, nm, v, t, s in con.execute("SELECT cab, nm, vendor, title, subject FROM products")}
    q = defaultdict(lambda: {"orders": 0.0, "opens": 0.0, "carts": 0.0, "wf": [], "pos": [], "days": 0})
    for c, nm, query, d, o, op, ca, wf, mp in con.execute(
            "SELECT cab, nm, query, date, orders, opens, carts, week_freq, median_pos FROM query_day WHERE date>=?", (since,)):
        x = q[(c, nm, query)]
        x["orders"] += num(o)
        x["opens"] += num(op)
        x["carts"] += num(ca)
        x["days"] += 1
        if wf:
            x["wf"].append(num(wf))
        if mp:
            x["pos"].append(num(mp))
    ad = defaultdict(lambda: {"orders": 0.0, "spend": 0.0})
    for c, nm, query, o, s in con.execute(
            "SELECT cab, nm, query, orders, spend FROM ad_query_day WHERE date>=?", (since,)):
        ad[(c, nm, query)]["orders"] += num(o)
        ad[(c, nm, query)]["spend"] += num(s)
    stock = {}
    for c, nm, d, wb, mp in con.execute("SELECT cab, nm, date, stock_wb, stock_mp FROM stock_day ORDER BY date"):
        stock[(c, nm)] = num(wb) + num(mp)
    con.close()
    return prod, q, ad, stock


def main():
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — успешные поисковые запросы для SEO за {DAYS} дн. (по wb_history.db)")
    if not DB.exists():
        log("Нет wb_history.db — её собирает python wb_positions.py (по расписанию в 6:00).")
        return
    prod, q, ad, stock = load()
    if not q:
        log("В базе нет запросов за период — нужен «Джем» и хотя бы несколько дней wb_positions.py.")
        return
    # ---- по категориям: запрос → сумма по товарам всех кабинетов
    subj = defaultdict(lambda: defaultdict(lambda: {"orders": 0.0, "opens": 0.0, "ad_orders": 0.0, "ad_spend": 0.0,
                                                     "wf": [], "items": []}))
    for (c, nm, query), x in q.items():
        s = prod.get((c, nm), {}).get("subject", "без категории")
        g = subj[s][query]
        g["orders"] += x["orders"]
        g["opens"] += x["opens"]
        a = ad.get((c, nm, query)) or {}
        g["ad_orders"] += a.get("orders", 0.0)
        g["ad_spend"] += a.get("spend", 0.0)
        if x["wf"]:
            g["wf"].append(med(x["wf"]))
        g["items"].append((c, nm, x["orders"], med(x["pos"])))
    today = dt.date.today().isoformat()
    lib_now, tasks = {}, []
    for s, queries in sorted(subj.items(), key=lambda kv: -sum(v["orders"] for v in kv[1].values())):
        tot_o = sum(v["orders"] for v in queries.values())
        tot_op = sum(v["opens"] for v in queries.values())
        if not tot_o:
            continue
        conv_cat = tot_o / tot_op if tot_op else None
        good = []
        for query, g in queries.items():
            if any(w in query for w in NEGATIVE) or g["orders"] < MIN_ORDERS:
                continue
            conv = g["orders"] / g["opens"] if g["opens"] else None
            if conv_cat and conv is not None and conv < conv_cat * SUCCESS_CONV:
                continue
            good.append((query, g, conv))
        if not good:
            continue
        good.sort(key=lambda x: -x[1]["orders"])
        log(f"\n══════════ {s} ══════════  заказов из поиска за {DAYS} дн.: {tot_o:.0f}, конверсия переход → заказ "
            f"{(conv_cat or 0):.1%}")
        log(f"  {'запрос':38}{'в нед.':>8}{'заказы':>8}{'конв.':>7}{'без рекл.':>10}{'заказ рекл.':>12}  лучший товар (позиция)")
        for query, g, conv in good[:TOP_PER_SUBJECT]:
            best = max(g["items"], key=lambda x: x[2])
            bp = prod.get((best[0], best[1]), {})
            org = 1 - g["ad_orders"] / g["orders"] if g["orders"] else None
            cpo = g["ad_spend"] / g["ad_orders"] if g["ad_orders"] else None
            wf = med(g["wf"])
            wf_s = f"{wf:,.0f}".replace(",", " ") if wf else "—"
            conv_s = f"{conv:.1%}" if conv is not None else "—"
            org_s = f"{max(org, 0):.0%}" if org is not None else "—"
            cpo_s = f"{cpo:,.0f} ₽".replace(",", " ") if cpo else "—"
            pos_s = f" ({best[3]:.0f})" if best[3] else ""
            log(f"  {query[:37]:38}{wf_s:>8}{g['orders']:>8.0f}{conv_s:>7}{org_s:>10}{cpo_s:>12}  "
                f"{(bp.get('vendor') or str(best[1]))[:22]}{pos_s}")
            selling = sorted({prod.get((c, nm), {}).get("vendor") or str(nm) for c, nm, o, p in g["items"] if o > 0})
            lib_now[(s, query)] = {"категория": s, "запрос": query, "частота в неделю": round(wf) if wf else "",
                                   "заказы за период": round(g["orders"]), "конверсия": round(conv, 4) if conv else "",
                                   "доля без рекламы": round(max(org, 0), 2) if org is not None else "",
                                   "цена заказа с рекламы ₽": round(cpo) if cpo else "",
                                   "продают (арт. продавца)": ", ".join(selling[:8])}
            # задачи SEO: товары категории с остатком, которые по этому запросу не продают
            have = {(c, nm): (o, p) for c, nm, o, p in g["items"]}
            color = next((c_ for c_ in COLORS if c_ in query), None)
            for (c, nm), info in prod.items():
                if info["subject"] != s or stock.get((c, nm), 0) < MIN_STOCK:
                    continue
                o, p = have.get((c, nm), (0.0, None))
                if o > 0 and (p is None or p <= FAR_POS):
                    continue
                missing = missing_words(query, info["title"])
                if color and color in (info["title"] + " " + info["vendor"]).lower():
                    color_note = ""
                elif color:
                    color_note = "в запросе цвет — только если товар этого цвета"
                else:
                    color_note = ""
                tasks.append({"категория": s, "запрос": query, "частота в неделю": round(wf) if wf else "",
                              "заказов по запросу в категории": round(g["orders"]), "кабинет": c, "артикул WB": nm,
                              "арт. продавца": info["vendor"],
                              "сейчас": ("запроса нет среди запросов товара" if (c, nm) not in have else
                                         f"позиция {p:.0f}, заказов {o:.0f}" if p else f"заказов {o:.0f}"),
                              "слов нет в названии": " ".join(missing) or "все слова есть — проверьте описание и "
                                                                          "характеристики",
                              "проверить": color_note, "название": info["title"]})
    # ---- библиотека: копится
    lib = {}
    if LIB.exists():
        try:
            for r in csv.DictReader(LIB.read_bytes().decode("utf-8-sig", errors="replace").splitlines(), delimiter=";"):
                lib[(r.get("категория"), r.get("запрос"))] = r
        except (OSError, csv.Error):
            pass
    fields = ["категория", "запрос", "статус", "впервые успешный", "последний раз успешный", "частота в неделю",
              "заказы за период", "лучшие заказы за период", "конверсия", "доля без рекламы", "цена заказа с рекламы ₽",
              "продают (арт. продавца)"]
    merged = []
    for key in set(lib) | set(lib_now):
        old, new = lib.get(key) or {}, lib_now.get(key)
        if new:
            best = max(num(str(old.get("лучшие заказы за период") or 0).replace(",", ".")), new["заказы за период"])
            merged.append({**new, "статус": "успешный", "впервые успешный": old.get("впервые успешный") or today,
                           "последний раз успешный": today, "лучшие заказы за период": round(best)})
        else:
            merged.append({k: old.get(k, "") for k in fields} | {"статус": "был успешным"})
    merged.sort(key=lambda r: (r["категория"], r["статус"] != "успешный",
                               -num(str(r.get("заказы за период") or 0).replace(",", "."))))
    write_csv(LIB, [{k: r.get(k, "") for k in fields} for r in merged], fields)
    tasks.sort(key=lambda r: (r["категория"], -r["заказов по запросу в категории"], r["арт. продавца"]))
    if tasks:
        write_csv(TASKS, tasks, list(tasks[0]))
    n_new = sum(1 for k in lib_now if k not in lib)
    log(f"\nБиблиотека: успешных запросов сейчас {len(lib_now)}" + (f", новых {n_new}" if lib else "")
        + f", всего в {LIB.name} — {len(merged)} (с «был успешным»).")
    log(f"Задачи для SEO: {len(tasks)} (товар × запрос) — {TASKS.name}. Слова запроса, которых нет в названии, — в "
        "название, описание, характеристики; описание и характеристики проверьте сами (без токена «Контент» их не видно).")
    by_nm = defaultdict(list)
    for t in tasks:
        by_nm[(t["арт. продавца"], t["кабинет"])].append(t)
    if by_nm:
        log("\nКому больше всего поможет SEO (товар — сколько успешных запросов категории он упускает):")
        for (v, c), lst in sorted(by_nm.items(), key=lambda kv: -sum(t["заказов по запросу в категории"] for t in kv[1]))[:15]:
            log(f"  • {v} ({c}): {len(lst)} запр., например: " + "; ".join(t["запрос"] for t in lst[:3]))
    REPORT.write_text("\n".join(out), encoding="utf-8")
    print(f"\nСохранено: {REPORT.name}, {LIB.name}" + (f", {TASKS.name}" if tasks else ""))


if __name__ == "__main__":
    main()

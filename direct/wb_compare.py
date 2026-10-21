# -*- coding: utf-8 -*-
"""
Какая кампания лучше продвигает одни и те же модели — сравнение кабинетов (Яворская, Ребрик, Караман).
Запрос пользователя 21.10.2026: «из 3 кабинетов выбираем кампании, которые работают лучше всего на примерно одинаковые
артикулы». Только чтение — в кабинетах ничего не меняет.

Одни и те же модели AWER продаются в трёх кабинетах: артикулы WB у них разные, артикул продавца — тот же. Скрипт:
  1. берёт кампании каждого кабинета (работающие, на паузе, завершённые за 30 дней) и их расход и заказы с рекламы
     по товарам за период;
  2. сводит товары в модели по артикулу продавца, все цвета вместе: «2617 C24-2317CQ Авер Куртка NAVY» → 2617,
     «2764327643MAVI» и «27643NILYESIL» → 27643, «TC26-2775-1 БлекВинил …» → TC26-2775-1;
  3. собирает в группы кампании на одних и тех же моделях (из любых кабинетов): к самой расходной кампании — те, у кого
     с ней общих моделей не меньше OVERLAP от меньшей из двух, и так далее по убыванию расхода;
  4. в группе ставит кампании по цене заказа с рекламы — лучшая первой; при малом числе заказов место считается
     с поправкой (как будто у каждой ещё PRIOR заказа по средней цене группы), меньше FEW заказов — «предварительно»;
  5. по каждой модели, которую рекламирует больше одной кампании, показывает, где заказ с рекламы дешевле, и сколько
     всего заказов (с рекламой и без) у карточек этой модели в каждом кабинете — у сильной карточки (отзывы, рейтинг)
     и с рекламы заказ обычно дешевле.

Запуск:  python wb_compare.py                          — последние 14 дней по вчера
         python wb_compare.py --days 7
         python wb_compare.py --from 2026-10-14 [--to 2026-10-20]
         python wb_compare.py --cabinet "Ребрик"       — один кабинет (сравнение его кампаний между собой)
Нужны категории токена: «Продвижение», «Аналитика».
Результат: wb_compare_report.txt, wb_compare.csv (кампании по группам), wb_compare_models.csv (модель × кампания).
"""
import datetime as dt
import re
import sys
from collections import defaultdict

from wb_campaigns import list_campaigns
from wb_common import (TODAY, YESTERDAY, ApiError, ad_stats, arg_cabinet, cut, each_cabinet, fm, funnel, load_tokens,
                       log, save_log, warn_incomplete, write_csv)

# ======================== НАСТРОЙКИ ========================
DAYS = 14             # период по умолчанию — столько дней по вчера
OVERLAP = 0.5         # в одну группу: общих моделей не меньше этой доли у той кампании, где моделей меньше
MIN_SPEND = 300       # кампания потратила за период меньше (₽) — судить не из чего, в группы не беру
MIN_MODEL_SPEND = 300 # на модель кампания потратила меньше (₽) — по этой модели её не сравниваю (показана последней)
PRIOR = 2             # поправка на малое число заказов (для места в группе и по модели)
FEW = 5               # заказов меньше — вывод предварительный
# ===========================================================

STATUS = {4: "готова", 9: "активна", 11: "пауза", 7: "завершена"}
PAY = {"cpm": "показы", "cpc": "клики"}
KEYS = ("views", "clicks", "sum", "orders", "sum_price")


def arg(name):
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def period():
    try:
        d2 = dt.date.fromisoformat(arg("--to")) if arg("--to") else YESTERDAY
        if arg("--from"):
            d1 = dt.date.fromisoformat(arg("--from"))
        else:
            d1 = d2 - dt.timedelta(days=int(arg("--days") or DAYS) - 1)
    except ValueError:
        sys.exit("--from / --to — дата в виде ГГГГ-ММ-ДД, --days — число дней")
    if not d1 <= d2 <= TODAY:
        sys.exit("Период: --from не позже --to, --to не позже сегодняшнего дня")
    return d1, d2


def model_of(vendor):
    """Номер модели из артикула продавца (все цвета вместе) или None, если артикула нет."""
    first = (str(vendor or "").split() or [""])[0]
    m = re.match(r"\d{4,}", first)
    if m:
        d = m.group()
        h = len(d) // 2
        return d[:h] if len(d) % 2 == 0 and d[:h] == d[h:] else d      # «2764327643MAVI» → 27643
    return first.upper() or None


def model_name(vendors):
    """Общее начало артикулов продавца модели: «2617 C24-2317CQ Авер Куртка» (цвета отличаются — отрезаны)."""
    words = [str(v).split() for v in vendors if v]
    pre = []
    for col in zip(*words):
        if any(w != col[0] for w in col):
            break
        pre.append(col[0])
    return " ".join(pre)


def collect(cab, token, d1, d2):
    log(f"\n{'═' * 20} {cab} {'═' * 20}")
    camps = list_campaigns(token, True)
    ids = [i for i, a in camps.items() if a["_status"] in (7, 9, 11)]
    log(f"  Кампаний: {len(camps)} (со статистикой — {len(ids)}). Загружаю расход и заказы {d1:%d.%m}–{d2:%d.%m}…")
    _, by_camp = ad_stats(token, ids, d1, d2) if ids else ({}, {})
    try:
        info = funnel(token, d1, d2)
        miss = sorted({nm for (_a, nm) in by_camp} - set(info))
        if miss:                                  # товар из кампании, которого нет в общей выдаче воронки
            info.update(funnel(token, d1, d2, miss))
    except ApiError as e:
        warn_incomplete(f"{cab}: воронка продаж (артикулы продавца, все заказы): {e} — товары не сведены в модели")
        info = {}
    adv = {nm for (_a, nm), v in by_camp.items() if v["sum"] > 0}
    log(f"  Товаров в кабинете: {len(info)}, с расходом на рекламу: {len(adv)}.")
    return {"camps": camps, "stats": by_camp, "info": info}


def build(data):
    """Кампании всех кабинетов с расходом по моделям и модели: {модель: артикулы продавца, предметы, все заказы карточек
    по кабинетам}."""
    models = defaultdict(lambda: {"vendors": set(), "subjects": defaultdict(int), "cards": defaultdict(float)})
    nm_model = {}
    for cab, d in data.items():
        for nm, nf in d["info"].items():
            m = model_of(nf.get("vendor"))
            if not m:
                continue
            nm_model[(cab, nm)] = m
            mm = models[m]
            mm["vendors"].add(nf.get("vendor"))
            if nf.get("subject"):
                mm["subjects"][nf["subject"]] += 1
            mm["cards"][cab] += nf.get("orderCount") or 0
    camps = []
    for cab, d in data.items():
        per_camp = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
        for (aid, nm), v in d["stats"].items():
            m = nm_model.get((cab, nm)) or f"арт. WB {nm}"  # без артикула продавца — отдельно, ни с чем не сводится
            for k in KEYS:
                per_camp[aid][m][k] += v[k]
        for aid, a in d["camps"].items():
            st = a.get("settings") or {}
            per = per_camp.get(aid, {})
            tot = {k: sum(v[k] for v in per.values()) for k in KEYS}
            camps.append({"cab": cab, "id": aid, "name": st.get("name") or "", "status": STATUS.get(a.get("_status"), "?"),
                          "pay": PAY.get(st.get("payment_type"), st.get("payment_type") or "?"), "per": per, "tot": tot,
                          "models": {m for m, v in per.items() if v["sum"] > 0}})
    return camps, models


def overlap(a, b):
    return len(a & b) / min(len(a), len(b)) if a and b else 0


def make_groups(camps):
    pool = sorted((c for c in camps if c["tot"]["sum"] >= MIN_SPEND), key=lambda c: -c["tot"]["sum"])
    groups, used = [], set()
    for c in pool:
        if (c["cab"], c["id"]) in used:
            continue
        g = [c]
        used.add((c["cab"], c["id"]))
        for o in pool:
            if (o["cab"], o["id"]) not in used and overlap(c["models"], o["models"]) >= OVERLAP:
                g.append(o)
                used.add((o["cab"], o["id"]))
        groups.append(g)
    return groups


def cpo(t):
    return t["sum"] / t["orders"] if t["orders"] else None


def ranked(items, tot_of):
    """По цене заказа с поправкой на малое число заказов: [(элемент, цена с поправкой)], средняя цена заказа."""
    sp, od = sum(tot_of(x)["sum"] for x in items), sum(tot_of(x)["orders"] for x in items)
    base = sp / od if od else None
    if base is None:                              # заказов нет ни у кого — первым больший расход (больше всего впустую)
        return [(x, None) for x in sorted(items, key=lambda x: -tot_of(x)["sum"])], None
    sc = [(x, (tot_of(x)["sum"] + PRIOR * base) / (tot_of(x)["orders"] + PRIOR)) for x in items]
    return sorted(sc, key=lambda p: p[1]), base


def times(k):
    """«в 1,8 раза», «в 3 раза», «в 12 раз»."""
    if k >= 10:
        return f"в {k:.0f} раз"
    return f"в {k:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " раза"


def title(c):
    return f"{c['cab']} «{c['name']}» {c['id']}"


def few(t):
    return " (заказов мало — предварительно)" if t["orders"] < FEW else ""


def group_label(g, models):
    spend = defaultdict(float)
    for c in g:
        for m, v in c["per"].items():
            spend[m] += v["sum"]
    top = [m for m, _ in sorted(spend.items(), key=lambda p: -p[1]) if _ > 0]
    subj = defaultdict(float)
    for m in top:
        mm = models.get(m)
        if mm and mm["subjects"]:
            subj[max(mm["subjects"], key=mm["subjects"].get)] += spend[m]
    s = ", ".join(k for k, _ in sorted(subj.items(), key=lambda p: -p[1])[:2])
    return (s + ": " if s else "") + ", ".join(top[:5]) + (f" и ещё {len(top) - 5}" if len(top) > 5 else "")


def report_groups(groups, models):
    rows, alone = [], []
    log("\n" + "═" * 100)
    log("1. ГРУППЫ КАМПАНИЙ НА ОДНИХ И ТЕХ ЖЕ МОДЕЛЯХ (лучшая — первой)")
    n = 0
    for g in groups:
        if len(g) < 2:
            alone.append(g[0])
            continue
        n += 1
        lst, base = ranked(g, lambda c: c["tot"])
        lead = lst[0][0]
        log(f"\n── Группа {n} — {group_label(g, models)}")
        log(f"   {'№':>2} {'кабинет':9}{'кампания':38}{'оплата':8}{'статус':10}{'моделей':>9}{'расход ₽':>10}"
            f"{'заказов':>8}{'заказ ₽':>9}{'ДРР':>7}{'CTR':>7}{'клик ₽':>8}")
        for i, (c, _sc) in enumerate(lst, 1):
            t = c["tot"]
            common = len(c["models"] & lead["models"])
            mod = f"{len(c['models'])}" if c is lead else f"{common}/{len(c['models'])}"
            drr = t["sum"] / t["sum_price"] * 100 if t["sum_price"] else None
            ctr = t["clicks"] / t["views"] * 100 if t["views"] else None
            cpc = t["sum"] / t["clicks"] if t["clicks"] else None
            log(f"   {i:>2} {cut(c['cab'], 8):9}{cut(c['name'], 26) + ' ' + str(c['id']):38}{c['pay']:8}{c['status']:10}"
                f"{mod:>9}{fm(t['sum']):>10}{fm(t['orders']):>8}{fm(cpo(t)):>9}{fm(drr, 1, '%'):>7}{fm(ctr, 2, '%'):>7}"
                f"{fm(cpc, 1):>8}")
            rows.append({"группа": n, "место": i, "кабинет": c["cab"], "кампания": c["name"], "ID": c["id"],
                         "оплата": c["pay"], "статус": c["status"], "моделей": len(c["models"]),
                         "общих с первой": common, "расход ₽": round(t["sum"]), "заказов с рекламы": round(t["orders"]),
                         "заказ ₽": round(cpo(t)) if cpo(t) else "", "сумма заказов с рекламы ₽": round(t["sum_price"]),
                         "ДРР %": round(drr, 1) if drr else "", "показы": round(t["views"]), "клики": round(t["clicks"]),
                         "CTR %": round(ctr, 2) if ctr else "", "клик ₽": round(cpc, 1) if cpc else ""})
        if base is None:
            log("   Заказов с рекламы нет ни у одной кампании группы — лучшей нет.")
        else:
            log(f"   Лучше всех: {title(lead)} — заказ {fm(cpo(lead['tot']))} ₽, заказов {fm(lead['tot']['orders'])}"
                f"{few(lead['tot'])}.")
    log("\n   «моделей» — у первой: сколько моделей она рекламировала; у остальных: общих с первой / всего у неё."
        "\n   ДРР — расход ÷ сумма заказов с рекламы. Место — по цене заказа с поправкой на малое число заказов.")
    if alone:
        log("\n   Кампании без пары (на их моделях больше никто не тратил от " + fm(MIN_SPEND) + " ₽): "
            + "; ".join(f"{title(c)} — {fm(c['tot']['sum'])} ₽, заказов {fm(c['tot']['orders'])}" for c in alone))
    return rows, n


def report_models(camps, models):
    by_model = defaultdict(list)
    for c in camps:
        for m, v in c["per"].items():
            if v["sum"] > 0:
                by_model[m].append((c, v))
    shared = {m: lst for m, lst in by_model.items() if sum(v["sum"] >= MIN_MODEL_SPEND for _c, v in lst) > 1}
    rows, wins = [], defaultdict(lambda: [0, 0])
    log("\n" + "═" * 100)
    log("2. ПО МОДЕЛЯМ — где заказ с рекламы дешевле (модели, которые рекламирует больше одной кампании)")
    if not shared:
        log("   Таких моделей нет.")
    for m, lst in sorted(shared.items(), key=lambda p: -sum(v["sum"] for _c, v in p[1])):
        mm = models.get(m) or {}
        name = cut(model_name(mm.get("vendors") or []) or m, 48)
        subj = max(mm["subjects"], key=mm["subjects"].get) if mm.get("subjects") else ""
        cards = mm.get("cards") or {}
        log(f"\n   ▶ {m} — {name}" + (f" ({subj})" if subj else "")
            + ("; все заказы карточек модели за период: " + ", ".join(f"{k} {fm(v)}" for k, v in sorted(
                cards.items(), key=lambda p: -p[1])) if cards else ""))
        order, base = ranked([p for p in lst if p[1]["sum"] >= MIN_MODEL_SPEND], lambda p: p[1])
        order += [(p, None) for p in sorted(lst, key=lambda p: -p[1]["sum"]) if p[1]["sum"] < MIN_MODEL_SPEND]
        for i, ((c, v), _sc) in enumerate(order, 1):
            small = v["sum"] < MIN_MODEL_SPEND
            mark = ("  (мало расхода — не сравниваю)" if small else
                    "  ← дешевле всех" if i == 1 and base is not None else "")
            log(f"      {cut(title(c), 52):52} {c['pay']:7} расход {fm(v['sum']):>7} ₽, заказов {fm(v['orders']):>3}, "
                f"заказ {fm(cpo(v)):>6} ₽{mark}")
            if not small:
                wins[(c["cab"], c["id"])][1] += 1
            if i == 1 and base is not None:
                wins[(c["cab"], c["id"])][0] += 1
            rows.append({"модель": m, "артикул продавца (общее начало)": model_name(mm.get("vendors") or []),
                         "предмет": subj, "кабинет": c["cab"], "кампания": c["name"], "ID": c["id"], "оплата": c["pay"],
                         "статус": c["status"], "место": "" if small else i, "расход ₽": round(v["sum"]),
                         "заказов с рекламы": round(v["orders"]), "заказ ₽": round(cpo(v)) if cpo(v) else "",
                         "показы": round(v["views"]), "клики": round(v["clicks"]),
                         "все заказы карточек в кабинете": round(cards.get(c["cab"], 0))})
        if base is None:
            log("      заказов с рекламы на этой модели нет ни у одной кампании")
    return rows, wins


def summary(groups, camps, wins, d1, d2):
    log("\n" + "═" * 100)
    log("3. ИТОГ")
    n = 0
    for g in groups:
        if len(g) < 2:
            continue
        n += 1
        lst, base = ranked(g, lambda c: c["tot"])
        if base is None:
            log(f"\n   Группа {n}: заказов с рекламы нет ни у одной кампании — "
                + "; ".join(f"{title(c)} потратила {fm(c['tot']['sum'])} ₽" for c, _ in lst))
            continue
        lead = lst[0][0]
        best = cpo(lead["tot"])
        log(f"\n   Группа {n}: лучше всех — {title(lead)} ({lead['pay']}): заказ {fm(best)} ₽, заказов "
            f"{fm(lead['tot']['orders'])}{few(lead['tot'])}.")
        for c, _sc in lst[1:]:
            t = c["tot"]
            if not t["orders"]:
                log(f"      {title(c)} ({c['pay']}): {fm(t['sum'])} ₽ без единого заказа.")
            else:
                k = cpo(t) / best if best else None
                log(f"      {title(c)} ({c['pay']}): заказ {fm(cpo(t))} ₽"
                    + (f" — {times(k)} дороже" if k and k >= 1.15 else " — почти так же") + few(t) + ".")
    top = sorted(((k, v) for k, v in wins.items() if v[0]), key=lambda p: -p[1][0])
    if top:
        names = {(c["cab"], c["id"]): title(c) for c in camps}
        log("\n   Дешевле всех по отдельным моделям: " + "; ".join(f"{names[k]} — на {v[0]} из {v[1]}" for k, v in top))
    log(f"\n   Как понимать. Сравнение за {d1:%d.%m}–{d2:%d.%m}. В Ребрике и Карамане до 27.10 идёт эксперимент: "
        "одинаковые модели нарочно рекламируются по-разному (за клики / за показы) — разница кампаний там во многом и "
        "есть ответ эксперимента. Заказ с рекламы зависит и от карточки: у карточки с отзывами и рейтингом он дешевле — "
        "смотрите «все заказы карточек модели» в разделе 2. Ничего не изменено; что делать с дорогими кампаниями — "
        "решаете вы, изменения — отдельно, с пробным прогоном.")


def main():
    d1, d2 = period()
    cabs = load_tokens(arg_cabinet())
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — какие кампании лучше продвигают одни и те же модели: "
        f"{', '.join(cabs)}; {d1:%d.%m}–{d2:%d.%m} ({(d2 - d1).days + 1} дн.). Только чтение.")
    if d1 < TODAY - dt.timedelta(days=30):
        log("  ! Завершённые больше 30 дней назад кампании в список не попадают — их расход в начале периода не учтён.")
    data = {}
    for cab, out, err in each_cabinet(cabs, lambda c, t: collect(c, t, d1, d2)):
        if err is not None:
            log(f"\n✗ {cab}: {err}")
            warn_incomplete(f"{cab}: {err}")
            continue
        data[cab] = out
    camps, models = build(data)
    groups = make_groups(camps)
    g_rows, _n = report_groups(groups, models)
    m_rows, wins = report_models(camps, models)
    summary(groups, camps, wins, d1, d2)
    write_csv("wb_compare.csv", g_rows, header=["группа", "место", "кабинет", "кампания", "ID"])
    write_csv("wb_compare_models.csv", m_rows, header=["модель", "кабинет", "кампания", "ID"])
    save_log("wb_compare_report.txt")
    print("\nСохранено: wb_compare_report.txt, wb_compare.csv, wb_compare_models.csv (открыть в Excel).")


if __name__ == "__main__":
    main()

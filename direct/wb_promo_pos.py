# -*- coding: utf-8 -*-
"""
Акция WB: как изменились позиции товаров в поиске со вчерашнего дня по текущий момент. Только чтение — ничего не меняет.

Сравнивает БАЗОВЫЙ день (по умолчанию вчера, весь день) и СЕГОДНЯ (с 0:00 до сейчас) по «Джему» и воронке продаж WB:
  • цену товара в поиске (по «Джему») и среднюю цену заказа — видно, у каких товаров действует акционная цена;
  • среднюю позицию товара в поиске и видимость (по всем запросам, где товар показывался), медианную — у главных;
  • позиции по каждому поисковому запросу товара: сколько запросов поднялось и сколько опустилось;
  • переходы из поиска и заказы сегодня — против вчерашних к этому же часу (по графику спроса из wb_bids.py);
  • то же неделю назад (из wb_history.db, если есть) — для сравнения в тот же день недели.
Главный вывод — разница между товарами С акционной ценой и БЕЗ неё: общие колебания дня (выходной, погода, реклама)
у обеих групп одинаковы, а разница между ними — это и есть действие акции.

Запуск:  python wb_promo_pos.py                              (все кабинеты; ≈5–10 минут — лимит «Джема» 3 запроса в минуту)
         python wb_promo_pos.py --cabinet "Яворская"
         python wb_promo_pos.py --nm 253631693,253633739      (только эти товары)
         python wb_promo_pos.py --day 2026-10-16              (с каким днём сравнивать; по умолчанию вчера)
         python wb_promo_pos.py --day 2026-10-16 --to 2026-10-17   (два полных дня — назавтра, окончательно)
Товары — те, что отслеживает wb_positions.py (лучшие по заказам, рекламируемые, wb_watch.csv), или --nm.
Нужны категории токена: «Аналитика» (с подпиской «Джем»).
Позиция: меньше — лучше (1 — первое место в выдаче). Сегодняшний день неполный: позиции сравнимы сразу, переходы и
заказы — с поправкой на час. Окончательно — назавтра, по полному дню (--day <день до акции> --to <день акции>).
Результат: wb_promo_pos_report.txt, wb_promo_pos.csv.
"""
import datetime as dt
import sqlite3
import statistics
import sys

import wb_bids as B
import wb_positions as P
from wb_common import (TODAY, YESTERDAY, ApiError, arg_cabinet, cut, each_cabinet, load_tokens, log, save_log,
                       warn_incomplete, write_csv)

# ======================== НАСТРОЙКИ ========================
MAX_NMS = 60          # не больше стольких товаров на кабинет (каждые 50 — +4 запроса к «Джему»)
MEDIAN_NMS = 10       # медианная позиция — по стольким главным товарам (запрос на товар, ≈20 с каждый)
PRICE_DROP = 0.03     # цена ниже вчерашней на 3%+ — товар «в акции»
POS_MOVE = 1.0        # позиция изменилась хотя бы на столько мест — «лучше» / «хуже»
TOP_QUERIES = 3       # главных запросов на товар в таблице (по переходам за базовый день)
# ===========================================================

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
CUR = [TODAY]         # с каким днём сравнивать базовый: сегодня (неполный) или --to (полный день)


def arg(name):
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def day_share(day, now=None):
    """Доля дневного спроса, прошедшая к этому моменту (график заказов по часам — тот же, что у графика расхода)."""
    now = now or dt.datetime.now()
    curve = B.PACE_WE if day.weekday() >= 5 else B.PACE_WD
    if now.date() > day:
        return 1.0
    h = now.hour
    return (sum(curve[:h]) + curve[h] * now.minute / 60) / sum(curve)


def dname(d):
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


def tracked(cab):
    """Товары, которые отслеживает wb_positions.py (wb_history.db, только чтение)."""
    if not P.DB.exists():
        return []
    try:
        con = sqlite3.connect(P.DB.as_uri() + "?mode=ro", uri=True, timeout=10)
        r = con.execute("SELECT value FROM meta WHERE key=?", (f"tracked:{cab}",)).fetchone()
        con.close()
        return [int(x) for x in P.json.loads(r[0])] if r else []
    except (sqlite3.Error, ValueError):
        return []


def history(cab, nms, day):
    """Неделю назад из wb_history.db: {nm: {avg_pos, median_pos, opens, orders, search_opens}} (только чтение)."""
    if not P.DB.exists() or not nms:
        return {}
    res = {}
    try:
        con = sqlite3.connect(P.DB.as_uri() + "?mode=ro", uri=True, timeout=10)
        q = ",".join("?" * len(nms))
        for nm, ap, mp, so in con.execute(f"SELECT nm, avg_pos, median_pos, search_opens FROM product_pos_day "
                                          f"WHERE cab=? AND date=? AND nm IN ({q})", (cab, day.isoformat(), *nms)):
            res.setdefault(nm, {}).update(avg_pos=ap, median_pos=mp, search_opens=so)
        for nm, op, od in con.execute(f"SELECT nm, opens, orders FROM funnel_day WHERE cab=? AND date=? AND nm IN ({q})",
                                      (cab, day.isoformat(), *nms)):
            res.setdefault(nm, {}).update(opens=op, orders=od)
        con.close()
    except sqlite3.Error:
        return {}
    return res


def collect(cab, token, nms, base):
    """Свежие данные WB за базовый день и сегодня — во временную базу в памяти (wb_history.db не трогаю)."""
    con = sqlite3.connect(":memory:")
    con.executescript(P.SCHEMA)
    problems = []

    def step(label, fn, *a):
        try:
            return fn(*a)
        except ApiError as e:
            problems.append(f"{label}: {e}")
            log(f"  ! {label}: {e}")
            return None

    days = (base, CUR[0])
    for d in days:
        step(f"воронка за {d:%d.%m}", P.funnel_day, con, cab, token, d, set(nms))
    if not nms:          # отслеживаемых нет — главные по переходам за базовый день
        nms = [r[0] for r in con.execute("SELECT nm FROM funnel_day WHERE cab=? AND date=? ORDER BY opens DESC LIMIT ?",
                                         (cab, base.isoformat(), MAX_NMS))]
    if not nms:
        return con, [], problems
    log(f"  Товаров: {len(nms)}. Запросов к «Джему» ≈{4 * -(-len(nms) // 50) + min(MEDIAN_NMS, len(nms))}, "
        f"WB разрешает 3 в минуту.")
    for d in days:
        step(f"позиции товаров за {d:%d.%m}", P.details_day, con, cab, token, nms, d)
    for d in days:
        step(f"запросы за {d:%d.%m}", P.texts_day, con, cab, token, nms, d)
    main = [r[0] for r in con.execute(f"SELECT nm FROM funnel_day WHERE cab=? AND date=? AND nm IN "
                                      f"({','.join('?' * len(nms))}) ORDER BY orders DESC, opens DESC LIMIT ?",
                                      (cab, base.isoformat(), *nms, MEDIAN_NMS))]
    for nm in main:
        if step(f"медианная позиция {nm}", P.report_nm, con, cab, token, nm, base, CUR[0]) is False:
            break
    con.commit()
    return con, nms, problems


def one(con, cab, nm, d):
    r = con.execute("SELECT p.avg_pos, p.median_pos, p.visibility, p.search_opens, p.search_orders, p.is_ad, "
                    "f.opens, f.orders, f.avg_price FROM (SELECT ? AS nm) k "
                    "LEFT JOIN product_pos_day p ON p.cab=? AND p.nm=k.nm AND p.date=? "
                    "LEFT JOIN funnel_day f ON f.cab=? AND f.nm=k.nm AND f.date=?",
                    (nm, cab, d.isoformat(), cab, d.isoformat())).fetchone()
    keys = ("avg_pos", "median_pos", "visibility", "search_opens", "search_orders", "is_ad", "opens", "orders", "avg_price")
    out = dict(zip(keys, r)) if r else {}
    prices = [x[0] for x in con.execute("SELECT price FROM query_day WHERE cab=? AND nm=? AND date=? AND price>0",
                                        (cab, nm, d.isoformat()))]
    out["price"] = statistics.median(prices) if prices else None
    return out


def queries(con, cab, nm, base):
    """[(запрос, частота, позиция в базовый день, сегодня, переходы в базовый день)] — запросы, что есть в оба дня."""
    rows = con.execute(
        "SELECT a.query, COALESCE(a.week_freq, a.freq), COALESCE(a.median_pos, a.avg_pos), "
        "COALESCE(b.median_pos, b.avg_pos), a.opens FROM query_day a JOIN query_day b "
        "ON b.cab=a.cab AND b.nm=a.nm AND b.query=a.query AND b.date=? "
        "WHERE a.cab=? AND a.nm=? AND a.date=?", (CUR[0].isoformat(), cab, nm, base.isoformat())).fetchall()
    return [r for r in rows if r[2] and r[3]]


def fp(v, nd=0):
    return "—" if v is None else (f"{v:.{nd}f}" if nd else f"{v:.0f}")


def arrow(before, now):
    """Позиция: меньше — лучше."""
    if before is None or now is None:
        return " "
    return "↑" if now < before - POS_MOVE + 1e-9 else "↓" if now > before + POS_MOVE - 1e-9 else "="


def tempo(now, before, share):
    """Сегодня к этому часу против базового дня, приведённого к той же доле спроса: +0.25 — на 25% больше."""
    if before is None or now is None or share <= 0:
        return None
    exp = before * share
    return None if exp < 1 else now / exp - 1


def pct(x):
    return "—" if x is None else f"{x * 100:+.0f}%"


def analyze(cab, con, nms, base, problems):
    share_t = day_share(CUR[0])
    week = CUR[0] - dt.timedelta(days=7)
    hist = history(cab, nms, week)
    rows, csv = [], []
    for nm in nms:
        a, t = one(con, cab, nm, base), one(con, cab, nm, CUR[0])
        if all(a.get(k) is None for k in ("avg_pos", "opens", "price")) and all(
                t.get(k) is None for k in ("avg_pos", "opens", "price")):
            continue
        pa, pt = a.get("price") or a.get("avg_price"), t.get("price") or t.get("avg_price")
        promo = None if not (pa and pt) else (pt <= pa * (1 - PRICE_DROP))
        qs = queries(con, cab, nm, base)
        up = sum(1 for q in qs if q[3] < q[2] - POS_MOVE + 1e-9)
        down = sum(1 for q in qs if q[3] > q[2] + POS_MOVE - 1e-9)
        dq = statistics.median([q[3] - q[2] for q in qs]) if qs else None
        prod = con.execute("SELECT vendor FROM products WHERE cab=? AND nm=?", (cab, nm)).fetchone()
        w = hist.get(nm) or {}
        r = {"nm": nm, "vendor": (prod[0] if prod else "") or "", "promo": promo, "pa": pa, "pt": pt, "a": a, "t": t,
             "qs": qs, "up": up, "down": down, "dq": dq, "week": w,
             "d_avg": (t["avg_pos"] - a["avg_pos"]) if a.get("avg_pos") and t.get("avg_pos") else None,
             "tempo_search": tempo(t.get("search_opens"), a.get("search_opens"), share_t),
             "tempo_orders": tempo(t.get("orders"), a.get("orders"), share_t)}
        rows.append(r)
        csv.append({"кабинет": cab, "артикул WB": nm, "артикул продавца": r["vendor"],
                    "в акции (цена ниже)": "" if promo is None else ("да" if promo else "нет"),
                    f"цена {base:%d.%m}": fp(pa), f"цена {CUR[0]:%d.%m}": fp(pt),
                    f"ср. позиция {base:%d.%m}": fp(a.get("avg_pos"), 1), f"ср. позиция {CUR[0]:%d.%m}": fp(t.get("avg_pos"), 1),
                    f"медиана {base:%d.%m}": fp(a.get("median_pos"), 1), f"медиана {CUR[0]:%d.%m}": fp(t.get("median_pos"), 1),
                    f"ср. позиция {week:%d.%m}": fp(w.get("avg_pos"), 1),
                    f"видимость {base:%d.%m}": fp(a.get("visibility")), f"видимость {CUR[0]:%d.%m}": fp(t.get("visibility")),
                    "запросов выше": up, "запросов ниже": down, "медиана изменения по запросам": fp(dq, 1),
                    f"переходы из поиска {base:%d.%m}": fp(a.get("search_opens")),
                    f"переходы из поиска {CUR[0]:%d.%m} (к этому часу)": fp(t.get("search_opens")),
                    "темп переходов из поиска": pct(r["tempo_search"]),
                    f"заказы {base:%d.%m}": fp(a.get("orders")), f"заказы {CUR[0]:%d.%m} (к этому часу)": fp(t.get("orders")),
                    "темп заказов": pct(r["tempo_orders"]), "реклама": "да" if (a.get("is_ad") or t.get("is_ad")) else ""})
        csv[-1] = {k: (v.replace(".", ",") if isinstance(v, str) and v.replace(".", "", 1).lstrip("-").isdigit() else v)
                   for k, v in csv[-1].items()}          # дробные — с запятой, как ждёт русский Excel

    log(f"  Сравнение: {dname(base)} (весь день) → {dname(CUR[0])} " + ("(весь день)" if share_t >= 1 else
        f"до {dt.datetime.now():%H:%M} (прошло ≈{share_t:.0%} дневного спроса)") + ". Позиция: меньше — лучше.")
    if base.weekday() < 5 <= CUR[0].weekday():
        log("  Внимание: будний день против выходного — переходы и заказы по выходным распределены по-другому; "
            "позиции сравнимы, а разницу групп «в акции / без акции» день недели не искажает.")
    have_today = sum(1 for r in rows if r["t"].get("avg_pos") is not None)
    if rows and not have_today:
        warn_incomplete(f"{cab}: WB ещё не отдал позиции за сегодня — запустите позже (или завтра: --day {base})")
    if not rows:
        log("  Нет данных по товарам.")
        return [], csv

    def group(sel):
        g = [r for r in rows if sel(r)]
        d = [r["d_avg"] for r in g if r["d_avg"] is not None]
        better = sum(1 for x in d if x <= -POS_MOVE)
        worse = sum(1 for x in d if x >= POS_MOVE)
        ts = [r["tempo_search"] for r in g if r["tempo_search"] is not None]
        to = [r["tempo_orders"] for r in g if r["tempo_orders"] is not None]
        qu, qd = sum(r["up"] for r in g), sum(r["down"] for r in g)
        drops = [r["pt"] / r["pa"] - 1 for r in g if r["pa"] and r["pt"]]
        return {"n": len(g), "nd": len(d), "med": statistics.median(d) if d else None, "better": better, "worse": worse,
                "same": len(d) - better - worse, "ts": statistics.median(ts) if ts else None,
                "to": statistics.median(to) if to else None, "qu": qu, "qd": qd,
                "drop": statistics.median(drops) if drops else None}

    gp, gn, gu = group(lambda r: r["promo"] is True), group(lambda r: r["promo"] is False), group(lambda r: r["promo"] is None)
    log("")
    log(f"  Цена: ниже вчерашней на {PRICE_DROP:.0%}+ у {gp['n']} из {len(rows)} товаров"
        + (f" (в среднем {gp['drop'] * 100:+.0f}%)" if gp["n"] and gp["drop"] is not None else "")
        + f"; без изменений — {gn['n']}" + (f"; цену не узнать (нет показов и заказов) — {gu['n']}" if gu["n"] else "") + ".")
    for title, g in (("В акции (цена ниже)", gp), ("Без акции", gn)):
        if not g["n"]:
            continue
        log(f"  {title}: {g['n']} тов.")
        if g["nd"]:
            log(f"      средняя позиция: лучше у {g['better']}, хуже у {g['worse']}, так же у {g['same']}; "
                f"медиана изменения {g['med']:+.1f} места" + (" (меньше — лучше)" if g["med"] else ""))
        if g["qu"] or g["qd"]:
            log(f"      запросы: поднялись {g['qu']}, опустились {g['qd']}")
        log(f"      переходы из поиска — темп к вчера: {pct(g['ts'])}; заказы — темп: {pct(g['to'])}")
    log("")
    log("  Выводы:")
    for line in conclusions(gp, gn, share_t, base):
        log("   • " + line)

    log("")
    log(f"  {'артикул':>11} {'арт. продавца':16} {'цена было→сейчас':>17} {'ср.поз.':>13} {'медиана':>13} "
        f"{'нед.назад':>9} {'запр.↑/↓':>8} {'переходы поиск':>15} {'заказы':>9}")
    rows.sort(key=lambda r: (r["promo"] is not True, -(r["a"].get("orders") or 0), -(r["a"].get("opens") or 0)))
    for r in rows:
        a, t = r["a"], r["t"]
        mark = "%" if r["promo"] else " "
        log(f"  {r['nm']:>11} {cut(r['vendor'], 15):15}{mark} {fp(r['pa']):>7}→{fp(r['pt']):<7}"
            f" {fp(a.get('avg_pos'), 1):>5}→{fp(t.get('avg_pos'), 1):<5}{arrow(a.get('avg_pos'), t.get('avg_pos'))}"
            f" {fp(a.get('median_pos'), 1):>5}→{fp(t.get('median_pos'), 1):<5}{arrow(a.get('median_pos'), t.get('median_pos'))}"
            f" {fp(r['week'].get('avg_pos'), 1):>9} {r['up']:>3}/{r['down']:<4}"
            f" {fp(a.get('search_opens')):>5}/{fp(t.get('search_opens')):<4}{pct(r['tempo_search']):>5}"
            f" {fp(a.get('orders')):>3}/{fp(t.get('orders')):<3}{'' if r['tempo_orders'] is None else pct(r['tempo_orders'])}")
    log(f"  (% — в акции; «было» — {dname(base)} целиком, «сейчас» — {dname(CUR[0])} "
        + ("целиком" if share_t >= 1 else "к этому часу") + "; переходы и заказы: было/сейчас и темп"
        + ("" if share_t >= 1 else " — сейчас против «было» к этому же часу") + ")")
    promo_rows = [r for r in rows if r["promo"] and r["qs"]]
    if promo_rows:
        log("")
        log(f"  Главные запросы товаров в акции (позиция {base:%d.%m} → {CUR[0]:%d.%m}):")
        for r in promo_rows:
            top = sorted(r["qs"], key=lambda q: -(q[4] or 0))[:TOP_QUERIES]
            log(f"    {r['nm']} {cut(r['vendor'], 20)}: " + "; ".join(
                f"«{cut(q[0], 28)}» {q[2]:.0f}→{q[3]:.0f}{arrow(q[2], q[3])}" for q in top))
    if problems:
        warn_incomplete(f"{cab}: часть запросов к WB не прошла ({len(problems)}) — подробности выше")
    return rows, csv


def conclusions(gp, gn, share, base):
    out = []
    if not gp["n"]:
        out.append("Ни у одного отслеживаемого товара цена не стала ниже вчерашней — либо акция ещё не применилась "
                   "(WB обновляет цены в поиске с задержкой), либо товары акции не в отслеживаемых: укажите их через --nm.")
        return out
    if gp["med"] is not None and gn["med"] is not None:
        diff = gp["med"] - gn["med"]
        if diff <= -POS_MOVE:
            out.append(f"Акция подняла товары в поиске: у товаров в акции позиция изменилась на {gp['med']:+.1f}, у товаров "
                       f"без акции — на {gn['med']:+.1f}; разница ≈{-diff:.1f} места в пользу акции.")
        elif diff >= POS_MOVE:
            out.append(f"Товары в акции просели относительно остальных ({gp['med']:+.1f} против {gn['med']:+.1f}) — "
                       "проверьте остатки и рекламу этих товаров: сама по себе скидка позицию не ухудшает.")
        else:
            out.append(f"Заметного сдвига позиций от акции пока нет: в акции {gp['med']:+.1f}, без акции {gn['med']:+.1f} места.")
    elif gp["med"] is not None:
        out.append(f"Позиция товаров в акции изменилась на {gp['med']:+.1f} места (меньше — лучше); сравнить не с чем — "
                   "товаров без акции с позициями нет.")
    if gp["ts"] is not None:
        rel = f" (без акции — {pct(gn['ts'])})" if gn["ts"] is not None else ""
        out.append(f"Переходы из поиска у товаров в акции — {pct(gp['ts'])} к вчерашнему темпу{rel}.")
    if gp["to"] is not None:
        rel = f" (без акции — {pct(gn['to'])})" if gn["to"] is not None else ""
        out.append(f"Заказы у товаров в акции — {pct(gp['to'])} к вчерашнему темпу{rel}.")
    if share < 0.35:
        out.append(f"Прошло лишь ≈{share:.0%} дневного спроса — цифры переходов и заказов ещё шумные; позиции надёжнее. "
                   "Повторите проверку вечером.")
    if share < 1:
        out.append(f"Окончательно — по полному дню, завтра: python wb_promo_pos.py --day {base} --to {CUR[0]}")
    return out


def main():
    base = YESTERDAY
    if arg("--day"):
        try:
            base = dt.date.fromisoformat(arg("--day"))
        except ValueError:
            sys.exit("--day — дата в виде ГГГГ-ММ-ДД")
        if base >= TODAY:
            sys.exit("--day — день до сегодняшнего")
    if arg("--to"):
        try:
            CUR[0] = dt.date.fromisoformat(arg("--to"))
        except ValueError:
            sys.exit("--to — дата в виде ГГГГ-ММ-ДД")
        if not base < CUR[0] <= TODAY:
            sys.exit("--to — день после --day и не позже сегодняшнего")
    only = [int(x) for x in (arg("--nm") or "").replace(" ", "").split(",") if x.strip().isdigit()]
    cabs = load_tokens(arg_cabinet())
    log(f"Акция: позиции в поиске {dname(base)} → {dname(CUR[0])}"
        + (f" до {dt.datetime.now():%H:%M}" if CUR[0] == TODAY else "") + " (только чтение). "
        f"Кабинетов: {len(cabs)}.")
    allcsv = []

    def run(cab, token):
        log(f"\n{'═' * 20} {cab} {'═' * 20}")
        nms = (only or tracked(cab))[:MAX_NMS]
        con, nms, problems = collect(cab, token, nms, base)
        _, csv = analyze(cab, con, nms, base, problems)
        con.close()
        return csv

    for cab, csv, err in each_cabinet(cabs, run):
        if err is not None:
            log(f"\n✗ {cab}: {err}")
            warn_incomplete(f"{cab}: {err}")
            continue
        allcsv += csv or []
    write_csv("wb_promo_pos.csv", allcsv)
    save_log("wb_promo_pos_report.txt")
    print("\nСохранено: wb_promo_pos_report.txt, wb_promo_pos.csv (открыть в Excel).")


if __name__ == "__main__":
    main()

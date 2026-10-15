# -*- coding: utf-8 -*-
"""
Wildberries: реклама «до» и «после» нашего управления (автопилот wb_bids.py с 13.10.2026 00:10, часовая проверка
wb_guard.py, с 16.10 — частая проверка лимита). Только чтение — в кабинетах ничего не меняет.

Что показывает (по каждому кабинету):
  1. Кабинет целиком, в среднем за день: расход, показы, клики, цена клика, заказы с рекламы, цена заказа,
     доля рекламы в рекламных заказах (ДРР), все заказы кабинета и общий ДРР (реклама ÷ все заказы).
  2. Каждая кампания: расход в день (по дням, когда она тратила), CTR, цена клика, заказы, цена заказа, ДРР — до и
     после, и вывод: лучше / хуже / в пределах случайности / рано судить.
  3. Можно ли уже доверять разнице: сколько заказов было бы на расход «после», если бы реклама работала как «до»,
     и обычный случайный разброс.

Заказы — оформленные (с рекламы — по статистике WB, все — по воронке продаж), не выкупы. Расход и заказы
сегодняшнего дня неполные — «после» заканчивается вчера.
Сравнивать лучше целые недели: спрос по дням недели разный. «После» короче 7 дней — вывод предварительный.

Запуск:  python wb_before_after.py                            (до: 14 дней до 13.10; после: 13.10 — вчера)
         python wb_before_after.py 2026-10-16                 (после — с 16.10, до — 14 дней перед ним)
         python wb_before_after.py 2026-10-13 21              (до — 21 день)
         python wb_before_after.py --cabinet "Яворская"
Нужны категории токена «Продвижение» и «Аналитика». WB отдаёт статистику рекламы раз в 20 с — 1–2 минуты.
Результат: вывод на экран + wb_before_after.txt, wb_before_after.csv
"""
import datetime as dt
import math
import sys
from collections import defaultdict

from wb_common import (ADV, YESTERDAY, ApiError, arg_cabinet, call, campaigns, chunks, cut, each_cabinet, fm,
                       funnel, load_tokens, log, num, pace, save_log, warn_incomplete, write_csv)

# ---------------- НАСТРОЙКИ ----------------
START = dt.date(2026, 10, 13)  # первый wb_bids.py --apply — 13.10.2026 00:10
BEFORE_DAYS = 14               # сколько дней «до» (две целые недели — дни недели уравновешены)
MIN_ORDERS = 20                # меньше заказов в периоде — вывод по кампании предварительный
MIN_SPEND = 100                # кампании, потратившие меньше (до + после, ₽), — одной строкой в конце
# Что менялось после START (для пояснений в отчёте)
CHANGES = [
    ("13.10", "первый запуск автопилота: ставки по рекомендации WB, дневные лимиты, убраны товары < 15 шт"),
    ("13.10", "расписание: автопилот 8:00 / 13:00 / 18:00, часовая проверка ставок и пополнений"),
    ("14.10", "эксперимент Ребрик/Караман: новые кампании за показы (TC26, 2101, пуховики), график расхода"),
    ("15.10", "недельные потолки пополнений подняты; утром кампании за показы перерасходовали лимиты"),
    ("16.10", "частая проверка лимита каждые 5 мин, ночная пауза 23:30–7:00"),
]
# -------------------------------------------

_dates = []
for _a in sys.argv[1:]:
    try:
        _dates.append(dt.date.fromisoformat(_a))
    except ValueError:
        pass
if _dates:
    START = _dates[0]
_days = [int(a) for a in sys.argv[1:] if a.isdigit()]
if _days:
    BEFORE_DAYS = max(1, min(_days[0], 31))
B1, B2 = START - dt.timedelta(days=BEFORE_DAYS), START - dt.timedelta(days=1)
A1, A2 = START, YESTERDAY
PAY = {"cpm": "показы", "cpc": "клики"}
KEYS = ("views", "clicks", "sum", "orders", "sum_price")


def camp_stats(token, ids, d1, d2):
    """{кампания: {день: {views, clicks, sum, orders, sum_price}}} по статистике рекламы WB."""
    res = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    a = d1
    while a <= d2:
        b = min(d2, a + dt.timedelta(days=30))
        for part in chunks(ids, 50):
            pace(token, "fullstats", 21)
            try:
                data = call("GET", ADV + "/adv/v3/fullstats", token,
                            params={"ids": ",".join(map(str, part)), "beginDate": a.isoformat(), "endDate": b.isoformat()},
                            what="Статистика рекламы")
            except ApiError as e:
                warn_incomplete(f"статистика рекламы {a:%d.%m}–{b:%d.%m} по {len(part)} кампаниям: {e}")
                continue
            for item in data or []:
                aid = item.get("advertId")
                for day in item.get("days") or []:
                    d = str(day.get("date") or "")[:10]
                    for app in day.get("apps") or []:
                        for n in app.get("nms") or []:
                            for k in KEYS:
                                res[aid][d][k] += num(n.get(k))
        a = b + dt.timedelta(days=1)
    return res


def totals(days):
    """Сумма по дням + число дней с расходом."""
    t = defaultdict(float)
    for v in days.values():
        for k in KEYS:
            t[k] += v[k]
    t["active"] = sum(1 for v in days.values() if v["sum"] > 0)
    return t


def ratios(t, days):
    """Показатели периода: в день (на days дней) и отношения."""
    return {"day_sum": t["sum"] / days if days else None, "day_orders": t["orders"] / days if days else None,
            "ctr": t["clicks"] / t["views"] * 100 if t["views"] else None,
            "cpc": t["sum"] / t["clicks"] if t["clicks"] else None,
            "cpm": t["sum"] / t["views"] * 1000 if t["views"] else None,
            "cpo": t["sum"] / t["orders"] if t["orders"] else None,
            "drr": t["sum"] / t["sum_price"] * 100 if t["sum_price"] else None,
            "cr": t["orders"] / t["clicks"] * 100 if t["clicks"] else None}


def verdict(b, a):
    """Сравнение отдачи: заказов «после» против ожидаемых при отдаче «до» (с учётом случайности обоих периодов)."""
    pb, pa = b["orders"], a["orders"]
    if not b["sum"]:
        return "до — не было расхода (новая кампания)", None
    if not a["sum"]:
        return "после — не тратила", None
    if pb < 1:
        return "до — ни одного заказа, сравнивать не с чем", None
    exp = pb / b["sum"] * a["sum"]
    sd = math.sqrt(exp + exp ** 2 / pb)
    lo, hi = max(0.0, exp - 2 * sd), exp + 2 * sd
    if pa > hi:
        v = "лучше: заказов заметно больше, чем при прежней отдаче"
    elif pa < lo:
        v = "хуже: заказов заметно меньше, чем при прежней отдаче"
    else:
        v = "в пределах случайности"
    if pb + pa < MIN_ORDERS and v != "в пределах случайности":
        v += " (мало заказов — предварительно)"
    return v, (exp, lo, hi)


def chg(b, a, d=0, suffix="", lower_better=None):
    """«было → стало (±%, лучше/хуже)»; lower_better None — без оценки (расход, показы: больше — не значит лучше)."""
    if b is None and a is None:
        return "—"
    s = f"{fm(b, d, suffix)} → {fm(a, d, suffix)}"
    if b and a is not None:
        p = (a / b - 1) * 100
        if abs(p) < 0.5:
            return s + " (без изменений)"
        good = None if lower_better is None or abs(p) < 1 else (p < 0) == lower_better
        s += f" ({p:+.0f}%{'' if good is None else ', лучше' if good else ', хуже'})"
    return s


def analyze(cab, token):
    log(f"\n{'═' * 22} {cab} {'═' * 22}")
    camps = campaigns(token, statuses=(7, 9, 11), changed_since=B1.isoformat())
    if not camps:
        log("Кампаний нет.")
        return []
    st_b = camp_stats(token, list(camps), B1, B2)
    st_a = camp_stats(token, list(camps), A1, A2)
    try:
        f_b, f_a = funnel(token, B1, B2), funnel(token, A1, A2)
    except ApiError as e:
        warn_incomplete(f"воронка продаж: {e} — все заказы кабинета не посчитаны")
        f_b, f_a = None, None
    nb, na = (B2 - B1).days + 1, (A2 - A1).days + 1

    # 1. Кабинет целиком
    tb, ta = defaultdict(float), defaultdict(float)
    for aid in camps:
        for src, dst in ((st_b, tb), (st_a, ta)):
            t = totals(src.get(aid, {}))
            for k in KEYS:
                dst[k] += t[k]
    rb, ra = ratios(tb, nb), ratios(ta, na)
    log(f"До: {B1:%d.%m}–{B2:%d.%m} ({nb} дн.).  После: {A1:%d.%m}–{A2:%d.%m} ({na} дн.).")
    per_day = defaultdict(float)
    for aid in camps:
        for d, v in st_b.get(aid, {}).items():
            per_day[d] += v["sum"]
    on_b = sum(1 for x in per_day.values() if x >= MIN_SPEND)     # день с рекламой — от 100 ₽ по кабинету
    if on_b < nb:
        log(f"  В «до» реклама кабинета шла {on_b} дн. из {nb} — средние за день «до» занижены, сравнивайте кампании.")
    if na < 7:
        log(f"  «После» всего {na} дн. — меньше недели: вывод предварительный, дни недели не уравновешены.")
    log("\n1. Кабинет целиком, в среднем за день (было → стало):")
    rows_c = [("Расход на рекламу", chg(rb["day_sum"], ra["day_sum"], 0, " ₽")),
              ("Показы", chg(tb["views"] / nb, ta["views"] / na)),
              ("Клики", chg(tb["clicks"] / nb, ta["clicks"] / na)),
              ("CTR (клики из показов)", chg(rb["ctr"], ra["ctr"], 2, "%", lower_better=False)),
              ("Цена клика", chg(rb["cpc"], ra["cpc"], 1, " ₽", lower_better=True)),
              ("Заказы с рекламы", chg(rb["day_orders"], ra["day_orders"], 1, lower_better=False)),
              ("Цена заказа с рекламы", chg(rb["cpo"], ra["cpo"], 0, " ₽", lower_better=True)),
              ("ДРР рекламы (расход ÷ её заказы)", chg(rb["drr"], ra["drr"], 1, "%", lower_better=True))]
    if f_b is not None:
        ob, oa = sum(v["orderCount"] for v in f_b.values()), sum(v["orderCount"] for v in f_a.values())
        sb, sa = sum(v["orderSum"] for v in f_b.values()), sum(v["orderSum"] for v in f_a.values())
        rows_c += [("Все заказы кабинета, шт", chg(ob / nb, oa / na, 1, lower_better=False)),
                   ("Все заказы кабинета, ₽", chg(sb / nb, sa / na, 0, " ₽", lower_better=False)),
                   ("ДРР общий (реклама ÷ все заказы)", chg(tb["sum"] / sb * 100 if sb else None,
                                                             ta["sum"] / sa * 100 if sa else None, 1, "%",
                                                             lower_better=True))]
    for k, v in rows_c:
        log(f"  {k:36}{v}")
    v, rng = verdict(tb, ta)
    log("\n  Можно ли доверять разнице:")
    if rng:
        exp, lo, hi = rng
        log(f"  если бы реклама работала как «до», на расход «после» пришлось бы ≈{exp:.0f} заказов "
            f"(обычный разброс {lo:.0f}–{hi:.0f}); по факту {ta['orders']:.0f} — {v}.")
    else:
        log(f"  {v}.")

    # 2. Кампании
    log("\n2. Кампании (в день — по дням, когда кампания тратила; было → стало):")
    out, small = [], []
    order = sorted(camps, key=lambda i: -(totals(st_a.get(i, {}))["sum"] + totals(st_b.get(i, {}))["sum"]))
    for aid in order:
        t_b, t_a = totals(st_b.get(aid, {})), totals(st_a.get(aid, {}))
        if not t_b["sum"] and not t_a["sum"]:
            continue
        if t_b["sum"] + t_a["sum"] < MIN_SPEND:
            small.append((aid, t_b["sum"] + t_a["sum"]))
            continue
        s = camps[aid].get("settings") or {}
        name, pay = s.get("name") or str(aid), PAY.get(s.get("payment_type"), s.get("payment_type") or "?")
        r_b, r_a = ratios(t_b, t_b["active"]), ratios(t_a, t_a["active"])
        vv, rng = verdict(t_b, t_a)
        log(f"\n  «{cut(name, 40)}» {aid} (за {pay}): до — {t_b['active']:.0f} дн. с расходом, после — {t_a['active']:.0f}")
        log(f"    расход в день     {chg(r_b['day_sum'], r_a['day_sum'], 0, ' ₽')}")
        log(f"    CTR               {chg(r_b['ctr'], r_a['ctr'], 2, '%', lower_better=False)}")
        log(f"    цена клика        {chg(r_b['cpc'], r_a['cpc'], 1, ' ₽', lower_better=True)}")
        log(f"    заказы            {t_b['orders']:.0f} → {t_a['orders']:.0f} (в день {fm(r_b['day_orders'], 1)} → "
            f"{fm(r_a['day_orders'], 1)})")
        log(f"    цена заказа       {chg(r_b['cpo'], r_a['cpo'], 0, ' ₽', lower_better=True)}")
        log(f"    ДРР               {chg(r_b['drr'], r_a['drr'], 1, '%', lower_better=True)}")
        log(f"    итог: {vv}" + (f" (ожидалось ≈{rng[0]:.0f}, разброс {rng[1]:.0f}–{rng[2]:.0f})" if rng else ""))
        for per, t, r in (("до", t_b, r_b), ("после", t_a, r_a)):
            out.append({"кабинет": cab, "кампания": aid, "название": name, "оплата": pay, "период": per,
                        "дней с расходом": t["active"], "расход ₽": round(t["sum"], 2), "показы": t["views"],
                        "клики": t["clicks"], "заказы": t["orders"], "сумма заказов ₽": round(t["sum_price"], 2),
                        "расход в день ₽": r["day_sum"], "CTR %": r["ctr"], "цена клика ₽": r["cpc"],
                        "цена заказа ₽": r["cpo"], "ДРР %": r["drr"], "итог": vv})
    if small:
        log(f"\n  Почти без расхода (меньше {MIN_SPEND} ₽ за оба периода): "
            + ", ".join(f"«{cut((camps[a].get('settings') or {}).get('name') or a, 30)}» {fm(x)} ₽" for a, x in small))
    return out


def main():
    if A2 < A1:
        sys.exit(f"«После» начинается {A1:%d.%m}, а полных дней с тех пор ещё нет — запустите завтра.")
    cabs = load_tokens(arg_cabinet())
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — реклама WB до и после управления (с {START:%d.%m.%Y})")
    log("Что менялось:")
    for d, what in CHANGES:
        log(f"  {d} — {what}")
    log("Заказы — оформленные, не выкупы. Кампании Ребрика и Карамана созданы 08–11.10, а TC26, 2101 и пуховики за "
        "показы — 14.10: у них «до» короткое или его нет.")
    rows = []
    for cab, res, err in each_cabinet(cabs, analyze):
        if err is not None:
            log(f"\n{cab}: ! {err}")
            continue
        rows += res or []
    write_csv("wb_before_after.csv", rows)
    save_log("wb_before_after.txt")
    print("\nСохранено: wb_before_after.txt, wb_before_after.csv")


if __name__ == "__main__":
    main()

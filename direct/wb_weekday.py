# -*- coding: utf-8 -*-
"""
Реклама и заказы Wildberries по дням недели и часам — есть ли «дешёвые» и «дорогие» дни.
Только отчёт: в кабинете ничего не меняет.

Что показывает (по каждому кабинету, за последние N дней, по умолчанию 91 = 13 недель):
  1. Реклама по дням недели: расход, клики, заказы в среднем за день, цена клика, конверсия, цена заказа —
     и устойчива ли разница или это случайность (сравнение с тем, сколько заказов «должно было быть»
     при средней цене заказа; |z| ≥ 2 — разница устойчивая).
  2. Все заказы кабинета (реклама + органика + FBS) по дням недели и по часам оформления — когда покупают.
     Почасовой статистики рекламы WB не отдаёт, поэтому часы — только справка, не основание менять ставки.
  3. Спрос по дням недели из «Джема» (частотность запросов) — если wb_positions.py уже накопил историю.
  4. Вывод: если какой-то день устойчиво дороже/дешевле на 20%+, его можно учесть дневным лимитом
     (в «дешёвые» дни +20%, в «дорогие» −20%), а не ставкой — ставка и так не выше окупаемой.

Запуск:  python wb_weekday.py              (все кабинеты, 91 день)
         python wb_weekday.py 63          (за 63 дня)
         python wb_weekday.py --cabinet "Яворская"
Нужны категории токена: «Продвижение» (статистика рекламы), «Статистика» (заказы с временем оформления).
Время: «Статистика» WB отдаёт время заказа по Москве.
Результат: wb_weekday_report.txt, wb_weekday.csv, wb_weekday_hours.csv
"""
import datetime as dt
import math
import sqlite3
from collections import defaultdict

from wb_common import (HERE, YESTERDAY, ApiError, ad_stats, arg_cabinet, arg_int, campaigns, fm, load_tokens, log,
                       save_log, stat_rows, write_csv)

# ---------------- НАСТРОЙКИ ----------------
DEFAULT_DAYS = 91           # 13 недель: по ≈13 дней на каждый день недели
STAT_MAX_DAYS = 90          # «Статистика» WB отдаёт заказы не старше 90 дней
Z_STABLE = 2.0              # |z| ≥ 2 — разница не похожа на случайность
MIN_DIFF = 0.2              # предлагать учитывать день, только если цена заказа отличается на 20% и больше
MIN_ORDERS_DAY = 30         # меньше рекламных заказов на день недели за период — судить рано
# -------------------------------------------

WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def wd_of(iso):
    return dt.date.fromisoformat(iso[:10]).weekday()


def ad_by_weekday(token, d1, d2):
    ids = list(campaigns(token, statuses=(7, 9, 11), changed_since=d1.isoformat()))
    if not ids:
        log("  Рекламных кампаний за период нет.")
        return None, {}
    log(f"  Кампаний: {len(ids)}. Загружаю статистику рекламы по дням (WB — 3 запроса в минуту)…")
    by_day_nm, _ = ad_stats(token, ids, d1, d2)
    days = defaultdict(lambda: defaultdict(float))
    for (d, _nm), v in by_day_nm.items():
        if d and d1.isoformat() <= d <= d2.isoformat():
            for k in ("views", "clicks", "sum", "orders"):
                days[d][k] += v[k]
    wd = {i: defaultdict(float) for i in range(7)}
    for d, v in days.items():
        w = wd[wd_of(d)]
        w["days"] += 1
        for k, x in v.items():
            w[k] += x
    return wd, days


def ad_table(wd):
    tot = defaultdict(float)
    for w in wd.values():
        for k, x in w.items():
            tot[k] += x
    if not tot["orders"] or not tot["sum"]:
        log("  Рекламных заказов за период нет — сравнивать нечего.")
        return [], []
    cpo_all = tot["sum"] / tot["orders"]
    log(f"\n=== 1. Реклама по дням недели (в среднем за день) — всего {fm(tot['orders'])} рекл. заказов, "
        f"средняя цена заказа {fm(cpo_all)} ₽ ===")
    log(f"  {'день':5}{'дней':>5}{'расход ₽':>10}{'клики':>8}{'заказы':>8}{'клик ₽':>8}{'конв.':>7}"
        f"{'заказ ₽':>9}{'к среднему':>11}{'z':>6}  вывод")
    rows, notable = [], []
    for i in range(7):
        w = wd[i]
        n = w["days"] or 1
        exp = w["sum"] / cpo_all            # сколько заказов было бы при средней цене заказа
        z = (w["orders"] - exp) / math.sqrt(exp) if exp > 0 else 0.0
        cpo = w["sum"] / w["orders"] if w["orders"] else None
        diff = (cpo / cpo_all - 1) if cpo else None
        if w["orders"] < MIN_ORDERS_DAY:
            verdict = "мало данных"
        elif abs(z) >= Z_STABLE and diff is not None and abs(diff) >= MIN_DIFF:
            verdict = "дешевле — устойчиво" if diff < 0 else "дороже — устойчиво"
            notable.append((WD[i], diff, z))
        elif abs(z) >= Z_STABLE:
            verdict = "разница устойчива, но меньше 20%"
        else:
            verdict = "как обычно (в пределах случайности)"
        log(f"  {WD[i]:5}{int(w['days']):>5}{fm(w['sum'] / n):>10}{fm(w['clicks'] / n):>8}{fm(w['orders'] / n, 1):>8}"
            f"{fm(w['sum'] / w['clicks'] if w['clicks'] else None, 1):>8}"
            f"{fm(w['orders'] / w['clicks'] * 100 if w['clicks'] else None, 1, '%'):>7}{fm(cpo):>9}"
            f"{(f'{diff:+.0%}' if diff is not None else '—'):>11}{z:>6.1f}  {verdict}")
        rows.append({"день недели": WD[i], "дней": int(w["days"]), "расход ₽": round(w["sum"]),
                     "клики": round(w["clicks"]), "заказы реклама": round(w["orders"]),
                     "цена клика ₽": round(w["sum"] / w["clicks"], 2) if w["clicks"] else "",
                     "конверсия %": round(w["orders"] / w["clicks"] * 100, 2) if w["clicks"] else "",
                     "цена заказа ₽": round(cpo) if cpo else "", "к среднему %": round(diff * 100) if diff is not None else "",
                     "z": round(z, 2), "вывод": verdict})
    return rows, notable


def orders_by_time(token, d1, d2):
    """Все заказы кабинета по дням недели и часам (по времени оформления, Москва)."""
    start = max(d1, YESTERDAY - dt.timedelta(days=STAT_MAX_DAYS - 1))
    rows = stat_rows(token, "orders", start)
    seen, by_wd, by_hour, grid = set(), defaultdict(float), defaultdict(float), defaultdict(float)
    days_wd = defaultdict(set)
    for r in rows:
        ts = str(r.get("date") or "")
        if len(ts) < 13 or not (start.isoformat() <= ts[:10] <= d2.isoformat()):
            continue
        key = r.get("srid") or (ts, r.get("nmId"), r.get("barcode"))
        if key in seen:
            continue
        seen.add(key)
        w, h = wd_of(ts), int(ts[11:13])
        by_wd[w] += 1
        by_hour[h] += 1
        grid[(w, h // 3)] += 1
        days_wd[w].add(ts[:10])
    return start, by_wd, by_hour, grid, days_wd


def jam_by_weekday(cab, d1, d2):
    db = HERE / "wb_history.db"
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        per_day = con.execute("SELECT date, SUM(freq) FROM query_day WHERE cab=? AND date BETWEEN ? AND ? "
                              "AND freq IS NOT NULL GROUP BY date", (cab, d1.isoformat(), d2.isoformat())).fetchall()
    except sqlite3.Error:
        return None
    acc = defaultdict(list)
    for d, f in per_day:
        if f:
            acc[wd_of(d)].append(f)
    return acc if sum(len(v) for v in acc.values()) >= 14 else None


def run_cabinet(cab, token, n_days):
    d2 = YESTERDAY
    d1 = d2 - dt.timedelta(days=n_days - 1)
    log(f"\n══════════ {cab}: {d1:%d.%m} – {d2:%d.%m.%Y} ({n_days} дн.) ══════════")
    out_rows, hours_rows = [], []
    try:
        wd, _ = ad_by_weekday(token, d1, d2)
    except ApiError as e:
        log(f"  ! Реклама: {e}")
        wd = None
    notable = []
    if wd:
        rows, notable = ad_table(wd)
        out_rows += [{"кабинет": cab, **r} for r in rows]

    try:
        start, by_wd, by_hour, grid, days_wd = orders_by_time(token, d1, d2)
    except ApiError as e:
        log(f"  ! Заказы («Статистика»): {e}")
        by_wd = None
    if by_wd:
        total = sum(by_wd.values())
        log(f"\n=== 2. Все заказы кабинета (реклама + органика + FBS) с {start:%d.%m}: {fm(total)} шт ===")
        avg = total / max(1, sum(len(v) for v in days_wd.values()))
        log("  По дням недели (в среднем за день, к среднему дню):")
        log("   " + "  ".join(f"{WD[i]} {fm(by_wd[i] / max(1, len(days_wd[i])), 1)} "
                              f"({(by_wd[i] / max(1, len(days_wd[i])) / avg - 1):+.0%})" for i in range(7)))
        log("  По часам оформления (доля заказов):")
        for h0 in range(0, 24, 6):
            log("   " + "  ".join(f"{h:02d}ч {by_hour[h] / total:5.1%}" for h in range(h0, h0 + 6)))
        night = sum(by_hour[h] for h in range(0, 7)) / total
        log(f"  Ночью (00–07) оформляется {night:.0%} заказов. Почасовой статистики рекламы у WB нет — "
            "это не основание менять ставки по часам.")
        for i in range(7):
            for b in range(8):
                hours_rows.append({"кабинет": cab, "день недели": WD[i], "часы": f"{b * 3:02d}–{b * 3 + 3:02d}",
                                   "заказы": round(grid[(i, b)]), "доля %": round(grid[(i, b)] / total * 100, 2)})

    jam = jam_by_weekday(cab, d1, d2)
    log("\n=== 3. Спрос по дням недели («Джем», частотность ваших запросов) ===")
    if not jam:
        log("  Истории пока мало — появится, когда wb_positions.py поработает 2+ недели.")
    else:
        m = {i: sum(v) / len(v) for i, v in jam.items() if v}
        base = sum(m.values()) / len(m)
        log("   " + "  ".join(f"{WD[i]} {(m[i] / base - 1):+.0%}" for i in range(7) if i in m))

    log("\n=== 4. Вывод ===")
    if not wd:
        log("  Данных рекламы нет.")
    elif not notable:
        log("  Устойчивых «дешёвых» или «дорогих» дней (разница 20%+ и не похожа на случайность) нет — "
            "управлять ставками или лимитами по дням недели не нужно. Проверить снова через месяц.")
    else:
        for name, diff, z in notable:
            act = "дневной лимит +20%" if diff < 0 else "дневной лимит −20%"
            log(f"  {name}: заказ с рекламы {diff:+.0%} к среднему (z = {z:.1f}) → можно {act} в этот день.")
        log("  Это предложение, не автоматика. Скажите Claude — он добавит правило по дням в wb_bids.py "
            "(с пробным прогоном). Ставка при этом остаётся не выше окупаемой.")
    return out_rows, hours_rows


def main():
    n_days = arg_int(DEFAULT_DAYS)
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — реклама и заказы по дням недели и часам (только отчёт)")
    all_rows, all_hours = [], []
    for cab, token in load_tokens(arg_cabinet()).items():
        r, h = run_cabinet(cab, token, n_days)
        all_rows += r
        all_hours += h
    write_csv("wb_weekday.csv", all_rows, header=["кабинет", "день недели"] if not all_rows else None)
    write_csv("wb_weekday_hours.csv", all_hours, header=["кабинет", "день недели", "часы"] if not all_hours else None)
    save_log("wb_weekday_report.txt")
    print("\nСохранено: wb_weekday_report.txt, wb_weekday.csv, wb_weekday_hours.csv")


if __name__ == "__main__":
    main()

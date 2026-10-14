# -*- coding: utf-8 -*-
"""
Спрос по часам с учётом дня недели — основа для распределения рекламного бюджета по часам. Только чтение.

Берёт все заказы кабинета (реклама + органика, FBS и склады WB) из «Статистики» за 90 дней — это максимум, который
хранит WB — и строит:
  1. Таблицу «день недели × час»: сколько заказов в среднем оформляется в этот час этого дня недели.
  2. Отрезки суток по дням недели: ночь 0–7, утро 7–10, день 10–17, вечер 17–24 — доля заказов дня.
  3. Проверку: правда ли дни недели различаются по распределению в течение суток или разница — случайность
     (тест хи-квадрат по отрезкам суток; «устойчиво» — если вероятность случайности меньше 5%).
  4. Кривую расхода на каждый день недели: к какому часу какая доля дневного спроса набирается — по ней
     можно будет распределять бюджет (сверим с часовыми снимками расхода wb_spend_hourly.csv, см. wb_pacing.py).
Доли по одному часу одного дня недели шумные (≈13 таких дней за 90), поэтому в кривой они сглажены к общему
профилю часов: чем меньше заказов, тем ближе к общему.

Это СПРОС (сколько покупателей оформляют заказ), а не конверсия рекламы: WB не даёт рекламу по часам.

Запуск:  python wb_dayparts.py                        (все кабинеты, 90 дней)
         python wb_dayparts.py --cabinet "Яворская"
         python wb_dayparts.py --models 27643,36446     (только эти модели — текст в артикуле продавца)
Нужна категория токена «Статистика». WB отдаёт заказы раз в минуту — 1–3 минуты на кабинет.
Результат: вывод на экран + wb_dayparts_report.txt, wb_dayparts_profile.csv (день недели, час, доля, накоплено)
"""
import datetime as dt
import math
import sys
from collections import defaultdict

from wb_common import YESTERDAY, ApiError, arg_cabinet, load_tokens, log, save_log, stat_rows, write_csv

# ---------------- НАСТРОЙКИ ----------------
DAYS = 90
SMOOTH_K = 150          # вес общего профиля часов при сглаживании (в заказах): мало заказов в дне недели — ближе к общему
BANDS = [("ночь", 0, 7), ("утро", 7, 10), ("день", 10, 17), ("вечер", 17, 24)]
# -------------------------------------------
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def models():
    if "--models" in sys.argv:
        i = sys.argv.index("--models")
        if i + 1 < len(sys.argv):
            return [m.strip().lower() for m in sys.argv[i + 1].split(",") if m.strip()]
    return []


def chi2_sf(x, k):
    """Вероятность получить хи-квадрат ≥ x случайно (k степеней свободы), приближение Уилсона–Хилферти."""
    if k <= 0:
        return 1.0
    z = ((x / k) ** (1 / 3) - (1 - 2 / (9 * k))) / math.sqrt(2 / (9 * k))
    return 0.5 * math.erfc(z / math.sqrt(2))


def run_cabinet(cab, token, mods):
    log(f"\n══════════ {cab} ══════════")
    d1 = YESTERDAY - dt.timedelta(days=DAYS - 1)
    print("  заказы из «Статистики» (WB отдаёт 1 раз в минуту)...")
    rows = stat_rows(token, "orders", d1)
    grid = defaultdict(float)            # (день недели, час) → заказов
    days_wd = defaultdict(set)
    seen = set()
    for r in rows:
        ts = str(r.get("date") or "")
        if len(ts) < 13 or not (d1.isoformat() <= ts[:10] <= YESTERDAY.isoformat()):
            continue
        if mods and not any(m in str(r.get("supplierArticle") or "").lower() for m in mods):
            continue
        key = r.get("srid") or (ts, r.get("nmId"), r.get("barcode"))
        if key in seen:
            continue
        seen.add(key)
        w = dt.date.fromisoformat(ts[:10]).weekday()
        grid[(w, int(ts[11:13]))] += 1
        days_wd[w].add(ts[:10])
    n = sum(grid.values())
    log(f"  Период {d1:%d.%m}–{YESTERDAY:%d.%m.%Y}" + (f", модели: {', '.join(mods)}" if mods else ", все товары")
        + f". Заказов: {int(n)} (вместе с отменёнными — это спрос).")
    if n < 200:
        log("  Заказов мало — распределение по часам и дням недели будет случайным, выводы делать рано.")
        if not n:
            return []
    hour_tot = [sum(grid[(w, h)] for w in range(7)) for h in range(24)]
    wd_tot = [sum(grid[(w, h)] for h in range(24)) for w in range(7)]
    base = [x / n for x in hour_tot]

    # 1. таблица: заказов в среднем за один такой день недели, по 3 часа
    log("\n  1. Заказов в среднем за день, по 3 часа (по времени оформления, Москва):")
    log("    " + f"{'':4}" + "".join(f"{f'{b * 3:02d}–{b * 3 + 3:02d}':>8}" for b in range(8)) + f"{'за день':>9}")
    for w in range(7):
        nd = max(len(days_wd[w]), 1)
        cells = [sum(grid[(w, h)] for h in range(b * 3, b * 3 + 3)) / nd for b in range(8)]
        log(f"    {WD[w]:4}" + "".join(f"{c:>8.1f}" for c in cells) + f"{wd_tot[w] / nd:>9.1f}")

    # 2. отрезки суток
    log("\n  2. Доля заказов дня по отрезкам суток:")
    log("    " + f"{'':4}" + "".join(f"{f'{b[0]} {b[1]:02d}–{b[2]:02d}':>14}" for b in BANDS))
    band_obs = [[sum(grid[(w, h)] for h in range(a, z)) for _, a, z in BANDS] for w in range(7)]
    for w in range(7):
        t = wd_tot[w] or 1
        log(f"    {WD[w]:4}" + "".join(f"{x / t:>14.0%}" for x in band_obs[w]))
    allb = [sum(band_obs[w][i] for w in range(7)) for i in range(len(BANDS))]
    log(f"    {'все':4}" + "".join(f"{x / n:>14.0%}" for x in allb))

    # 3. различаются ли дни недели по распределению в течение суток (хи-квадрат: день недели × отрезок)
    chi = 0.0
    dev = []
    for w in range(7):
        for i in range(len(BANDS)):
            exp = wd_tot[w] * allb[i] / n
            if exp > 0:
                chi += (band_obs[w][i] - exp) ** 2 / exp
                dev.append(((band_obs[w][i] - exp) / math.sqrt(exp), w, i))
    k = (7 - 1) * (len(BANDS) - 1)
    p = chi2_sf(chi, k)
    log(f"\n  3. Различаются ли дни недели по часам: хи-квадрат {chi:.1f} при {k} степенях свободы, вероятность "
        f"случайности ≈{p:.1%}.")
    if p < 0.05:
        log("    Различие УСТОЙЧИВОЕ — распределять бюджет по часам нужно с учётом дня недели. Самые заметные отличия:")
        for z, w, i in sorted(dev, key=lambda x: -abs(x[0]))[:5]:
            share = band_obs[w][i] / (wd_tot[w] or 1)
            log(f"      • {WD[w]}, {BANDS[i][0]}: {share:.0%} заказов дня против {allb[i] / n:.0%} в среднем "
                f"({'больше' if z > 0 else 'меньше'} обычного)")
    else:
        log("    Устойчивого различия нет — профиль часов один на все дни недели, отличаются только объёмы по дням.")

    # 4. кривая расхода по дням недели (сглаженная)
    log("\n  4. К какому часу набирается доля дневного спроса (сглажено):")
    log("    " + f"{'':4}" + "".join(f"{f'к {h:02d}:00':>9}" for h in (10, 12, 15, 18, 21)))
    out = []
    for w in range(7):
        t = wd_tot[w]
        sm = [(grid[(w, h)] + SMOOTH_K * base[h]) / (t + SMOOTH_K) for h in range(24)]
        cum, acc = [], 0.0
        for h in range(24):
            acc += sm[h]
            cum.append(acc)
            out.append({"кабинет": cab, "день недели": WD[w], "час": f"{h:02d}", "заказов": int(grid[(w, h)]),
                        "доля часа %": round(sm[h] * 100, 2), "накоплено к концу часа %": round(acc * 100, 1)})
        log(f"    {WD[w]:4}" + "".join(f"{cum[h - 1]:>9.0%}" for h in (10, 12, 15, 18, 21)))
    log("    Пример: если к 15:00 обычно набирается 45% спроса, то при равномерном по спросу расходе к 15:00 должно уйти "
        "не больше 45% дневного бюджета.")
    return out


def main():
    mods = models()
    cabs = load_tokens(arg_cabinet())
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — спрос по часам и дням недели за {DAYS} дн. (только отчёт)")
    rows = []
    for cab, token in cabs.items():
        try:
            rows += run_cabinet(cab, token, mods)
        except ApiError as e:
            log(f"  ✗ {cab}: {e}")
    log("\nЭто спрос, а не конверсия рекламы: WB не даёт рекламу по часам. Сверим с часовыми снимками расхода "
        "(wb_pacing.py), когда их наберётся 3–4 недели.")
    write_csv("wb_dayparts_profile.csv", rows, header=list(rows[0]) if rows else None)
    save_log("wb_dayparts_report.txt")
    print("\nСохранено: wb_dayparts_report.txt, wb_dayparts_profile.csv")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
Заказы Wildberries по часам для выбранных моделей (по умолчанию 27643 и 36446) — когда покупают
и что потеряем, если реклама выключится раньше конца дня из-за дневного лимита. Только чтение.

Что показывает (по каждой модели — артикул продавца содержит заданный текст):
  1. Доля заказов по часам оформления (время Москвы) и в среднем заказов в этот час за день.
  2. «Если реклама выключится в ЧЧ:00»: какая доля заказов дня оформляется после этого часа.
     Это верхняя оценка: после выключения рекламы часть покупателей всё равно придёт — из обычного поиска.
  3. По дням недели — в среднем заказов за день.
Почасовой статистики рекламы WB не отдаёт — здесь все заказы модели (реклама + органика), это ориентир.

Запуск:  python wb_hours.py                       (27643 и 36446, последние 30 дней)
         python wb_hours.py 60                    (за 60 дней, не больше 90)
         python wb_hours.py --models 27643,36446,2101
         python wb_hours.py --cabinet "Яворская"
Нужна категория токена «Статистика». WB отдаёт заказы 1 раз в минуту — 1–2 минуты.
Можно запускать одновременно с wb_bids.py: это другой раздел API.
Результат: вывод на экран + wb_hours_report.txt, wb_hours.csv
"""
import datetime as dt
import sys
from collections import defaultdict

from wb_common import YESTERDAY, ApiError, arg_cabinet, load_tokens, log, save_log, stat_rows, write_csv

# ---------------- НАСТРОЙКИ ----------------
MODELS = ["27643", "36446"]   # модели: текст, который есть в артикуле продавца
DEFAULT_DAYS = 30             # за сколько дней; «Статистика» WB отдаёт заказы не старше 90 дней
# -------------------------------------------

WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def days():
    """Число дней — отдельное число в команде (значение после --models не считается: там тоже бывают цифры)."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a.isdigit() and not (i > 0 and args[i - 1] in ("--models", "--cabinet")):
            return min(int(a), 90)
    return DEFAULT_DAYS


DAYS = days()


def models():
    if "--models" in sys.argv:
        i = sys.argv.index("--models")
        if i + 1 < len(sys.argv):
            return [m.strip() for m in sys.argv[i + 1].split(",") if m.strip()]
    return MODELS


def run_cabinet(cab, token, mods):
    log(f"\n══════════ {cab} ══════════")
    d1 = YESTERDAY - dt.timedelta(days=DAYS - 1)
    print("  заказы из «Статистики» (WB отдаёт 1 раз в минуту)...")
    rows = stat_rows(token, "orders", d1)
    by = {m: {"hour": defaultdict(float), "wd": defaultdict(float), "days_wd": defaultdict(set), "n": 0, "cancel": 0}
          for m in mods}
    seen = set()
    for r in rows:
        ts = str(r.get("date") or "")
        if len(ts) < 13 or not (d1.isoformat() <= ts[:10] <= YESTERDAY.isoformat()):
            continue
        key = r.get("srid") or (ts, r.get("nmId"), r.get("barcode"))
        if key in seen:
            continue
        seen.add(key)
        art = str(r.get("supplierArticle") or "").lower()
        for m in mods:
            if m.lower() in art:
                g = by[m]
                g["n"] += 1
                g["cancel"] += 1 if r.get("isCancel") else 0
                g["hour"][int(ts[11:13])] += 1
                w = dt.date.fromisoformat(ts[:10]).weekday()
                g["wd"][w] += 1
                g["days_wd"][w].add(ts[:10])
                break
    out = []
    for m in mods:
        g = by[m]
        n = g["n"]
        log(f"\n  ▶ Модель {m}: {n} заказов за {DAYS} дн. ({n / DAYS:.1f} в день), отменено {g['cancel']}")
        if n < 30:
            log("    Заказов мало — картина по часам случайная, вывод делать рано.")
            if not n:
                continue
        log(f"    {'час':>5}{'доля':>7}{'в день':>8}  {'после этого часа':>17}")
        after = 1.0
        for h in range(24):
            share = g["hour"][h] / n
            log(f"    {h:02d}:00{share:>7.1%}{g['hour'][h] / DAYS:>8.2f}  {after:>16.0%}  "
                + "█" * round(share * 100))
            out.append({"кабинет": cab, "модель": m, "час": f"{h:02d}", "заказов": int(g["hour"][h]),
                        "доля": round(share * 100, 1), "в день": round(g["hour"][h] / DAYS, 2),
                        "доля после начала часа": round(after * 100, 1)})
            after -= share
        cum, marks = 0.0, {}
        for h in range(24):
            cum += g["hour"][h] / n
            for p in (0.5, 0.75, 0.9):
                if cum >= p and p not in marks:
                    marks[p] = h + 1
        log(f"    Половина заказов дня оформлена к {marks.get(0.5, 24):02d}:00, три четверти — к {marks.get(0.75, 24):02d}:00, "
            f"90% — к {marks.get(0.9, 24):02d}:00.")
        evening = sum(g["hour"][h] for h in range(17, 22)) / n
        log(f"    Вечер 17–22 ч: {evening:.0%} заказов.")
        log("    По дням недели (в среднем заказов за день): " + "  ".join(
            f"{WD[w]} {g['wd'][w] / max(len(g['days_wd'][w]), 1):.1f}" for w in range(7)))
    return out


def main():
    mods = models()
    cabs = load_tokens(arg_cabinet())
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — заказы по часам для моделей {', '.join(mods)} за {DAYS} дн. "
        "(время Москвы; все заказы — реклама и органика; только отчёт)")
    log("«после этого часа» — какая доля заказов дня оформляется с этого часа до полуночи: столько примерно")
    log("под угрозой, если реклама выключится в этот час из-за дневного лимита (часть придёт и без рекламы).")
    rows = []
    for cab, token in cabs.items():
        try:
            rows += run_cabinet(cab, token, mods)
        except ApiError as e:
            log(f"  ✗ {cab}: {e}")
    write_csv("wb_hours.csv", rows, header=list(rows[0]) if rows else None)
    save_log("wb_hours_report.txt")
    print("\nСохранено: wb_hours_report.txt, wb_hours.csv")


if __name__ == "__main__":
    main()

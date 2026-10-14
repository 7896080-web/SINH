# -*- coding: utf-8 -*-
"""
Равномерность расхода рекламы WB по часам — во сколько у кампаний кончаются деньги и сколько спроса они теряют.
Только чтение: читает wb_spend_hourly.csv (его каждый час пополняет wb_guard.py), к WB не обращается.

Как считается:
  • за клики — по «потрачено сегодня» (WB отдаёт напрямую) и дневному лимиту: «кончилось» = потрачено ≥ 97% лимита;
  • за показы — по убыванию бюджета между часовыми снимками (рост бюджета = пополнение, этот час не считается):
    «кончилось» = бюджет меньше 50 ₽;
  • упущенный спрос — доля заказов, которые по статистике кабинета оформляются ПОСЛЕ часа, когда кончились деньги
    (HOUR_SHARE: заказы Яворской по часам за 13.07–11.10.2026, 3 891 заказ; WB не даёт рекламу по часам).

Запуск:  python wb_pacing.py            (последние 7 дней)
         python wb_pacing.py 14
Результат: вывод на экран + wb_pacing_report.txt
"""
import csv
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "wb_spend_hourly.csv"
OUT = HERE / "wb_pacing_report.txt"
DAYS = next((int(a) for a in sys.argv[1:] if a.isdigit()), 7)
# доля заказов кабинета по часам оформления, % (wb_weekday.py, 91 день, 3 891 заказ)
HOUR_SHARE = [2.3, 0.6, 0.7, 0.4, 1.0, 1.1, 2.2, 3.6, 5.0, 5.7, 4.7, 5.6,
              5.8, 5.9, 5.1, 5.5, 5.3, 6.1, 5.3, 6.3, 6.5, 6.8, 4.7, 3.8]
EMPTY_CPM = 50          # бюджет кампании за показы меньше этого, ₽ — «деньги кончились»
FULL_CPC = 0.97         # потрачено ≥ 97% дневного лимита — «лимит кончился»
lines = []


def log(s=""):
    print(s)
    lines.append(str(s))


def f(v):
    try:
        return float(str(v).replace(",", ".").replace(" ", "")) if str(v).strip() != "" else None
    except ValueError:
        return None


def after_share(t):
    """Доля суточного спроса после момента t (часы с дробью)."""
    h = int(t)
    tot = sum(HOUR_SHARE)
    return (HOUR_SHARE[h] * (1 - (t - h)) + sum(HOUR_SHARE[h + 1:])) / tot if h < 24 else 0.0


def main():
    if not SRC.exists():
        log(f"Нет {SRC.name}: его пишет часовая проверка (wb_guard.py) начиная с 14.10.2026. Запустите позже.")
        return
    raw = SRC.read_bytes().decode("utf-8-sig", errors="replace").splitlines()
    rows = list(csv.DictReader(raw, delimiter=";"))
    since = (dt.date.today() - dt.timedelta(days=DAYS - 1)).isoformat()
    by = defaultdict(list)
    names = {}
    for r in rows:
        if (r.get("время") or "")[:10] < since:
            continue
        key = (r["кабинет"], r["кампания"])
        names[key] = (r.get("название") or "", r.get("оплата") or "")
        by[key].append(r)
    log(f"{dt.datetime.now():%d.%m.%Y %H:%M} — равномерность расхода рекламы за {DAYS} дн. (по часовым снимкам)")
    if not by:
        log("Снимков за период нет.")
        return
    problems = []
    for (cab, cid), rs in sorted(by.items()):
        name, pay = names[(cab, cid)]
        rs.sort(key=lambda r: r["время"])
        days = defaultdict(list)
        for r in rs:
            days[r["время"][:10]].append(r)
        stat = []      # (день, расход, во сколько кончилось или None, доля спроса после)
        prev_b = None
        for d, ds in sorted(days.items()):
            spent, out_at = 0.0, None
            if pay == "cpc":
                for r in ds:
                    s_, lim = f(r.get("потрачено сегодня ₽")), f(r.get("дневной лимит ₽"))
                    if s_ is not None:
                        spent = max(spent, s_)
                    if out_at is None and s_ is not None and lim and s_ >= lim * FULL_CPC:
                        hh, mm = map(int, r["время"][11:16].split(":"))
                        out_at = hh + mm / 60
            else:
                for r in ds:
                    b = f(r.get("бюджет ₽"))
                    if b is None:
                        continue
                    if prev_b is not None and b < prev_b:
                        spent += prev_b - b
                    if out_at is None and b < EMPTY_CPM and r.get("статус") in ("9", "11"):
                        hh, mm = map(int, r["время"][11:16].split(":"))
                        out_at = hh + mm / 60
                    prev_b = b
            stat.append((d, spent, out_at, after_share(out_at) if out_at is not None else 0.0))
        tag = "за клики" if pay == "cpc" else "за показы"
        log(f"\n  {cab}: «{name}» ({cid}, {tag})")
        for d, spent, out_at, lost in stat:
            log(f"    {d[8:10]}.{d[5:7]}  расход {spent:>7,.0f} ₽".replace(",", " ")
                + (f"   деньги кончились ≈{int(out_at):02d}:{int(out_at % 1 * 60):02d} → упущено ≈{lost:.0%} спроса"
                   if out_at is not None else "   хватило на весь день"))
        short = [x for x in stat if x[2] is not None and x[2] < 20]
        if len(stat) >= 2 and len(short) * 2 >= len(stat):
            problems.append((cab, name, cid, tag, len(short), len(stat),
                             sum(x[3] for x in short) / len(short)))
    log("\n  Итог:")
    if not problems:
        log("    Кампаний, у которых деньги регулярно кончаются до 20:00, нет — равномерный расход не нужен.")
    else:
        log("    Деньги регулярно кончаются до 20:00 (кандидаты на равномерный расход):")
        for cab, name, cid, tag, n, tot, lost in sorted(problems, key=lambda x: -x[6]):
            log(f"    • {cab}: «{name}» ({cid}, {tag}) — {n} из {tot} дн., в среднем упущено ≈{lost:.0%} спроса")
    log("\n  Снимки — раз в час с 7:40 до 23:40: время «кончилось» точное до часа. Расход за показы по бюджету — оценка.")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nСохранено: {OUT.name}")


if __name__ == "__main__":
    main()

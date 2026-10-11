# -*- coding: utf-8 -*-
"""
«Контроль органики» Wildberries — где мы, возможно, платим рекламой за заказы, которые пришли бы и так.
Только отчёт: ничего не меняет в кабинете и не обращается к WB — читает историю wb_history.db,
которую каждый день копит wb_positions.py (позиции и все заказы по запросу из «Джема»; реклама по запросам).

Идея (мировая практика, см. docs/ads-research-2026-10.md, раздел 2.9): если товар по запросу и так стоит
в топе выдачи, часть рекламных заказов — это покупатели, которые нашли бы товар бесплатно. Проверить можно
только экспериментом: на 7 дней убрать рекламу по запросу (кампания за клики — минус-фраза; за показы —
ставка по кластеру −30%) и сравнить ВСЕ заказы по запросу («Джем») с прошлыми 7 днями.
Если общие заказы упали меньше чем на 10–15% — реклама по этому запросу почти ничего не добавляла.

Что показывает:
  1. Кандидаты на проверку: в топе выдачи большую часть дней, есть заказы, есть расход рекламы.
  2. Брендовые запросы (awer, авер, mcl): расход и заказы — по исследованию eBay реклама по бренду
     почти не добавляет продаж; проверить так же.
  3. Как провести эксперимент и как читать результат.

Оговорки: позиция из «Джема» не отделяет рекламную позицию от органической. Если рекламная позиция почти
совпадает с общей — товар, возможно, держится в топе именно рекламой; это и покажет эксперимент.
Выдача WB персональная — вывод по одной неделе предварительный.

Запуск:  python wb_organic.py                     (все кабинеты, последние 7 дней)
         python wb_organic.py --days 14
         python wb_organic.py --cabinet "Яворская"
Нужно: wb_history.db (сначала несколько дней запусков wb_positions.py). Токен не нужен.
Результат: wb_organic_report.txt, wb_organic.csv
"""
import argparse
import datetime as dt
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from wb_common import write_csv

HERE = Path(__file__).resolve().parent
DB = HERE / "wb_history.db"

# ---------------- НАСТРОЙКИ ----------------
TOP_POS = 10            # «в топе» — медианная позиция по запросу не дальше этого места
TOP_SHARE = 0.7         # …в стольких днях периода
MIN_DAYS = 4            # меньше дней с данными по запросу — не судим
MIN_ORDERS = 3          # минимум всех заказов по запросу за период («Джем»)
MIN_SPEND = 100         # минимум расхода рекламы по запросу за период, ₽
NEAR_AD = 2             # рекламная позиция ближе стольких мест к общей — «возможно, держится рекламой»
BRAND = ["awer", "авер", "mcl"]   # как PROTECT в wb_bids.py
# -------------------------------------------

out = []


def log(s=""):
    print(s)
    out.append(str(s))


def money(v):
    return f"{v:,.0f}".replace(",", " ")


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--cabinet")
    a = ap.parse_args()
    if not DB.exists():
        sys.exit("Нет wb_history.db — сначала несколько дней запускайте python wb_positions.py.")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    last = con.execute("SELECT MAX(date) FROM query_day").fetchone()[0]
    if not last:
        sys.exit("В wb_history.db ещё нет данных по запросам — запустите python wb_positions.py.")
    d2 = dt.date.fromisoformat(last)
    d1 = d2 - dt.timedelta(days=a.days - 1)
    cab_sql, cab_arg = (" AND cab=?", (a.cabinet,)) if a.cabinet else ("", ())

    jam = defaultdict(lambda: {"orders": 0.0, "pos": [], "days": 0, "freq": 0.0})
    for cab, nm, q, d, orders, mpos, freq in con.execute(
            "SELECT cab, nm, query, date, orders, median_pos, freq FROM query_day WHERE date BETWEEN ? AND ?" + cab_sql,
            (d1.isoformat(), d2.isoformat()) + cab_arg):
        j = jam[(cab, nm, q)]
        j["orders"] += orders or 0
        j["freq"] += freq or 0
        if mpos is not None:
            j["pos"].append(mpos)
            j["days"] += 1
    ad = defaultdict(lambda: {"clicks": 0.0, "orders": 0.0, "spend": 0.0, "pos": []})
    for cab, nm, q, clicks, orders, spend, apos in con.execute(
            "SELECT cab, nm, query, clicks, orders, spend, ad_pos FROM ad_query_day WHERE date BETWEEN ? AND ?" + cab_sql,
            (d1.isoformat(), d2.isoformat()) + cab_arg):
        g = ad[(cab, nm, q)]
        g["clicks"] += clicks or 0
        g["orders"] += orders or 0
        g["spend"] += spend or 0
        if apos is not None:
            g["pos"].append(apos)
    vendor = {(c, n): v for c, n, v in con.execute("SELECT cab, nm, vendor FROM products")}

    log(f"Контроль органики WB: {d1:%d.%m} – {d2:%d.%m.%Y} ({a.days} дн.), только отчёт — в кабинете ничего не меняется.")
    log(f"«В топе» — медианная позиция по запросу не дальше {TOP_POS} места в {TOP_SHARE:.0%} дней и больше.")
    matched = sum(1 for k in ad if k in jam)
    log(f"Рекламных запросов с расходом: {sum(1 for g in ad.values() if g['spend'] > 0)}, "
        f"из них нашлись в «Джеме» (по точному тексту): {matched}.")

    rows, cand, brand = [], [], []
    for k, g in ad.items():
        if g["spend"] <= 0:
            continue
        cab, nm, q = k
        j = jam.get(k)
        jpos = med(j["pos"]) if j else None
        apos = med(g["pos"])
        top_days = sum(1 for p in (j["pos"] if j else []) if p <= TOP_POS)
        is_top = bool(j) and j["days"] >= MIN_DAYS and top_days >= TOP_SHARE * j["days"]
        near = jpos is not None and apos is not None and abs(jpos - apos) <= NEAR_AD
        r = {"кабинет": cab, "артикул WB": nm, "артикул продавца": vendor.get((cab, nm), ""), "запрос": q,
             "позиция медиана": round(jpos, 1) if jpos is not None else "", "дней в топе": top_days,
             "дней с данными": j["days"] if j else 0, "все заказы (Джем)": round(j["orders"]) if j else "",
             "рекламные заказы": round(g["orders"]), "рекламные клики": round(g["clicks"]),
             "расход рекламы ₽": round(g["spend"]), "рекламная позиция": round(apos, 1) if apos is not None else "",
             "держится рекламой?": "возможно" if near else "", "бренд": "да" if any(b in q for b in BRAND) else ""}
        rows.append(r)
        if r["бренд"]:
            brand.append(r)
        elif is_top and j["orders"] >= MIN_ORDERS and g["spend"] >= MIN_SPEND:
            cand.append(r)

    month = 30 / a.days
    log(f"\n=== 1. Кандидаты на проверку: в топе, есть заказы, есть расход рекламы ({len(cand)}) ===")
    if not cand:
        log("  Нет. Либо товары не держатся в топе, либо истории пока мало (нужно ≥ "
            f"{MIN_DAYS} дн. запусков wb_positions.py).")
    for r in sorted(cand, key=lambda r: -r["расход рекламы ₽"]):
        share = r["рекламные заказы"] / r["все заказы (Джем)"] if r["все заказы (Джем)"] else 0
        log(f"  {r['артикул WB']:>11} {str(r['артикул продавца'])[:16]:16} «{r['запрос']}»: позиция {r['позиция медиана']} "
            f"({r['дней в топе']} из {r['дней с данными']} дн. в топе), заказов всего {r['все заказы (Джем)']}, "
            f"из них с рекламы {r['рекламные заказы']} ({share:.0%}), расход {money(r['расход рекламы ₽'])} ₽"
            f" (≈{money(r['расход рекламы ₽'] * month)} ₽/мес)"
            + (" — рекламная позиция почти та же, возможно держится рекламой" if r["держится рекламой?"] else ""))
    if cand:
        log(f"  Если органика удержит заказы, экономия до ≈{money(sum(r['расход рекламы ₽'] for r in cand) * month)} ₽ в месяц.")

    log(f"\n=== 2. Брендовые запросы ({', '.join(BRAND)}) ===")
    if not brand:
        log("  Расхода рекламы по брендовым запросам нет.")
    for r in sorted(brand, key=lambda r: -r["расход рекламы ₽"]):
        log(f"  {r['артикул WB']:>11} «{r['запрос']}»: расход {money(r['расход рекламы ₽'])} ₽, рекламных заказов "
            f"{r['рекламные заказы']}, всех заказов {r['все заказы (Джем)'] or '—'}, позиция {r['позиция медиана'] or '—'}")
    if brand:
        log(f"  Всего по бренду: {money(sum(r['расход рекламы ₽'] for r in brand))} ₽ за {a.days} дн. "
            "Покупатель, который ищет AWER по названию, скорее всего найдёт вас и без рекламы — стоит проверить.")

    log("\n=== 3. Как проверить (эксперимент на 7 дней) ===")
    log("  1. Выберите 1–3 запроса из списков выше (лучше с наибольшим расходом).")
    log("  2. Кампания за клики: добавьте запрос в минус-фразы этого товара. Кампания за показы: ставка по кластеру −30%.")
    log("     Важно: в wb_bids.py запрос с брендом защищён от минус-фраз (PROTECT) — для теста бренда это делается вручную.")
    log("  3. Через 7 дней: python wb_organic.py --days 7 и сравните «все заказы (Джем)» по запросу с прошлой неделей.")
    log("     Упали меньше чем на 10–15% — реклама по запросу почти не добавляла, оставляем без неё.")
    log("     Упали сильнее — возвращаем рекламу. Одна неделя — предварительно; при сомнении повторить.")
    log("  Скажите Claude, какие запросы проверяем, — он подготовит пробный прогон и запомнит дату начала.")

    write_csv(HERE / "wb_organic.csv", rows, header=list(rows[0]) if rows else None)
    (HERE / "wb_organic_report.txt").write_text("\n".join(out), encoding="utf-8")
    print("\nСохранено: wb_organic_report.txt, wb_organic.csv")


if __name__ == "__main__":
    main()

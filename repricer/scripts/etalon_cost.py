"""Себестоимость по артикулу (верхняя планка) и маржинальность от текущей цены WB — ТОЛЬКО ЧТЕНИЕ.

Логика (задана владельцем 13.10):
  * себестоимость артикула = НАИБОЛЬШАЯ себестоимость 1С среди всех его SKU — всех
    размеров и ВСЕХ ЦВЕТОВ: одна на весь артикул, не взирая на цвет;
  * артикул — поле «Артикул» справочника 1С; окончание «-ТРИ ЦИФРЫ» — код цвета и
    отбрасывается: 256250-015, 256250-020 … — один артикул 256250;
  * маржинальность — от ТЕКУЩЕЙ цены WB, той, что установлена сейчас (цена продавца со
    скидкой, загруженная программой): к получению (цена × (1 − комиссия)) / себестоимость, ₽.

Себестоимость берётся из программы — её туда кладёт ответ 1С. Свежая: «Диагностика» →
«Запросить себестоимость», дождаться ответа (дата загрузки печатается в начале).

    C:\\repricer\\.venv\\Scripts\\python.exe C:\\repricer\\scripts\\etalon_cost.py --xlsx C:\\repricer\\себестоимость_маржа.xlsx
"""
from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(errors="replace")      # консоль Windows — cp1251
except Exception:
    pass

import priceapp  # noqa: E402,F401  (.env читается на импорте)
from priceapp import mapping, rates, settings  # noqa: E402
from priceapp.database import SessionLocal  # noqa: E402
from priceapp.models import Account, OnecBarcode, OnecCost  # noqa: E402
from priceapp.pricing import article_map, article_of, get_rule, item_facts  # noqa: E402

NO_CATEGORY = "(без категории)"
COLOR_SUFFIX = re.compile(r"^(.+?)-\d{3}$")


def model_of(article: str) -> str:
    """Артикул без кода цвета: «256250-015» -> «256250»."""
    m = COLOR_SUFFIX.match((article or "").strip())
    return m.group(1) if m else (article or "").strip()


def article_costs(costs: dict[str, Decimal], articles: dict[str, str]) -> dict[str, dict]:
    """Артикул -> {max, min, skus}: верхняя планка себестоимости, $."""
    out: dict[str, dict] = {}
    for item_id, c in costs.items():
        if c is None or c <= 0:
            continue
        m = model_of(article_of(articles, item_id))
        d = out.setdefault(m, {"max": c, "min": c, "skus": 0})
        d["max"], d["min"], d["skus"] = max(d["max"], c), min(d["min"], c), d["skus"] + 1
    return out


def wb_rows(db, articles, costs, per_art, info, usd: Decimal) -> list[dict]:
    rule = get_rule(db, "wb")
    out = []
    for a in db.query(Account).filter(Account.is_active.is_(True), Account.platform == "wb").order_by(Account.name):
        for item_id, rows in mapping.account_items(db, a.id).items():
            art1c = article_of(articles, item_id)
            model = model_of(art1c)
            facts = item_facts(rule, rows)
            top = per_art.get(model)
            cost_max = top["max"] * usd if top else None
            own = costs.get(item_id)
            own_rub = own * usd if own else None
            got = Decimal(facts.sale) * (1 - facts.commission / 100) if facts.sale and facts.commission is not None else None
            sku = info.get(item_id)
            out.append({"account": a.name, "model": model, "article": art1c, "item_id": item_id,
                        "size": sku.size if sku else "", "color": sku.color if sku else "",
                        "category": facts.category or NO_CATEGORY,
                        "list": facts.current, "sale": facts.sale,
                        "disc": facts.discount if facts.current else None, "commission": facts.commission,
                        "payout": got, "cost_usd": own, "cost_max_usd": top["max"] if top else None,
                        "cost_max_rub": cost_max,
                        "margin": got / cost_max if got is not None and cost_max else None,
                        "margin_own": got / own_rub if got is not None and own_rub else None})
    return out


def _f(v, nd=2):
    return None if v is None else round(float(v), nd)


def _med(vals):
    vals = [float(v) for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Себестоимость по артикулу и маржинальность от текущей цены WB")
    ap.add_argument("--xlsx", default="")
    ap.add_argument("--article", default="", help="показать один артикул подробно (например 256250)")
    args = ap.parse_args(argv)
    db = SessionLocal()
    try:
        rate = rates.current(db)
        if rate is None:
            print("Курса доллара нет — себестоимость в рублях не посчитать. Страница «Курс $».")
            return 1
        usd = Decimal(rate.usd_rub)
        articles = article_map(db)
        costs = {c.item_id: Decimal(str(c.cost_usd)) for c in db.query(OnecCost)}
        info = {}
        for b in db.query(OnecBarcode):
            info.setdefault(b.item_id, b)
        per_art = article_costs(costs, articles)
        print(f"Себестоимость 1С загружена {settings.get(db, settings.COST_LOADED_AT) or '-'} "
              f"({settings.get(db, settings.COST_ROWS) or '-'} строк); курс {usd}. НИЧЕГО НЕ ОТПРАВЛЯЕТСЯ.")
        print(f"Артикулов с себестоимостью: {len(per_art)}; "
              f"с разной себестоимостью внутри артикула: {sum(1 for d in per_art.values() if d['max'] != d['min'])}")
        rows = wb_rows(db, articles, costs, per_art, info, usd)
        accs = sorted({r["account"] for r in rows})
        for a in db.query(Account).filter(Account.platform == "wb", Account.is_active.is_(True)):
            print(f"  {a.name}: цены загружены с WB {a.prices_loaded_at}")
        nocost = sum(1 for r in rows if r["cost_max_rub"] is None)
        nosale = sum(1 for r in rows if not r["sale"])
        print(f"Товаров WB: {len(rows)}; без себестоимости: {nocost}; без текущей цены: {nosale}")

        cats: dict[str, list[dict]] = {}
        for r in rows:
            cats.setdefault(r["category"], []).append(r)
        print(f"\n  {'категория':30} {'арт.':>4} {'себест.$':>8}  " + "  ".join(f"{a[:12]:>12}" for a in accs)
              + "   (медиана маржинальности по кабинету)")
        for cat, rs in sorted(cats.items(), key=lambda kv: -len(kv[1])):
            per = []
            for a in accs:
                m = _med(r["margin"] for r in rs if r["account"] == a)
                per.append(f"{m:12.2f}" if m is not None else f"{'-':>12}")
            c = _med(r["cost_max_usd"] for r in rs)
            print(f"  {cat[:30]:30} {len({r['model'] for r in rs}):>4} {c if c is not None else '-':>8}  " + "  ".join(per))

        if args.article:
            want = model_of(args.article)
            d = per_art.get(want)
            print(f"\nАртикул {want}: себестоимость макс {d['max'] if d else '-'} $, мин {d['min'] if d else '-'} $, "
                  f"SKU {d['skus'] if d else 0}; по курсу {_f(d['max'] * usd) if d else '-'} руб.")
            seen = set()
            for r in sorted((r for r in rows if r["model"] == want), key=lambda r: (r["account"], r["article"])):
                k = (r["account"], r["article"], r["sale"], r["cost_usd"])
                if k in seen:
                    continue
                seen.add(k)
                print(f"  {r['account']}: {r['article']} {r['color']}: цена {r['list']} / со скидкой {r['sale']}, "
                      f"комиссия {_f(r['commission'])}%, к получению {_f(r['payout'], 0)}, себест. своя "
                      f"{r['cost_usd']} $ -> маржинальность {_f(r['margin'])} (от своей {_f(r['margin_own'])})")

        if args.xlsx:
            from openpyxl import Workbook
            book = Workbook()
            ws = book.active
            ws.title = "Артикулы"
            ws.append(["Артикул", "Категория", "Себестоимость макс, $", "Себестоимость мин, $", "Разброс, %",
                       "Себестоимость макс, руб", "SKU с себестоимостью"]
                      + [f"{a}: цена со скидкой (медиана)" for a in accs]
                      + [f"{a}: маржинальность (медиана)" for a in accs]
                      + ["Маржинальность мин", "Маржинальность макс"])
            by_model: dict[str, list[dict]] = {}
            for r in rows:
                by_model.setdefault(r["model"], []).append(r)
            for model in sorted(by_model):
                rs = by_model[model]
                d = per_art.get(model)
                ms = [r["margin"] for r in rs if r["margin"] is not None]
                ws.append([model, rs[0]["category"], _f(d["max"]) if d else None, _f(d["min"]) if d else None,
                           _f((d["max"] / d["min"] - 1) * 100, 1) if d else None,
                           _f(d["max"] * usd) if d else None, d["skus"] if d else 0]
                          + [_med(r["sale"] for r in rs if r["account"] == a) for a in accs]
                          + [_f(_med(r["margin"] for r in rs if r["account"] == a)) for a in accs]
                          + [_f(min(ms)) if ms else None, _f(max(ms)) if ms else None])
            ws2 = book.create_sheet("Товары WB")
            ws2.append(["Кабинет", "Категория", "Артикул (без цвета)", "Артикул 1С", "Размер", "Цвет", "ID_1С",
                        "Цена до скидки", "Цена со скидкой", "Скидка, %", "Комиссия, %", "К получению, руб",
                        "Себестоимость SKU, $", "Себестоимость артикула (макс), $",
                        "Себестоимость артикула, руб", "Маржинальность (от макс)", "Маржинальность (от своей)"])
            for r in sorted(rows, key=lambda r: (r["category"], r["model"], r["article"], r["size"], r["account"])):
                ws2.append([r["account"], r["category"], r["model"], r["article"], r["size"], r["color"],
                            r["item_id"], r["list"], r["sale"],
                            _f(r["disc"] * 100, 1) if r["disc"] is not None else None, _f(r["commission"]),
                            _f(r["payout"], 0), _f(r["cost_usd"]), _f(r["cost_max_usd"]),
                            _f(r["cost_max_rub"], 0), _f(r["margin"]), _f(r["margin_own"])])
            book.save(args.xlsx)
            print(f"\nФайл: {args.xlsx}")
    finally:
        db.rollback()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

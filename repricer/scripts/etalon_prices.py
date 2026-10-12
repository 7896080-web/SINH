"""Цены Kit и Ламоды от эталона WB через скидку продавца — ТОЛЬКО ЧТЕНИЕ, ничего не отправляет.

Логика (задана владельцем 12.10):
  * эталон по артикулу 1С = средняя по всем кабинетам WB ЦЕНА ПРОДАВЦА СО СКИДКОЙ минус 31%;
  * цель — цена ПОСЛЕ скидки продавца: Kit = эталон − 10%, Ламода = эталон;
  * цель достигается СКИДКОЙ продавца на площадке: при нынешней цене до скидки
    нужная скидка = 1 − цель / цена до скидки; коридор скидки — 35–50%;
  * скидка вне коридора — цену до скидки тоже менять: такую, чтобы скидка встала
    на ближнюю границу коридора (35% или 50%).

    C:\\repricer\\.venv\\Scripts\\python.exe C:\\repricer\\scripts\\etalon_prices.py --xlsx C:\\repricer\\эталон_цены.xlsx
    ... --wb-discount 31 --kit -10 --lamoda 0 --disc-min 35 --disc-max 50

Цель округляется тем же правилом, что и цены программы (шаг и окончание — «Правила
площадок»). Маржинальность — по цели: цель × (1 − комиссия) / себестоимость, ₽; ниже
пола площадки — пометка.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from decimal import ROUND_HALF_UP, Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(errors="replace")      # консоль Windows — cp1251

import priceapp  # noqa: E402,F401  (.env читается на импорте)
from priceapp import mapping, rates  # noqa: E402
from priceapp.database import SessionLocal  # noqa: E402
from priceapp.models import Account, OnecBarcode, OnecCost  # noqa: E402
from priceapp.platforms import PLATFORMS  # noqa: E402
from priceapp.pricing import (article_map, article_of, base_price, get_rule, item_facts,  # noqa: E402
                              round_price)

NO_CATEGORY = "(без категории)"

# Что делать с товаром — одно слово на строку, по нему же сводка.
ONLY_DISCOUNT = "сменить скидку"
LIST_UP = "поднять цену до скидки"
LIST_DOWN = "снизить цену до скидки"
SAME = "уже так"
NO_ETALON = "нет эталона (нет на WB)"
NO_CURRENT = "нет текущей цены"
NO_COST = "нет себестоимости"


def _accounts(db, platform):
    return (db.query(Account).filter(Account.is_active.is_(True), Account.platform == platform)
            .order_by(Account.name).all())


def etalon(db, articles, wb_discount: Decimal) -> dict[str, Decimal]:
    rule = get_rule(db, "wb")
    vals: dict[str, list[int]] = {}
    for a in _accounts(db, "wb"):
        for item_id, rows in mapping.account_items(db, a.id).items():
            sale = item_facts(rule, rows).sale
            if sale:
                vals.setdefault(article_of(articles, item_id), []).append(sale)
    return {art: Decimal(sum(v)) / len(v) * (1 - wb_discount / 100) for art, v in vals.items()}


def plan_item(target: int, current_list: int | None, current_sale: int | None,
              lo: Decimal, hi: Decimal) -> dict:
    """Как получить цену со скидкой `target`: какая скидка и какая цена до скидки."""
    if not current_list:
        return {"action": NO_CURRENT, "disc": None, "list": None}
    need = Decimal(1) - Decimal(target) / Decimal(current_list)
    if current_sale == target:
        return {"action": SAME, "disc": need, "list": current_list}
    if lo / 100 <= need <= hi / 100:
        return {"action": ONLY_DISCOUNT, "disc": need, "list": current_list}
    bound = lo / 100 if need < lo / 100 else hi / 100
    new_list = int((Decimal(target) / (1 - bound)).quantize(Decimal(1), ROUND_HALF_UP))
    return {"action": LIST_UP if new_list > current_list else LIST_DOWN, "disc": bound, "list": new_list,
            "need_at_current": need}


def compute(db, platform: str, shift: Decimal, et: dict, articles, costs, info, usd, lo, hi) -> list[dict]:
    rule = get_rule(db, platform)
    out = []
    for a in _accounts(db, platform):
        for item_id, rows in mapping.account_items(db, a.id).items():
            art = article_of(articles, item_id)
            facts = item_facts(rule, rows)
            sku = info.get(item_id)
            r = {"platform": platform, "account": a.name, "item_id": item_id, "article": art,
                 "size": sku.size if sku else "", "color": sku.color if sku else "",
                 "category": facts.category or NO_CATEGORY, "commission": facts.commission,
                 "current_list": facts.current, "current_sale": facts.sale,
                 "current_disc": facts.discount if facts.current else None,
                 "etalon": et.get(art), "target": None, "action": "", "disc": None, "list": None,
                 "change": None, "margin": None, "below_floor": False}
            if r["etalon"] is None:
                r["action"] = NO_ETALON
                out.append(r)
                continue
            r["target"] = round_price(r["etalon"] * (1 + shift / 100), rule.round_step, rule.round_minus)
            r.update(plan_item(r["target"], facts.current, facts.sale, lo, hi))
            if facts.sale:
                r["change"] = (r["target"] - facts.sale) * 100.0 / facts.sale
            base = base_price(costs.get(item_id), usd)
            if base and facts.commission is not None:
                r["margin"] = Decimal(r["target"]) * (1 - facts.commission / 100) / base
                r["below_floor"] = r["margin"] < Decimal(str(rule.min_markup_coef))
            elif not base:
                r["action"] = r["action"] or NO_COST
            out.append(r)
    return out


def _pct(d):
    return None if d is None else round(float(d) * 100, 1)


def _f(v, nd=2):
    return None if v is None else round(float(v), nd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wb-discount", type=Decimal, default=Decimal(31))
    ap.add_argument("--kit", type=Decimal, default=Decimal(-10))
    ap.add_argument("--lamoda", type=Decimal, default=Decimal(0))
    ap.add_argument("--disc-min", type=Decimal, default=Decimal(35))
    ap.add_argument("--disc-max", type=Decimal, default=Decimal(50))
    ap.add_argument("--show", type=int, default=15, help="примеров на площадку в консоли")
    ap.add_argument("--xlsx", default="", help="сохранить расчёт в .xlsx")
    args = ap.parse_args()
    lo, hi = args.disc_min, args.disc_max
    db = SessionLocal()
    try:
        rate = rates.current(db)
        if rate is None:
            print("Курса доллара нет — себестоимость в рублях не посчитать. Страница «Курс $».")
            return 1
        articles = article_map(db)
        costs = {c.item_id: c.cost_usd for c in db.query(OnecCost)}
        info = {}
        for b in db.query(OnecBarcode):
            info.setdefault(b.item_id, b)
        et = etalon(db, articles, args.wb_discount)
        print(f"Эталон: средняя цена продавца со скидкой на WB минус {args.wb_discount}% — {len(et)} артикулов. "
              f"Коридор скидки продавца {lo}–{hi}%. НИЧЕГО НЕ ОТПРАВЛЯЕТСЯ.")
        rows = []
        for platform, shift in (("kit", args.kit), ("lamoda", args.lamoda)):
            if not _accounts(db, platform):
                print(f"\n{PLATFORMS[platform]}: активных кабинетов нет.")
                continue
            items = compute(db, platform, shift, et, articles, costs, info, rate.usd_rub, lo, hi)
            rows += items
            cnt: dict[str, int] = {}
            for r in items:
                cnt[r["action"]] = cnt.get(r["action"], 0) + 1
            ch = [r["change"] for r in items if r["change"] is not None]
            print(f"\n=== {PLATFORMS[platform]}: цель со скидкой = эталон {shift:+}% — товаров {len(items)}")
            for act in (SAME, ONLY_DISCOUNT, LIST_UP, LIST_DOWN, NO_ETALON, NO_CURRENT, NO_COST):
                if cnt.get(act):
                    print(f"  {cnt[act]:6d}  {act}")
            if ch:
                print(f"  цена для покупателя изменится: медиана {statistics.median(ch):+.1f}%, "
                      f"от {min(ch):+.1f}% до {max(ch):+.1f}%")
            bf = sum(1 for r in items if r["below_floor"])
            if bf:
                print(f"  ниже пола по цели: {bf} — такая цена не ушла бы")
            print(f"  примеры:")
            shown = [r for r in items if r["target"]][:args.show]
            for r in shown:
                disc = f"скидка {_pct(r['disc'])}%" if r["disc"] is not None else ""
                lst = f"цена до скидки {r['list']}" if r["list"] else ""
                print(f"    {r['article']} {r['size']} {r['color']}: сейчас {r['current_list'] or '-'} / "
                      f"{r['current_sale'] or '-'} ({_pct(r['current_disc']) if r['current_disc'] is not None else '-'}%)"
                      f" -> цель {r['target']}: {r['action']}; {lst} {disc}".rstrip())
        if args.xlsx:
            from priceapp.excel import xlsx_bytes
            head = ["Площадка", "Кабинет", "Категория", "Артикул", "Размер", "Цвет", "ID_1С",
                    "Эталон (WB −%), ₽", "Цель со скидкой, ₽", "Сейчас: цена до скидки, ₽",
                    "Сейчас: цена со скидкой, ₽", "Сейчас: скидка, %", "Что сделать",
                    "Новая цена до скидки, ₽", "Новая скидка, %", "Изменение цены для покупателя, %",
                    "Маржинальность по цели", "Ниже пола", "Комиссия, %"]
            data = [[PLATFORMS[r["platform"]], r["account"], r["category"], r["article"], r["size"], r["color"],
                     r["item_id"], _f(r["etalon"], 0), r["target"], r["current_list"], r["current_sale"],
                     _pct(r["current_disc"]), r["action"], r["list"], _pct(r["disc"]),
                     _f(r["change"], 1), _f(r["margin"]), "да" if r["below_floor"] else "",
                     _f(r["commission"])]
                    for r in sorted(rows, key=lambda r: (r["platform"], r["category"], r["article"], r["size"]))]
            with open(args.xlsx, "wb") as f:
                f.write(xlsx_bytes(head, data))
            print(f"\nФайл: {args.xlsx}")
    finally:
        db.rollback()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

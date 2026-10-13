"""Цены Ламоды от эталона WB: план и ОТПРАВКА через API (цена до скидки + цена со скидкой).

Правило (владелец, 13.10):
  * цель — цена ПОСЛЕ скидки продавца = эталон WB (цена после скидки продавца, средняя по
    кабинетам) минус 31% — тот же расчёт, что `etalon_prices.py --only lamoda`;
  * скидка ЦЕЛАЯ и в коридоре 35–50%: нынешняя скидка карточки (округлённая), если она в
    коридоре, иначе ближняя граница; цена до скидки = цель / (1 − скидка), вверх до рубля —
    цена со скидкой остаётся ровно целью;
  * цена на Ламоде — на КАРТОЧКУ (parentSku), одна на все размеры: при разных целях размеров
    уходит наибольшая; разброс больше 5% — карточку не трогаем и называем;
  * ниже пола хотя бы у одного размера — карточку не трогаем.

Отправка — `POST /v2/nomenclatures-prices` с `price`, `salePrice`, `saleStart`, `saleEnd`
(без дат Ламода скидку не ставит), `force: false` ВСЕГДА: убрать товар из акции решает
человек в кабинете Ламоды, отказ по акции приходит ошибкой со словами, что делать.

    .venv\\Scripts\\python.exe scripts\\lamoda_apply.py --xlsx C:\\repricer\\ламода_отправка.xlsx
        план, ничего не отправляет
    ... --send --limit 3          сначала проба на трёх карточках
    ... --send --article 32481    проба на одном артикуле
    ... --send                    всё (спросит ДА; --yes — без вопроса)
    ... --verify                  после перезапуска программы: держит ли Ламода план

Карточки, где Ламода уже держит план, не отправляются.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(errors="replace")      # консоль Windows — cp1251
except Exception:
    pass

import priceapp  # noqa: E402,F401  (.env читается на импорте)
import etalon_prices as ep  # noqa: E402
from priceapp import audit, mapping, rates  # noqa: E402
from priceapp.database import SessionLocal  # noqa: E402
from priceapp.models import Account, OnecBarcode, OnecCost, PlatformItem  # noqa: E402
from priceapp.platforms import LAMODA_PUSH_CHUNK, lamoda_parent  # noqa: E402
from priceapp.pricing import article_map  # noqa: E402
from priceapp.timeutils import now_utc  # noqa: E402

MAX_SPREAD = Decimal("0.05")      # разброс целей размеров одной карточки


@dataclass
class LamodaCard:
    account_id: int
    account: str
    parent: str
    articles: str
    category: str
    cur_list: int | None
    cur_sale: int | None
    target: int | None = None
    new_list: int | None = None
    disc: int | None = None
    skip: str = ""
    margin: Decimal | None = None
    rows: list = field(default_factory=list)

    @property
    def cur_disc(self) -> int | None:
        if not self.cur_list or not self.cur_sale:
            return None
        return int(round((1 - self.cur_sale / self.cur_list) * 100))

    @property
    def already(self) -> bool:
        return (not self.skip and self.cur_list == self.new_list and self.cur_sale == self.target)


def list_for(target: int, cur_disc: int | None, lo: int, hi: int) -> tuple[int, int]:
    """Цена до скидки и целая скидка: нынешняя, если в коридоре, иначе ближняя граница;
    цена до скидки вверх до рубля — цена со скидкой остаётся целью."""
    d = cur_disc if cur_disc is not None and lo <= cur_disc <= hi else (lo if (cur_disc or 0) < lo else hi)
    return math.ceil(target * 100 / (100 - d) - 1e-9), d


def build_plan(db, lo: int, hi: int, wb_discount: Decimal = Decimal(31), shift: Decimal = Decimal(0)) -> list[LamodaCard]:
    rate = rates.current(db)
    if rate is None:
        raise SystemExit("Курса доллара нет — себестоимость в рублях не посчитать. Страница «Курс $».")
    articles = article_map(db)
    costs = {c.item_id: c.cost_usd for c in db.query(OnecCost)}
    info = {}
    for b in db.query(OnecBarcode):
        info.setdefault(b.item_id, b)
    et = ep.etalon(db, articles, wb_discount)
    rows = ep.compute(db, "lamoda", shift, et, articles, costs, info, rate.usd_rub, Decimal(lo), Decimal(hi))
    accs = {a.name: a for a in db.query(Account).filter(Account.platform == "lamoda")}
    cards: dict[tuple[int, str], LamodaCard] = {}
    for r in rows:
        a = accs[r["account"]]
        items = mapping.account_items(db, a.id).get(r["item_id"]) or []
        parents = {lamoda_parent(i.external_id) for i in items if lamoda_parent(i.external_id)}
        for p in parents:
            mine = [i for i in items if lamoda_parent(i.external_id) == p]
            c = cards.get((a.id, p))
            if c is None:
                c = cards[(a.id, p)] = LamodaCard(a.id, a.name, p, "", r["category"], None, None)
            c.rows.append(r)
            for i in mine:
                if i.current_price and (c.cur_list is None or i.current_price > c.cur_list):
                    c.cur_list, c.cur_sale = i.current_price, i.current_sale_price or i.current_price
    for c in cards.values():
        c.articles = ", ".join(sorted({r["article"] for r in c.rows}))
        targets = [r["target"] for r in c.rows if r["target"]]
        if len(targets) < len(c.rows):
            c.skip = "нет эталона WB хотя бы у одного размера"
            continue
        if any(r["below_floor"] for r in c.rows):
            c.skip = "ниже пола площадки по цели"
            continue
        if max(targets) > min(targets) * (1 + MAX_SPREAD):
            c.skip = f"цели размеров разные ({min(targets)}..{max(targets)}) — одной ценой на карточку нельзя"
            continue
        if not c.cur_list:
            c.skip = "нет текущей цены Ламоды — перезапустите программу"
            continue
        c.target = max(targets)
        margins = [r["margin"] for r in c.rows if r["margin"] is not None]
        c.margin = min(margins) if margins else None
        c.new_list, c.disc = list_for(c.target, c.cur_disc, lo, hi)
    return sorted(cards.values(), key=lambda c: (c.account, c.category, c.articles, c.parent))


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def send(db, cards: list[LamodaCard], sale_days: int, factory=None) -> dict[str, str]:
    """Отправить цену до скидки и цену со скидкой по карточкам. Принятые — сразу правим у нас
    текущую цену, чтобы `--verify` и страницы не ждали следующей загрузки."""
    from priceapp import accounts
    from priceapp.platforms import lamoda_push_failures
    out = {}
    start = now_utc().replace(microsecond=0) + timedelta(minutes=2)
    end = start + timedelta(days=sale_days)
    by_acc: dict[int, list[LamodaCard]] = {}
    for c in cards:
        by_acc.setdefault(c.account_id, []).append(c)
    for acc_id, group in by_acc.items():
        acc = db.get(Account, acc_id)
        client = accounts.client_for(db, acc, factory)
        sent, errors = 0, []
        for i in range(0, len(group), LAMODA_PUSH_CHUNK):
            chunk = group[i:i + LAMODA_PUSH_CHUNK]
            body = {"country": "RU", "force": False, "prices": [
                {"parentSku": c.parent, "price": {"amount": c.new_list * 100, "currency": "RUB"},
                 "salePrice": {"amount": c.target * 100, "currency": "RUB"},
                 "saleStart": _iso(start), "saleEnd": _iso(end)} for c in chunk]}
            try:
                resp = client._post("/v2/nomenclatures-prices", body)
            except Exception as e:
                text = str(e)
                resp_obj = getattr(e, "response", None)
                if resp_obj is not None and resp_obj.text:
                    text += " — " + resp_obj.text[:300]
                errors.append(text[:400])
                continue
            failed, unattributed = lamoda_push_failures(resp or {}, [c.parent for c in chunk])
            if unattributed:
                failed.update({c.parent: unattributed for c in chunk if c.parent not in failed})
            for c in chunk:
                if c.parent in failed:
                    errors.append(f"{c.parent} {c.articles}: {failed[c.parent]}"[:300])
                    continue
                sent += 1
                for it in db.query(PlatformItem).filter(
                        PlatformItem.account_id == acc.id,
                        PlatformItem.external_id.like(f"{c.parent}:%")):
                    it.current_price, it.current_sale_price = c.new_list, c.target
        out[acc.name] = f"отправлено карточек: {sent} из {len(group)}" + (
            f"; ошибки ({len(errors)}): " + "; ".join(errors[:10]) + (" …" if len(errors) > 10 else "")
            if errors else "")
        audit.log(db, "lamoda_apply", "lamoda_prices", acc.name,
                  f"{out[acc.name]}; скидка с {_iso(start)} по {_iso(end)}"[:1000])
        db.commit()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--disc-min", type=int, default=35)
    ap.add_argument("--disc-max", type=int, default=50)
    ap.add_argument("--wb-discount", type=Decimal, default=Decimal(31), help="Ламода = эталон WB минус N%%")
    ap.add_argument("--sale-days", type=int, default=365, help="срок скидки на Ламоде, дней")
    ap.add_argument("--xlsx", default="")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="отправить только первые N карточек (проба)")
    ap.add_argument("--article", default="", help="только карточки, где артикул 1С содержит этот текст (проба)")
    ap.add_argument("--verify", action="store_true", help="сверить план с ценами, загруженными с Ламоды")
    args = ap.parse_args(argv)
    lo, hi = args.disc_min, args.disc_max
    db = SessionLocal()
    try:
        cards = build_plan(db, lo, hi, args.wb_discount)
        if args.article:
            cards = [c for c in cards if args.article.lower() in c.articles.lower()]
            print(f"Только артикул «{args.article}»: карточек {len(cards)}")
        accs = db.query(Account).filter(Account.platform == "lamoda", Account.is_active.is_(True)).all()
        for a in accs:
            print(f"{a.name}: цены загружены с Ламоды {a.prices_loaded_at or '-'}")
            if a.guard_min_margin is not None:
                print(f"  ВНИМАНИЕ: у кабинета задан диапазон безопасности (от {a.guard_min_margin}) — программа может"
                      f" сама поднять цену до скидки, не зная о скидке Ламоды. Проверьте «Кабинеты».")
        go = [c for c in cards if not c.skip]
        if args.verify:
            ok = [c for c in go if c.already]
            bad = [c for c in go if not c.already]
            print(f"\nДержит план: {len(ok)}; НЕ держит: {len(bad)}")
            for c in bad[:30]:
                print(f"    {c.parent} {c.articles}: план {c.new_list} / {c.target} ({c.disc}%) — на Ламоде "
                      f"{c.cur_list} / {c.cur_sale} ({c.cur_disc}%)")
            return 0 if not bad else 1
        same = [c for c in go if c.already]
        go = [c for c in go if not c.already]
        print(f"\nКарточек Ламоды: {len(cards)}; к отправке {len(go)}; уже по плану {len(same)}; "
              f"не трогаем {sum(1 for c in cards if c.skip)}. Скидка целая, коридор {lo}-{hi}%.")
        up = sum(1 for c in go if c.cur_sale and c.target > c.cur_sale)
        dn = sum(1 for c in go if c.cur_sale and c.target < c.cur_sale)
        print(f"  цена со скидкой: дешевле {dn}, дороже {up}, прежняя {len(go) - up - dn}")
        big = sorted((c for c in go if c.cur_sale), key=lambda c: abs(c.target / c.cur_sale - 1), reverse=True)[:15]
        if big:
            print("\nСамые большие изменения:")
            for c in big:
                print(f"    {c.parent} {c.articles}: {c.cur_list} / {c.cur_sale} ({c.cur_disc}%) -> {c.new_list} / "
                      f"{c.target} ({c.disc}%), {(c.target / c.cur_sale - 1) * 100:+.1f}%")
        skipped = [c for c in cards if c.skip]
        if skipped:
            print(f"\nНЕ отправляются ({len(skipped)}):")
            reasons: dict[str, list[LamodaCard]] = {}
            for c in skipped:
                reasons.setdefault(c.skip.split(" (")[0], []).append(c)
            for r, cs in reasons.items():
                print(f"  {len(cs)}: {r} — {', '.join(sorted({c.articles for c in cs}))[:200]}")
        if args.xlsx:
            from openpyxl import Workbook
            book = Workbook()
            ws = book.active
            ws.title = "Ламода"
            ws.append(["Кабинет", "Категория", "parentSku", "Артикулы 1С", "Сейчас: цена до скидки, ₽",
                       "Сейчас: со скидкой, ₽", "Сейчас: скидка, %", "Новая цена до скидки, ₽",
                       "Новая цена со скидкой (цель), ₽", "Новая скидка, %", "Изменение со скидкой, %",
                       "Маржинальность по цели (мин. по размерам)", "Размеров", "Отправка"])
            for c in cards:
                ws.append([c.account, c.category, c.parent, c.articles, c.cur_list, c.cur_sale, c.cur_disc,
                           c.new_list, c.target, c.disc,
                           round((c.target / c.cur_sale - 1) * 100, 1) if c.target and c.cur_sale else None,
                           round(float(c.margin), 2) if c.margin is not None else None, len(c.rows),
                           c.skip or ("уже по плану" if c.already else "отправить")])
            book.save(args.xlsx)
            print(f"\nФайл: {args.xlsx}")
        if not args.send:
            print("\nНичего не отправлено. Проба: --send --limit 3; всё: --send")
            return 0
        if args.limit:
            go = go[:args.limit]
        if not go:
            print("\nОтправлять нечего.")
            return 0
        if not args.yes:
            ans = input(f"\nОтправить на Ламоду цену до скидки и цену со скидкой по {len(go)} карточкам? Введите ДА: ")
            if ans.strip().upper() != "ДА":
                print("Отменено, ничего не отправлено.")
                return 0
        for name, res in send(db, go, args.sale_days).items():
            print(f"  {name}: {res}")
        print("\nДальше: через 10–15 минут перезапустите программу (загрузит цены Ламоды), затем\n"
              "  .venv\\Scripts\\python.exe scripts\\lamoda_apply.py --verify")
    finally:
        db.rollback()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

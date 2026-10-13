r"""Новый эталон WB по заполненной форме маржинальности (scripts/etalon_cost.py).

  .venv\Scripts\python.exe scripts\etalon_apply.py --form маржинальность_wb.xlsx --xlsx план_эталон.xlsx
      ПЛАН: по каждой карточке WB — цена до скидки и скидка; ничего не отправляет
  ... --send      отправить план на WB (спросит подтверждение)
  ... --verify    дошёл ли план (по ценам, которые программа загрузила с WB после отправки)
  ... --adopt     сверить и, если всё дошло, записать эталон и включить правило эталонной цены

--send ВЫКЛЮЧАЕТ правило эталонной цены до --adopt: иначе оно, увидев новую цену до скидки
при старом эталоне, вернуло бы старую (13.10 так ушло 3831 возврат).

Правило (задано владельцем 13.10):
  * маржинальность кабинета для артикула — самая частная заполненная: «<кабинет>:
    маржинальность (если иная)», затем «Маржинальность артикула», затем маржинальность
    категории (лист «Категории»). Пусто везде — карточку не трогаем;
  * цель — цена СО СКИДКОЙ: себестоимость артикула, руб (из формы — та, что видел владелец)
    × маржинальность / (1 − комиссия), вверх до рубля; комиссия — нынешняя комиссия товара
    в кабинете (тариф WB по категории), как и в форме;
  * цель достигается ценой до скидки при нынешней скидке карточки, если та в коридоре
    35–50%; иначе скидка — на ближайшую границу. Цена до скидки — вверх до рубля, чтобы
    цена со скидкой была не ниже цели;
  * снижение цены для покупателя — ШАГАМИ не больше --max-drop (33%) за раз: WB от 50% одним
    изменением не применяет, а больше порога карантина категории (33,3% по умолчанию) — в
    карантин. Каждый --send делает следующий шаг от нынешней цены; --adopt ждёт конца шагов;
  * цена на WB — одна на карточку (nmID). Размеры от разных артикулов 1С на одной карточке —
    себестоимость СРЕДНЯЯ по этим артикулам, маржинальность заданная; разная маржинальность
    у них — карточку не трогаем и называем.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(errors="replace")      # консоль Windows — cp1251
except Exception:
    pass

import priceapp  # noqa: E402,F401  (.env читается на импорте)
import wb_restore_discount as wrd  # noqa: E402
from etalon_cost import model_of  # noqa: E402
from priceapp import mapping, reference, settings  # noqa: E402
from priceapp.database import SessionLocal  # noqa: E402
from priceapp.models import Account  # noqa: E402
from priceapp.pricing import article_map, article_of, get_rule, item_facts  # noqa: E402


def _num(v):
    if v is None or v == "":
        return None
    try:
        x = float(str(v).replace(",", ".").strip())
    except ValueError:
        return None
    return x if x > 0 else None


def read_form(path: Path) -> tuple[dict, dict, list[str]]:
    """(артикул -> {cost_rub, art, over{кабинет: m}, category}, категория -> m, кабинеты)."""
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    for need in ("Артикулы", "Категории"):
        if need not in book.sheetnames:
            raise SystemExit(f"В файле нет листа «{need}» — нужна форма scripts/etalon_cost.py")
    rows = book["Артикулы"].iter_rows(values_only=True)
    head = [str(h or "").strip() for h in next(rows)]
    key = "Артикул" if "Артикул" in head else "Изделие"
    for need in (key, "Категория", "Себестоимость макс, руб", "Маржинальность артикула"):
        if need not in head:
            raise SystemExit(f"На листе «Артикулы» нет колонки «{need}»")
    ix = {h: i for i, h in enumerate(head) if h}
    cabs = [h[:-len(": маржинальность (если иная)")] for h in head if h.endswith(": маржинальность (если иная)")]
    arts = {}
    for r in rows:
        a = r[ix[key]]
        if a is None or str(a).strip() == "":
            continue
        arts[str(a).strip()] = {
            "category": str(r[ix["Категория"]] or ""),
            "cost_rub": _num(r[ix["Себестоимость макс, руб"]]),
            "art": _num(r[ix["Маржинальность артикула"]]),
            "over": {c: _num(r[ix[f"{c}: маржинальность (если иная)"]]) for c in cabs}}
    cats = {}
    for r in list(book["Категории"].iter_rows(values_only=True))[1:]:
        if r and r[0]:
            cats[str(r[0])] = _num(r[3] if len(r) > 3 else None)
    return arts, cats, cabs


def margin_for(arts: dict, cats: dict, art: str, cab: str):
    d = arts.get(art)
    if d is None:
        return None
    return d["over"].get(cab) or d["art"] or cats.get(d["category"])


def target_sale(cost_rub: float, m: float, commission) -> int:
    return math.ceil(cost_rub * m / (1 - float(commission) / 100) - 1e-9)


def list_for(target: int, cur_disc: int | None, lo: int, hi: int) -> tuple[int, int]:
    """Цена до скидки и целая скидка: скидка карточки, если в коридоре, иначе ближняя граница;
    цена до скидки вверх — цена со скидкой не ниже цели."""
    d = cur_disc if cur_disc is not None and lo <= cur_disc <= hi else (lo if (cur_disc or 0) < lo else hi)
    return math.ceil(target * 100 / (100 - d) - 1e-9), d


def already_set(c, live: dict) -> bool:
    """WB уже держит цену до скидки и скидку из плана."""
    cur = live.get((c.account_id, c.nm))
    return bool(cur) and cur.price == c.new_list and cur.discount == c.disc


def build_plan(db, arts: dict, cats: dict, live: dict, lo: int, hi: int, max_drop: float = 33) -> list:
    """PlanCard на каждую карточку WB, где форма задаёт маржинальность.

    На одной карточке бывают размеры от РАЗНЫХ артикулов 1С («2403» и «2425 b (2403)») с
    разной себестоимостью, а цена на WB одна. Тогда себестоимость карточки — СРЕДНЯЯ по её
    артикулам, маржинальность — заданная (решение владельца 13.10). Разная маржинальность у
    артикулов одной карточки — карточку не трогаем и называем: выбирать за человека нельзя.
    Комиссия — наибольшая среди размеров карточки (обычно она одна)."""
    rule = get_rule(db, "wb")
    articles = article_map(db)
    out = []
    for a in db.query(Account).filter(Account.is_active.is_(True), Account.platform == "wb").order_by(Account.name):
        cards: dict[str, dict] = {}
        for item_id, rows in mapping.account_items(db, a.id).items():
            art = model_of(article_of(articles, item_id))
            facts = item_facts(rule, rows)
            for r in rows:
                nm = (r.external_id or "").split(":")[0]
                if not nm.isdigit():
                    continue
                c = cards.setdefault(nm, {"arts": {}, "comm": []})
                c["arts"][art] = (margin_for(arts, cats, art, a.name), (arts.get(art) or {}).get("cost_rub"))
                if facts.commission is not None:
                    c["comm"].append(facts.commission)
        for nm, c in sorted(cards.items(), key=lambda kv: (sorted(kv[1]["arts"]), kv[0])):
            cur = live.get((a.id, nm))
            given = {art: v for art, v in c["arts"].items() if v[0] is not None}
            if not given:
                continue                                # маржинальность не задана — не трогаем
            pc = wrd.PlanCard(a.id, a.name, nm, ", ".join(sorted(c["arts"]))[:120],
                              cur.price if cur else 0, cur.sale if cur else 0)
            margins = {v[0] for v in given.values()}
            costs = [v[1] for v in given.values() if v[1]]
            if len(margins) > 1:
                pc.skip = "у артикулов карточки разная маржинальность (" + ", ".join(
                    f"{art} {v[0]:g}" for art, v in sorted(given.items())) + ")"
            elif len(costs) < len(given):
                pc.skip = "нет себестоимости в форме"
            elif not c["comm"]:
                pc.skip = "нет комиссии"
            elif cur is None:
                pc.skip = "карточки нет в загруженных ценах WB"
            else:
                cost = sum(costs) / len(costs)
                pc.target = target_sale(cost, next(iter(margins)), max(c["comm"]))
                pc.cost_rub = round(cost, 2)
                pc.averaged = len(costs) > 1
                pc.final_target = pc.target
                floor = math.ceil(cur.sale * (1 - max_drop / 100) - 1e-9) if cur.sale else 0
                if pc.target < floor:
                    # WB не применяет снижение цены для покупателя на 50% и больше одним
                    # изменением, а снижение больше порога категории (33,3% по умолчанию)
                    # уводит в карантин. Снижаем ШАГАМИ от нынешней цены; следующий --send
                    # сделает следующий шаг.
                    pc.target, pc.step = floor, True
                pc.new_list, pc.disc = list_for(pc.target, cur.discount, lo, hi)
            out.append(pc)
    return out


def adopt(accs: dict) -> int:
    """Новый эталон: цены и скидки, которые WB держит сейчас, — эталонными; правило — включить."""
    from priceapp import discount_guard
    db = SessionLocal()
    try:
        st = reference.adopt_current(db, actor="etalon_apply")
        disc = discount_guard.adopt(db, actor="etalon_apply")
        settings.put(db, settings.REF_ENABLED, "1")
        db.commit()
    finally:
        db.close()
    print(f"\nЭталон записан: цен {st['recorded']}; скидок " + ", ".join(f"{k} {v}" for k, v in disc.items())
          + ". Правило эталонной цены ВКЛЮЧЕНО.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Новый эталон WB по форме маржинальности")
    ap.add_argument("--form", required=True)
    ap.add_argument("--xlsx", default="")
    ap.add_argument("--disc-min", type=int, default=35)
    ap.add_argument("--disc-max", type=int, default=50)
    ap.add_argument("--max-drop", type=float, default=33,
                    help="наибольшее снижение цены для покупателя за один шаг, %% (WB: от 50%% не применяет, "
                         "больше порога карантина — в карантин)")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--adopt", action="store_true",
                    help="после чистой сверки: записать эталон и включить правило эталонной цены")
    ap.add_argument("--force", action="store_true", help="с --adopt: записать эталон и при несовпадениях")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение (для проверок)")
    ap.add_argument("--after", default="", help="база «сейчас» (по умолчанию рабочая)")
    args = ap.parse_args(argv)
    if not Path(args.form).exists():
        print(f"Нет файла {args.form}")
        return 1
    arts, cats, cabs = read_form(Path(args.form))
    live, accs = wrd.load(Path(args.after) if args.after else wrd._live_db())
    db = SessionLocal()
    try:
        cards = build_plan(db, arts, cats, live, args.disc_min, args.disc_max, args.max_drop)
    finally:
        db.rollback()
        db.close()
    if args.verify or args.adopt:
        steps = [c for c in cards if getattr(c, "step", False)]
        if steps:
            print(f"Снижаются по шагам, ещё не у цели: {len(steps)} — следующий шаг: --send")
            for c in steps[:10]:
                print(f"    {c.account}: {c.nm} {c.article}: сейчас со скидкой {c.old_sale}, цель {c.final_target}")
        cards = [c for c in cards if not getattr(c, "step", False)]
        bad = [c for c in cards if not c.skip and not already_set(c, live)]
        wrd.verify_plan(cards, live, accs)
        if not args.adopt:
            return 0
        if (bad or steps) and not args.force:
            print(f"\nЭталон НЕ записан: не совпадает {len(bad)}, по шагам {len(steps)}. Записать всё равно (то, что WB держит "
                  f"сейчас, станет эталоном): добавьте --force")
            return 1
        return adopt(accs)
    go = [c for c in cards if not c.skip]
    # WB отвечает 400 на загрузку, где ВСЕ цены и скидки уже стоят («already set»), и это
    # выглядело как ошибка кабинета. Карточку, которая уже держит план, не отправляем вовсе.
    already = [c for c in go if not getattr(c, "step", False) and already_set(c, live)]
    if already:
        for c in already:
            c.skip = "уже стоит на WB по плану"
        go = [c for c in go if not c.skip]
        print(f"Уже стоят на WB по плану, не отправляются: {len(already)}")
    print(f"Форма {args.form}: артикулов {len(arts)}; кабинеты {', '.join(cabs)}. "
          f"Скидка в коридоре {args.disc_min}-{args.disc_max}%.")
    for name, t in accs.values():
        print(f"  {name}: цены загружены с WB {t or '-'}")
    print(f"\nКарточек к отправке: {len(go)}")
    for name in sorted({c.account for c in go}):
        cs = [c for c in go if c.account == name]
        ch = [(c.target / c.old_sale - 1) * 100 for c in cs if c.old_sale]
        up = sum(1 for x in ch if x > 0.5)
        dn = sum(1 for x in ch if x < -0.5)
        med = sorted(ch)[len(ch) // 2] if ch else 0
        av = sum(1 for c in cs if getattr(c, "averaged", False))
        print(f"  {name}: {len(cs)} (из них по средней себестоимости артикулов — {av}); цена со скидкой: дешевле {dn}, дороже {up}, без изменений {len(ch) - up - dn}; "
              f"медиана {med:+.1f}%")
    big = sorted((c for c in go if c.old_sale), key=lambda c: abs(c.target / c.old_sale - 1), reverse=True)[:15]
    if big:
        print("\nСамые большие изменения цены со скидкой:")
        for c in big:
            print(f"    {c.account}: {c.nm} {c.article}: {c.old_list} / {c.old_sale} -> {c.new_list}, скидка {c.disc}%,"
                  f" со скидкой {c.target} ({(c.target / c.old_sale - 1) * 100:+.1f}%)")
    stepping = [c for c in go if getattr(c, "step", False)]
    if stepping:
        print(f"\nСнижаются ПО ШАГАМ (за раз не больше {args.max_drop:g}% цены для покупателя): {len(stepping)}."
              f" Этот шаг — до промежуточной цены; дальше: --send ещё раз, когда WB применит.")
        for c in stepping[:10]:
            print(f"    {c.account}: {c.nm} {c.article}: со скидкой {c.old_sale} -> {c.target} (цель {c.final_target})")
    skipped = [c for c in cards if c.skip]
    if skipped:
        print(f"\nНЕ отправляются ({len(skipped)}):")
        for c in skipped[:20]:
            print(f"    {c.account}: {c.nm} {c.article}: {c.skip}")
    if args.xlsx:
        from openpyxl import Workbook
        book = Workbook()
        book.remove(book.active)
        for name in sorted({c.account for c in cards}):
            ws = book.create_sheet(name[:31])
            ws.append(["Артикул WB", "Артикул", "Сейчас: цена до скидки", "Сейчас: со скидкой",
                       "Новая цена до скидки", "Новая скидка, %", "Цель со скидкой", "Изменение, %",
                       "Себестоимость карточки, руб", "Средняя по артикулам", "Не отправляется"])
            for c in cards:
                if c.account == name:
                    t = getattr(c, "target", None)
                    ws.append([int(c.nm), c.article, c.old_list or None, c.old_sale or None, c.new_list, c.disc, t,
                               round((t / c.old_sale - 1) * 100, 1) if t and c.old_sale else None,
                               getattr(c, "cost_rub", None), "да" if getattr(c, "averaged", False) else "", c.skip])
        book.save(args.xlsx)
        print(f"\nФайл: {args.xlsx}")
    if not args.send:
        print("\nНичего не отправлено. Отправить: добавьте --send")
        return 0
    if not go:
        print("\nОтправлять нечего.")
        return 0
    if not args.yes:
        ans = input(f"\nОтправить на WB цену до скидки и скидку по {len(go)} карточкам? Введите ДА: ")
        if ans.strip().upper() != "ДА":
            print("Отменено, ничего не отправлено.")
            return 0
    db = SessionLocal()
    try:
        # Правило эталонной цены, увидев новую (более низкую) цену до скидки при старом
        # эталоне, решит, что цену занизила площадка, и ВЕРНЁТ старую — 13.10 так ушло 3831
        # возврат. Выключаем его до записи нового эталона (--adopt включит обратно).
        if reference.enabled(db):
            settings.put(db, settings.REF_ENABLED, "0")
            db.commit()
            print("Правило эталонной цены ВЫКЛЮЧЕНО до записи нового эталона (--adopt включит его).")
        for name, res in wrd.send_plan(db, cards).items():
            print(f"  {name}: {res}")
    finally:
        db.close()
    print("\nДальше: через 10 минут перезапустите программу (загрузит цены WB), затем\n"
          f"  .venv\\Scripts\\python.exe scripts\\etalon_apply.py --form {args.form} --adopt\n"
          "— сверит и, если всё дошло, запишет эталон и включит правило эталонной цены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

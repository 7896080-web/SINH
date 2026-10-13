r"""Вернуть на WB скидку продавца, которую сменила площадка, — по копии базы ДО смены.

Запуск (из C:\repricer):
  .venv\Scripts\python.exe scripts\wb_restore_discount.py --before backups\repricer-ГГГГММДД-ЧЧММСС.db
      показать, где скидка сейчас не та, что была в копии; ничего не отправляет
  ... --plan-xlsx эталон.xlsx         по отчёту эталона: цена до скидки и скидка под коридор
                                      35–50%, цена со скидкой — как в «Сейчас» (не растёт)
  ... --from-xlsx эталон.xlsx         то же, но «как было» — из отчёта эталона (лист «WB»,
                                      колонки «Сейчас: цена до скидки / со скидкой»)
  ... --xlsx скидки.xlsx              то же плюс файл (лист на кабинет)
  ... --send                          ОТПРАВИТЬ прежнюю скидку на WB (спросит подтверждение)

«Сейчас» — цены в рабочей базе, то есть загруженные программой при последнем опросе WB.
Поэтому сначала запустите программу: при запуске она загружает цены WB, и рабочая база
увидит новую скидку. Скрипт печатает, когда загружены цены в копии и в рабочей базе.

Отправляется ТОЛЬКО скидка (nmID + discount): цену до скидки WB оставляет прежней.
Карточки, у которых изменилась и цена до скидки, не отправляются — прежняя скидка от
другой цены дала бы другую цену для покупателя; они печатаются отдельно.
Скидка на WB — целый процент на карточку (nmID), не на размер.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REPRICER_SESSION_SECRET", "x" * 32)

CHUNK = 1000


@dataclass
class Card:
    account_id: int
    account: str
    nm: str
    article: str
    price: int
    sale: int

    @property
    def discount(self) -> int:
        if not self.price or not self.sale or self.sale >= self.price:
            return 0
        return int((Decimal(100) - Decimal(self.sale) * 100 / Decimal(self.price)).quantize(Decimal(1), ROUND_HALF_UP))


def load(path: Path) -> tuple[dict[tuple[int, str], Card], dict[int, tuple[str, str]]]:
    """(кабинет, nmID) -> карточка; кабинет -> (имя, когда загружены цены). Только чтение.
    Цена карточки — по строке с наибольшей ценой, как у программы при отправке."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        accs = {i: (n, str(t or "")) for i, n, t in con.execute(
            "SELECT id, name, prices_loaded_at FROM accounts WHERE platform = 'wb'")}
        cards: dict[tuple[int, str], Card] = {}
        for acc, ext, art, price, sale in con.execute(
                "SELECT account_id, external_id, article, current_price, current_sale_price FROM platform_items "
                "WHERE current_price IS NOT NULL"):
            if acc not in accs:
                continue
            nm = (ext or "").split(":")[0]
            if not nm.isdigit():
                continue
            c = cards.get((acc, nm))
            if c is None or price > c.price:
                cards[(acc, nm)] = Card(acc, accs[acc][0], nm, art or "", int(price), int(sale or price))
        return cards, accs
    finally:
        con.close()


def load_xlsx(path: Path, db) -> dict[tuple[int, str], Card]:
    """Прежние цены из отчёта эталона (scripts/etalon_prices.py, лист «WB»): кабинет, ID_1С,
    «Сейчас: цена до скидки», «Сейчас: цена со скидкой». Отчёт знает SKU 1С, а WB — карточку
    (nmID): её берём из каталога кабинета в рабочей базе. Цена карточки — по строке с
    наибольшей ценой, как у программы при отправке."""
    from openpyxl import load_workbook
    from priceapp import mapping
    from priceapp.models import Account
    book = load_workbook(path, read_only=True, data_only=True)
    if "WB" not in book.sheetnames:
        raise SystemExit(f"В файле {path} нет листа «WB» — нужен отчёт scripts/etalon_prices.py")
    rows = book["WB"].iter_rows(values_only=True)
    head = list(next(rows))
    need = ["Кабинет", "ID_1С", "Сейчас: цена до скидки, ₽", "Сейчас: цена со скидкой, ₽"]
    missing = [h for h in need if h not in head]
    if missing:
        raise SystemExit(f"На листе «WB» нет колонок: {', '.join(missing)}")
    ix = [head.index(h) for h in need]
    accs = {a.name: a for a in db.query(Account).filter(Account.platform == "wb")}
    items: dict[int, dict] = {}
    cards: dict[tuple[int, str], Card] = {}
    for r in rows:
        name, item_id, price, sale = (r[i] for i in ix)
        a = accs.get(name)
        if a is None or not item_id or not price:
            continue
        if a.id not in items:
            items[a.id] = mapping.account_items(db, a.id)
        for it in items[a.id].get(str(item_id), []):
            nm = (it.external_id or "").split(":")[0]
            if not nm.isdigit():
                continue
            c = cards.get((a.id, nm))
            if c is None or int(price) > c.price:
                cards[(a.id, nm)] = Card(a.id, a.name, nm, it.article or "", int(price), int(sale or price))
    return cards


@dataclass
class PlanCard:
    account_id: int
    account: str
    nm: str
    article: str
    old_list: int
    old_sale: int
    new_list: int | None = None
    disc: int | None = None
    skip: str = ""


def corridor(old_list: int, old_sale: int, lo: int, hi: int) -> tuple[int, int]:
    """Цена до скидки и ЦЕЛАЯ скидка (на WB она целая) так, чтобы цена со скидкой осталась
    прежней и НЕ выросла даже на рубль. Скидка в коридоре — цена до скидки та же; иначе
    скидка на ближайшую границу, цена до скидки = прежняя со скидкой / (1 − граница), вниз."""
    need = Decimal(100) - Decimal(old_sale) * 100 / Decimal(old_list)
    d = int(need.quantize(Decimal(1), ROUND_HALF_UP))
    if Decimal(old_list) * (100 - d) / 100 > old_sale + Decimal("0.5"):
        d += 1
    if lo <= d <= hi:
        return old_list, d
    bound = lo if d < lo else hi
    return int(Decimal(old_sale) * 100 // (100 - bound)), bound


def load_plan(path: Path, db, after: dict, lo: int, hi: int) -> list[PlanCard]:
    """План по отчёту эталона: «как было» — колонки «Сейчас: …» листа «WB»; по карточке
    (nmID) цена до скидки и скидка под коридор. Карточку НЕ трогаем, если цены размеров в
    отчёте разные (цена уходит одна на карточку — дешёвые размеры подорожали бы) или если
    цена до скидки на WB с отчёта сменилась (отчёт про неё устарел)."""
    from openpyxl import load_workbook
    from priceapp import mapping
    from priceapp.models import Account
    book = load_workbook(path, read_only=True, data_only=True)
    if "WB" not in book.sheetnames:
        raise SystemExit(f"В файле {path} нет листа «WB» — нужен отчёт scripts/etalon_prices.py")
    rows = book["WB"].iter_rows(values_only=True)
    head = list(next(rows))
    need = ["Кабинет", "ID_1С", "Сейчас: цена до скидки, ₽", "Сейчас: цена со скидкой, ₽"]
    missing = [h for h in need if h not in head]
    if missing:
        raise SystemExit(f"На листе «WB» нет колонок: {', '.join(missing)}")
    ix = [head.index(h) for h in need]
    accs = {a.name: a for a in db.query(Account).filter(Account.platform == "wb")}
    items: dict[int, dict] = {}
    seen: dict[tuple[int, str], set] = {}
    art: dict[tuple[int, str], str] = {}
    for r in rows:
        name, item_id, price, sale = (r[i] for i in ix)
        a = accs.get(name)
        if a is None or not item_id or not price or not sale:
            continue
        if a.id not in items:
            items[a.id] = mapping.account_items(db, a.id)
        for it in items[a.id].get(str(item_id), []):
            nm = (it.external_id or "").split(":")[0]
            if nm.isdigit():
                seen.setdefault((a.id, nm), set()).add((int(price), int(sale)))
                art.setdefault((a.id, nm), it.article or "")
    names = {a.id: a.name for a in accs.values()}
    out = []
    for key, pairs in sorted(seen.items(), key=lambda kv: (names[kv[0][0]], art[kv[0]], kv[0][1])):
        old_list = max(p for p, _ in pairs)
        old_sale = next(s for p, s in pairs if p == old_list)
        c = PlanCard(key[0], names[key[0]], key[1], art[key], old_list, old_sale)
        live = after.get(key)
        if len({s for _, s in pairs}) > 1:
            c.skip = "цены со скидкой у размеров разные — одной ценой на карточку дешёвые подорожали бы"
        elif len({p for p, _ in pairs}) > 1:
            c.skip = "цены до скидки у размеров разные"
        elif live is None:
            c.skip = "карточки нет в загруженных ценах WB"
        elif live.price != old_list:
            c.skip = f"цена до скидки на WB сменилась с отчёта: было {old_list}, сейчас {live.price}"
        else:
            c.new_list, c.disc = corridor(old_list, old_sale, lo, hi)
        out.append(c)
    return out


def send_plan(db, cards: list[PlanCard], factory=None) -> dict[str, str]:
    """Цена до скидки и скидка на карточку WB (nmID)."""
    from priceapp import accounts, audit
    from priceapp.models import Account
    from priceapp.platforms import WB_PRICES
    out = {}
    by_acc: dict[int, list[PlanCard]] = {}
    for c in cards:
        if not c.skip:
            by_acc.setdefault(c.account_id, []).append(c)
    for acc_id, group in by_acc.items():
        acc = db.get(Account, acc_id)
        client = accounts.client_for(db, acc, factory)
        sent, errors = 0, []
        for start in range(0, len(group), CHUNK):
            chunk = group[start:start + CHUNK]
            body = {"data": [{"nmID": int(c.nm), "price": c.new_list, "discount": c.disc} for c in chunk]}
            try:
                data = client._post(f"{WB_PRICES}/api/v2/upload/task", body)
            except Exception as e:
                errors.append(str(e)[:200])
                continue
            if isinstance(data, dict) and data.get("error"):
                errors.append(str(data.get("errorText") or "WB вернул error=true")[:200])
                continue
            sent += len(chunk)
        out[acc.name] = f"отправлено карточек: {sent}" + (f"; ошибки: {'; '.join(errors)}" if errors else "")
        audit.log(db, "wb_restore_discount", "wb_corridor_prices", acc.name, out[acc.name][:500])
        db.commit()
    return out


def run_plan(args, after_path: Path) -> int:
    after, accs_a = load(after_path)
    from priceapp.database import SessionLocal
    db = SessionLocal()
    try:
        cards = load_plan(Path(args.plan_xlsx), db, after, args.disc_min, args.disc_max)
    finally:
        db.rollback()
        db.close()
    go = [c for c in cards if not c.skip]
    print(f"План по отчёту {args.plan_xlsx}: цена со скидкой — как в колонке «Сейчас: цена со скидкой», "
          f"скидка в коридоре {args.disc_min}–{args.disc_max}%.")
    for name, t in accs_a.values():
        print(f"  {name}: цены загружены с WB {t or '-'}")
    print(f"\nКарточек к отправке: {len(go)}")
    per: dict[str, list[PlanCard]] = {}
    for c in go:
        per.setdefault(c.account, []).append(c)
    for name, cs in per.items():
        same = sum(1 for c in cs if c.new_list == c.old_list)
        print(f"  {name}: {len(cs)} (цена до скидки та же, меняется скидка — {same}; "
              f"меняется цена до скидки — {len(cs) - same})")
    for c in go[:15]:
        print(f"    {c.account}: {c.nm} {c.article}: было {c.old_list} / со скидкой {c.old_sale} -> "
              f"{c.new_list}, скидка {c.disc}% (со скидкой {c.new_list * (100 - c.disc) // 100})")
    skipped = [c for c in cards if c.skip]
    if skipped:
        print(f"\nНЕ отправляются ({len(skipped)}):")
        reasons: dict[str, int] = {}
        for c in skipped:
            r = c.skip.split(":")[0]
            reasons[r] = reasons.get(r, 0) + 1
        for r, n in reasons.items():
            print(f"  {n:5d}  {r}")
    if args.xlsx:
        from openpyxl import Workbook
        book = Workbook()
        book.remove(book.active)
        for name in sorted({c.account for c in cards}):
            ws = book.create_sheet(name[:31])
            ws.append(["Артикул WB", "Артикул продавца", "Было: цена до скидки", "Было: со скидкой",
                       "Новая цена до скидки", "Новая скидка, %", "Со скидкой будет", "Не отправляется"])
            for c in cards:
                if c.account == name:
                    ws.append([int(c.nm), c.article, c.old_list, c.old_sale, c.new_list, c.disc,
                               c.new_list * (100 - c.disc) // 100 if c.new_list else None, c.skip])
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
        for name, res in send_plan(db, cards).items():
            print(f"  {name}: {res}")
    finally:
        db.close()
    print("\nПроверка: запустите программу (она загрузит цены WB) и повторите команду без --send.")
    return 0


def plan(before: dict, after: dict) -> tuple[list[tuple[Card, Card]], list[tuple[Card, Card]]]:
    """(вернуть скидку, не трогать: сменилась и цена до скидки)."""
    restore, price_moved = [], []
    for key, b in sorted(before.items(), key=lambda kv: (kv[1].account, kv[1].article, kv[1].nm)):
        a = after.get(key)
        if a is None or a.discount == b.discount:
            continue
        (restore if a.price == b.price else price_moved).append((b, a))
    return restore, price_moved


def send(db, restore: list[tuple[Card, Card]], factory=None) -> dict[str, str]:
    """Прежняя скидка на WB — только поле discount, цену до скидки не передаём."""
    from priceapp import accounts, audit
    from priceapp.models import Account
    from priceapp.platforms import WB_PRICES
    out = {}
    by_acc: dict[int, list[tuple[Card, Card]]] = {}
    for b, a in restore:
        by_acc.setdefault(b.account_id, []).append((b, a))
    for acc_id, pairs in by_acc.items():
        acc = db.get(Account, acc_id)
        if acc is None or acc.platform != "wb":
            out[pairs[0][0].account] = "кабинета нет в рабочей базе — не отправлено"
            continue
        client = accounts.client_for(db, acc, factory)
        sent, errors = 0, []
        for start in range(0, len(pairs), CHUNK):
            chunk = pairs[start:start + CHUNK]
            body = {"data": [{"nmID": int(b.nm), "discount": b.discount} for b, _ in chunk]}
            try:
                data = client._post(f"{WB_PRICES}/api/v2/upload/task", body)
            except Exception as e:
                errors.append(str(e)[:200])
                continue
            if isinstance(data, dict) and data.get("error"):
                errors.append(str(data.get("errorText") or "WB вернул error=true")[:200])
                continue
            sent += len(chunk)
        out[acc.name] = f"скидка возвращена: {sent} карточек" + (f"; ошибки: {'; '.join(errors)}" if errors else "")
        audit.log(db, "wb_restore_discount", "wb_restore_discount", acc.name, out[acc.name][:500])
        db.commit()
    return out


def _live_db() -> Path:
    from priceapp.database import DATABASE_URL
    if not DATABASE_URL.startswith("sqlite:///"):
        raise SystemExit("рабочая база не SQLite-файл")
    return Path(DATABASE_URL[len("sqlite:///"):])


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Вернуть на WB скидку продавца по копии базы")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--before", help="копия базы ДО смены скидки (backups\\repricer-*.db)")
    src.add_argument("--plan-xlsx", help="отчёт эталона: цена до скидки и скидка под коридор, "
                                         "цена со скидкой — как в «Сейчас»")
    src.add_argument("--from-xlsx", help="отчёт эталона (лист «WB», колонки «Сейчас: …») — цены ДО смены скидки")
    ap.add_argument("--after", default="", help="база «сейчас» (по умолчанию рабочая)")
    ap.add_argument("--xlsx", default="")
    ap.add_argument("--send", action="store_true", help="отправить прежнюю скидку на WB")
    ap.add_argument("--disc-min", type=int, default=35)
    ap.add_argument("--disc-max", type=int, default=50)
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение (для проверок)")
    args = ap.parse_args(argv)
    after_path = Path(args.after) if args.after else _live_db()
    if args.plan_xlsx:
        if not Path(args.plan_xlsx).exists():
            print(f"Нет файла {args.plan_xlsx}")
            return 1
        return run_plan(args, after_path)
    before_path = Path(args.before or args.from_xlsx)
    if not before_path.exists():
        print(f"Нет файла {before_path}")
        return 1
    if before_path.resolve() == after_path.resolve():
        print("Копия и рабочая база — один и тот же файл.")
        return 1
    after, accs_a = load(after_path)
    if args.from_xlsx:
        from priceapp.database import SessionLocal
        db = SessionLocal()
        try:
            before = load_xlsx(before_path, db)
        finally:
            db.rollback()
            db.close()
        accs_b = {}
        print(f"Было (файл): {before_path} — карточек WB {len(before)}")
    else:
        before, accs_b = load(before_path)
        print(f"Копия:   {before_path}")
    print(f"Сейчас:  {after_path}")
    for i, (name, t) in accs_a.items():
        tb = accs_b.get(i, ("", ""))[1]
        warn = "  <- цены в рабочей базе НЕ новее копии: запустите программу, чтобы она загрузила цены WB" \
            if t and tb and t <= tb else ""
        print(f"  {name}: цены {'в копии ' + (tb or '-') + ', ' if not args.from_xlsx else ''}загружены с WB {t or '-'}{warn}")
    restore, moved = plan(before, after)
    print(f"\nСкидка сменилась, цена до скидки та же — вернуть: {len(restore)} карточек")
    per: dict[str, int] = {}
    for b, _ in restore:
        per[b.account] = per.get(b.account, 0) + 1
    for name, n in per.items():
        print(f"  {name}: {n}")
    for b, a in restore[:15]:
        print(f"    {b.account}: {b.nm} {b.article}: цена {a.price}, скидка сейчас {a.discount}% "
              f"(со скидкой {a.sale}) -> вернуть {b.discount}% (было со скидкой {b.sale})")
    if moved:
        print(f"\nСменилась и цена до скидки — НЕ отправляются ({len(moved)}), смотрите в кабинете WB:")
        for b, a in moved[:15]:
            print(f"    {b.account}: {b.nm} {b.article}: было {b.price} / {b.discount}%, сейчас {a.price} / {a.discount}%")
    if args.xlsx:
        from openpyxl import Workbook
        book = Workbook()
        book.remove(book.active)
        head = ["Артикул WB", "Артикул продавца", "Цена до скидки", "Скидка была, %", "Скидка сейчас, %",
                "Вернуть скидку, %", "Со скидкой была", "Со скидкой сейчас"]
        for name in sorted({b.account for b, _ in restore + moved}):
            ws = book.create_sheet(name[:31])
            ws.append(head + ["Примечание"])
            for b, a in restore + moved:
                if b.account != name:
                    continue
                note = "" if a.price == b.price else f"цена до скидки сменилась: было {b.price} — не отправляется"
                ws.append([int(b.nm), b.article, a.price, b.discount, a.discount,
                           b.discount if not note else None, b.sale, a.sale, note])
        if not book.sheetnames:
            book.create_sheet("Нет изменений").append(["Скидка нигде не сменилась"])
        book.save(args.xlsx)
        print(f"\nФайл: {args.xlsx}")
    if not args.send:
        print("\nНичего не отправлено. Отправить прежнюю скидку: добавьте --send")
        return 0
    if not restore:
        print("\nОтправлять нечего.")
        return 0
    if not args.yes:
        ans = input(f"\nОтправить прежнюю скидку на WB по {len(restore)} карточкам? Введите ДА: ")
        if ans.strip().upper() != "ДА":
            print("Отменено, ничего не отправлено.")
            return 0
    from priceapp.database import SessionLocal
    db = SessionLocal()
    try:
        for name, res in send(db, restore).items():
            print(f"  {name}: {res}")
    finally:
        db.close()
    print("\nПроверка: перезапустите программу (она загрузит цены WB заново) и запустите скрипт ещё раз "
          "без --send — карточек «вернуть» должно стать 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

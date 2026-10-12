r"""Вернуть на WB скидку продавца, которую сменила площадка, — по копии базы ДО смены.

Запуск (из C:\repricer):
  .venv\Scripts\python.exe scripts\wb_restore_discount.py --before backups\repricer-ГГГГММДД-ЧЧММСС.db
      показать, где скидка сейчас не та, что была в копии; ничего не отправляет
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
    ap.add_argument("--before", required=True, help="копия базы ДО смены скидки (backups\\repricer-*.db)")
    ap.add_argument("--after", default="", help="база «сейчас» (по умолчанию рабочая)")
    ap.add_argument("--xlsx", default="")
    ap.add_argument("--send", action="store_true", help="отправить прежнюю скидку на WB")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение (для проверок)")
    args = ap.parse_args(argv)
    before_path = Path(args.before)
    after_path = Path(args.after) if args.after else _live_db()
    if not before_path.exists():
        print(f"Нет файла {before_path}")
        return 1
    if before_path.resolve() == after_path.resolve():
        print("Копия и рабочая база — один и тот же файл.")
        return 1
    before, accs_b = load(before_path)
    after, accs_a = load(after_path)
    print(f"Копия:   {before_path}")
    print(f"Сейчас:  {after_path}")
    for i, (name, t) in accs_a.items():
        tb = accs_b.get(i, ("", ""))[1]
        warn = "  <- цены в рабочей базе НЕ новее копии: запустите программу, чтобы она загрузила цены WB" \
            if t and tb and t <= tb else ""
        print(f"  {name}: цены в копии {tb or '-'}, сейчас {t or '-'}{warn}")
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

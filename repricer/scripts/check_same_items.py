"""Проверка цен и наценок одинаковых товаров по площадкам — ТОЛЬКО ЧТЕНИЕ.

Одинаковый товар — общий SKU 1С хотя бы в двух кабинетах. Считается тем же
`compare_all`, что и «Правила и журнал» -> «Сравнение площадок», поэтому числа
совпадают со страницей. В базу ничего не пишет, на площадки ничего не шлёт.

    C:\\repricer\\.venv\\Scripts\\python.exe C:\\repricer\\scripts\\check_same_items.py
    ... check_same_items.py --xlsx C:\\repricer\\проверка.xlsx   # подробности в файл
    ... check_same_items.py --show 30                            # строк на расхождение
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Консоль Windows — cp1251: символ вне неё не должен обрывать вывод.
sys.stdout.reconfigure(errors="replace")

import priceapp  # noqa: E402,F401  (.env читается на импорте)
from priceapp.database import SessionLocal  # noqa: E402
from priceapp.routers import prices as P  # noqa: E402


def cell_text(a, c) -> str:
    if c is None:
        return f"{a.name}: -"
    return (f"{a.name}: тек. {c['current'] or '-'}, нов. {c['price'] if c['price'] is not None else '-'}"
            f" (маржин. {P._f(c['markup_coef']) if c['markup_coef'] is not None else '-'}),"
            f" {P.target_label(c).replace('×', 'x')}")   # знака × в cp1251 нет


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--show", type=int, default=10, help="сколько товаров печатать на каждое расхождение")
    ap.add_argument("--xlsx", default="", help="сохранить все строки с расхождениями в этот .xlsx")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        accs = P._accounts(db)
        rows, cols, summary = P.compare_all(db, accs)
        print(f"Кабинетов: {len(cols)} ({', '.join(a.name for a in cols)})")
        print(f"Одинаковых товаров (общий SKU 1С хотя бы в двух кабинетах): {summary['shared']}")
        print(f"С расхождениями: {summary['issues']}")
        for k, label in P.ISSUES.items():
            print(f"  {summary[k]:6d}  {label}")
        print(f"  {summary['markup_differs']:6d}  наценки разные (между площадками это нормально)")
        for k, label in P.ISSUES.items():
            hit = [r for r in rows if k in r["issues"]]
            if not hit:
                continue
            print(f"\n=== {label}: {len(hit)}" + (f", первые {args.show}" if len(hit) > args.show else ""))
            for r in hit[:args.show]:
                s = r["sku"]
                print(f"- {s.article if s else r['item_id']} {s.size if s else ''} {s.color if s else ''}"
                      f" [{r['item_id']}]")
                for a in cols:
                    if r["cells"][a.id] is not None:
                        print("    " + cell_text(a, r["cells"][a.id]))
        if args.xlsx:
            from priceapp.excel import xlsx_bytes
            headers, data = P.compare_table([r for r in rows if r["issues"]], cols)
            with open(args.xlsx, "wb") as f:
                f.write(xlsx_bytes(headers, data))
            print(f"\nФайл: {args.xlsx}")
    finally:
        db.rollback()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

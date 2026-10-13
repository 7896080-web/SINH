r"""Мэппинг Sync Admin -> репрайсер: привязки «баркод -> SKU 1С», сделанные в Sync Admin.

В Sync Admin (браузер на сервере, вход как обычно) — три выгрузки «Мэппинга» по источнику
(вся выгрузка целиком упирается в 50 000 строк, а строки из справочника 1С у нас и так есть):
  http://127.0.0.1:8000/mapping/export?view=mapped&source_platform=excel_import
  http://127.0.0.1:8000/mapping/export?view=mapped&source_platform=excel_repoint
  http://127.0.0.1:8000/mapping/export?view=mapped&source_platform=pool_match
Файлы — на рабочий ПК и сюда:

  .venv\Scripts\python.exe scripts\load_sync_mapping.py файл1.xlsx файл2.xlsx файл3.xlsx
      ПОКАЗАТЬ, что изменится в сопоставлении по каждому кабинету; ничего не пишет
  ... --apply
      записать (снимок заменяется целиком — передавайте ВСЕ файлы сразу)

Правила — как в Sync Admin (`priceapp/mapping.py`): привязка файлом и переподвязка главнее
справочника 1С; догадка по пулу — только для баркода, которого в справочнике нет.
Изменённое сопоставление меняет себестоимость, от которой считается цена, — на площадки
ничего не уходит, но следующий расчёт цен пойдёт по новому сопоставлению.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(errors="replace")      # консоль Windows — cp1251
except Exception:
    pass

import priceapp  # noqa: E402,F401  (.env читается на импорте)
from priceapp import audit, mapping  # noqa: E402
from priceapp.database import Base, SessionLocal, engine  # noqa: E402
from priceapp.models import Account  # noqa: E402
from priceapp.platforms import PLATFORMS  # noqa: E402


def snapshot(db) -> dict[int, dict[str, tuple[str, str]]]:
    out = {}
    for a in db.query(Account).order_by(Account.platform, Account.name):
        out[a.id] = {r.item.barcode: (r.status, r.item_id) for r in mapping.build(db, a.id)}
    return out


def diff(before: dict, after: dict) -> dict[int, dict]:
    res = {}
    for acc, b in before.items():
        a = after.get(acc, {})
        d = {"newly": [], "changed": [], "lost": [], "same": 0}
        for code, (st0, id0) in b.items():
            st1, id1 = a.get(code, ("", ""))
            m0, m1 = st0 in mapping.MAPPED, st1 in mapping.MAPPED
            if not m0 and m1:
                d["newly"].append((code, id1))
            elif m0 and m1 and id0 != id1:
                d["changed"].append((code, id0, id1))
            elif m0 and not m1:
                d["lost"].append((code, id0))
            else:
                d["same"] += 1
        res[acc] = d
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Мэппинг Sync Admin -> репрайсер")
    ap.add_argument("files", nargs="+", help="выгрузки «Мэппинг» Sync Admin (.xlsx)")
    ap.add_argument("--apply", action="store_true", help="записать")
    args = ap.parse_args(argv)
    rows = []
    for f in args.files:
        p = Path(f)
        if not p.exists():
            print(f"Нет файла {p}")
            return 1
        try:
            got = mapping.parse_sync_mapping(p.read_bytes())
        except mapping.MappingError as e:
            print(f"{p.name}: {e}")
            return 1
        print(f"{p.name}: привязок Sync Admin {len(got)} "
              f"({', '.join(f'{k} {v}' for k, v in sorted(Counter(r['source'] for r in got).items())) or '-'})")
        if len(got) >= 50000:
            print(f"  ВНИМАНИЕ: {p.name} — 50 000 строк, это предел выгрузки Sync Admin: файл, скорее всего,"
                  f" обрезан. Выгрузите по частям (отбор поиском) и передайте все файлы.")
        rows += got
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        before = snapshot(db)
        try:
            st = mapping.load_sync_mapping(db, rows)
        except mapping.MappingError as e:
            print(e)
            return 1
        after = snapshot(db)
        accs = {a.id: a for a in db.query(Account)}
        print(f"\nВсего привязок Sync Admin: {st['rows']} "
              f"({', '.join(f'{k} {v}' for k, v in sorted(st['by_source'].items()))})")
        print("\nЧто изменится в сопоставлении (строки каталога кабинетов):")
        for acc, d in diff(before, after).items():
            a = accs[acc]
            print(f"  {PLATFORMS.get(a.platform, a.platform)} / {a.name}: стало сопоставлено {len(d['newly'])}; "
                  f"другой SKU 1С {len(d['changed'])}; перестало {len(d['lost'])}; без изменений {d['same']}")
            for code, id0, id1 in d["changed"][:5]:
                print(f"      {code}: было {id0} -> стало {id1}")
            for code, id1 in d["newly"][:3]:
                print(f"      {code}: новое -> {id1}")
        if not args.apply:
            db.rollback()
            print("\nНичего не записано. Записать: добавьте --apply")
            return 0
        audit.log(db, "load_sync_mapping", "sync_mapping", "",
                  f"мэппинг Sync Admin: {st['rows']} привязок {st['by_source']}")
        db.commit()
        print("\nЗаписано.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

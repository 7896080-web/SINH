r"""Служебная утилита эталонных цен WB (запуск: .venv\Scripts\python.exe scripts\reference_tool.py <команда>).

  adopt-sent   записать эталонными уже отправленные цены (ProductPrice.last_sent_price) —
               после отправки цен, сделанной до запуска правила; уже записанные эталонные
               перезаписываются последней отправленной ценой
  status       сколько эталонных цен по кабинетам, настройки правила, курс эталонных и текущий
  run          один прогон правила (пересчёт по курсу + возврат цен) по уже загруженным ценам
  enable / disable   включить или выключить правило
  adopt-current      НОВЫЙ ЭТАЛОН WB: цены до скидки и скидки, которые площадка держит сейчас
                     (загруженные программой), — эталонными. Перед этим перезапустите программу,
                     чтобы она загрузила свежие цены, и сверьте их (wb_restore_discount.py --verify)
  adopt-discount     нынешние скидки WB (загруженные программой) — эталонными; программа будет
                     возвращать их, если WB сменит скидку сам (автоакцией)
  discount-check     где скидка на WB сейчас не эталонная (по загруженным ценам; ничего не шлёт)
  discount-on / discount-off   включить или выключить проверку скидки
"""
import sys
from pathlib import Path

# Консоль Windows — cp1251: символ из имени кабинета или ответа площадки, которого в ней нет,
# ронял бы вывод на полуслове. «?» вместо символа лучше трассировки.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ.setdefault("REPRICER_SESSION_SECRET", "x" * 32)

from priceapp import discount_guard, rates, reference, settings          # noqa: E402
from priceapp.database import Base, SessionLocal, engine  # noqa: E402
from priceapp.models import Account, ReferenceDiscount, ReferencePrice      # noqa: E402


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    Base.metadata.create_all(bind=engine)         # как при запуске программы: заводит только недостающие таблицы
    db = SessionLocal()
    try:
        if cmd == "adopt-sent":
            print(reference.adopt_sent(db))
        elif cmd == "run":
            print(reference.summary(reference.run(db, actor="reference_tool")))
        elif cmd == "adopt-current":
            from priceapp.models import Account as A
            for a in db.query(A).filter(A.platform == "wb", A.is_active.is_(True)):
                print(f"{a.name}: цены загружены с WB {a.prices_loaded_at}")
            st = reference.adopt_current(db, actor="reference_tool")
            print(f"эталонных цен записано: {st['recorded']} (без цены на WB: {st['skipped']})")
            for name, n in discount_guard.adopt(db, actor="reference_tool").items():
                print(f"{name}: эталонных скидок {n}")
        elif cmd == "adopt-discount":
            for name, n in discount_guard.adopt(db, actor="reference_tool").items():
                print(f"{name}: эталонных скидок {n}")
        elif cmd == "discount-check":
            from priceapp.models import Account as A
            for a in db.query(A).filter(A.platform == "wb"):
                bad = discount_guard.mismatches(db, a)
                print(f"{a.name}: скидка не эталонная у {len(bad)} карточек (цены загружены {a.prices_loaded_at})")
                for nm, cur, ref, _ in bad[:10]:
                    print(f"    {nm}: сейчас {cur}%, эталонная {ref}%")
        elif cmd in ("discount-on", "discount-off"):
            settings.put(db, settings.DISCOUNT_GUARD_ENABLED, "1" if cmd == "discount-on" else "0")
            db.commit()
            print("проверка скидки WB:", "включена" if cmd == "discount-on" else "выключена")
        elif cmd in ("enable", "disable"):
            settings.put(db, settings.REF_ENABLED, "1" if cmd == "enable" else "0")
            db.commit()
            print("правило эталонной цены:", "включено" if cmd == "enable" else "выключено")
        else:
            cur = rates.current(db)
            print("правило:", "включено" if reference.enabled(db) else "ВЫКЛЮЧЕНО",
                  "| падение", reference.drop_percent(db), "% | курс", reference.rate_percent(db), "% | текущий курс",
                  cur.usd_rub if cur else None, "| курс сместился за порог:", reference.rate_moved(db))
            for a in db.query(Account).filter(Account.platform == "wb"):
                rows = db.query(ReferencePrice).filter(ReferencePrice.account_id == a.id).all()
                rates_ = sorted({str(r.usd_rub) for r in rows})
                nd = db.query(ReferenceDiscount).filter(ReferenceDiscount.account_id == a.id).count()
                print(f"{a.name}: эталонных цен {len(rows)}; курсы эталонных {rates_}; эталонных скидок {nd}; "
                      f"цены загружены {a.prices_loaded_at}")
            print("проверка скидки:", "включена" if discount_guard.enabled(db) else "ВЫКЛЮЧЕНА")
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv)

r"""Служебная утилита эталонных цен WB (запуск: .venv\Scripts\python.exe scripts\reference_tool.py <команда>).

  adopt-sent   записать эталонными уже отправленные цены (ProductPrice.last_sent_price) —
               после отправки цен, сделанной до запуска правила; уже записанные эталонные
               перезаписываются последней отправленной ценой
  status       сколько эталонных цен по кабинетам, настройки правила, курс эталонных и текущий
  run          один прогон правила (пересчёт по курсу + возврат цен) по уже загруженным ценам
  enable / disable   включить или выключить правило
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ.setdefault("REPRICER_SESSION_SECRET", "x" * 32)

from priceapp import rates, reference, settings          # noqa: E402
from priceapp.database import Base, SessionLocal, engine  # noqa: E402
from priceapp.models import Account, ReferencePrice      # noqa: E402


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    Base.metadata.create_all(bind=engine)         # как при запуске программы: заводит только недостающие таблицы
    db = SessionLocal()
    try:
        if cmd == "adopt-sent":
            print(reference.adopt_sent(db))
        elif cmd == "run":
            print(reference.summary(reference.run(db, actor="reference_tool")))
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
                print(f"{a.name}: эталонных цен {len(rows)}; курсы эталонных {rates_}; цены загружены {a.prices_loaded_at}")
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv)

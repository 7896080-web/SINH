"""Сроки хранения: журнал цен и журнал действий не растут бесконечно.

Симуляция 15.10: около 60 строк журнала цен в сутки на 160 товаров — на боевом каталоге это
1–2 млн строк в год, а «Цены товаров» читают отправленные строки и на миллионе открываются
около 30 с (аудит 14.10, п. 4.3). Удаляем ТОЛЬКО прошлое: отправленные и отклонённые
предложения старше `CHANGES_DAYS`. Подтверждённые, заблокированные и ожидающие не трогаем
никогда — это незаконченные дела; отказы площадки (`error`) тоже — отчёт сам решает, живы ли
они. Последняя отправленная цена по товару при этом не теряется: она хранится отдельно
(`ProductPrice.last_sent_price`). Порциями: одно DELETE на сотни тысяч строк держало бы
запись базы минутами.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from priceapp.models import AuditLog, PriceChange, PriceChangeStatus
from priceapp.timeutils import now_utc

CHANGES_DAYS = 180
AUDIT_DAYS = 365
PORTION = 1000


def _purge(db: Session, model, *conds) -> int:
    n = 0
    while True:
        ids = [i for (i,) in db.query(model.id).filter(*conds).limit(PORTION)]
        if not ids:
            return n
        db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        n += len(ids)


def prune(db: Session) -> dict:
    now = now_utc()
    changes = _purge(db, PriceChange,
                     PriceChange.status.in_((PriceChangeStatus.sent.value, PriceChangeStatus.rejected.value)),
                     PriceChange.created_at < now - timedelta(days=CHANGES_DAYS))
    audit = _purge(db, AuditLog, AuditLog.at < now - timedelta(days=AUDIT_DAYS))
    return {"price_changes": changes, "audit": audit}

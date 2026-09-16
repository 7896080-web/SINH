from datetime import datetime
from app.timeutils import now_utc

from sqlalchemy.orm import Session

from app.models import Barcode, MappingConflict


def resolve_barcode(db: Session, barcode: str, account_id: int) -> str | None:
    """Возвращает uid_1c товара по баркоду, либо None, если баркод неизвестен —
    в этом случае конфликт фиксируется в MappingConflict, привязанный к
    конкретному кабинету (раздел 3 спецификации + кабинеты WB).
    Ничего не коммитит — вызывающий код сам решает, когда коммитить."""

    row = db.query(Barcode).filter(Barcode.barcode == barcode).first()
    if row is not None:
        return row.uid_1c

    conflict = db.query(MappingConflict).filter(
        MappingConflict.barcode == barcode, MappingConflict.account_id == account_id,
    ).first()

    if conflict is not None:
        conflict.attempts += 1
        conflict.last_seen = now_utc()
    else:
        db.add(MappingConflict(barcode=barcode, account_id=account_id, attempts=1))

    return None

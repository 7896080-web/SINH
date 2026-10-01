from sqlalchemy.orm import Session

from priceapp.models import AuditLog


def log(db: Session, username: str, action: str, obj: str = "", details: str = "") -> None:
    """Запись в журнал. Коммитит вызывающий — вместе с самим действием."""
    db.add(AuditLog(username=username or "", action=action, object=obj[:100], details=details))

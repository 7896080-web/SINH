from sqlalchemy.orm import Session

from finapp.models import AuditLog


def log_action(db: Session, actor: str, action: str, details: str = "") -> None:
    """Запись в журнал действий. Коммитит вызывающий — вместе с самим действием."""
    db.add(AuditLog(actor=actor or "", action=action, details=details))

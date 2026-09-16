from sqlalchemy.orm import Session

from app.models import AuditLog


def log_action(db: Session, actor: str, action: str, details: str = ""):
    db.add(AuditLog(actor=actor, action=action, details=details))

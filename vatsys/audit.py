"""Audit log helper: every change to data that feeds a return is recorded."""

from sqlalchemy.orm import Session

from .models import AuditLog


def log(session: Session, action: str, *, user_id: int | None = None, business_id: int | None = None,
        entity: str | None = None, entity_id: int | None = None, **detail) -> None:
    session.add(AuditLog(action=action, user_id=user_id, business_id=business_id, entity=entity,
                         entity_id=entity_id, detail=detail or None))

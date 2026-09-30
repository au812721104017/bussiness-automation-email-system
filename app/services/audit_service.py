import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from app.db.models import AuditEvent

# Must contain at least: timestamp, run_id, event_type, status, safe_metadata (per spec)

ALLOWED_EVENT_TYPES = {
    "RUN_CREATED", "RUN_STATUS_CHANGED",
    "EMAIL_RECEIVED", "EMAIL_NORMALIZED",
    "DRAFT_CREATED", "DRAFT_UPDATED",
    "APPROVAL_REQUESTED", "APPROVAL_GRANTED", "APPROVAL_REJECTED",
    "SEND_ATTEMPTED", "SEND_SUCCEEDED", "SEND_FAILED",
    "ERROR", "RETRY",
}

def _sanitize_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    if metadata is None:
        return None
    # Never allow raw secrets/bodies - caller must pass safe fields only
    # We do a simple json dump; caller responsibility to filter
    try:
        return json.dumps(metadata, ensure_ascii=False, default=str)[:4000]
    except Exception:
        return json.dumps({"error": "metadata_serialization_failed"})

def create_audit_event(db: Session, *, run_id: str, event_type: str, status: str = "success", safe_metadata: Optional[Dict[str, Any]] = None) -> AuditEvent:
    if not run_id:
        raise ValueError("run_id is required for audit event")
    if not event_type:
        raise ValueError("event_type is required")
    # Normalize - allow custom but prefer allowed set
    safe_meta_str = _sanitize_metadata(safe_metadata)
    event = AuditEvent(
        run_id=run_id,
        event_type=event_type,
        status=status,
        safe_metadata=safe_meta_str,
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event

def get_audits_by_run(db: Session, run_id: str):
    return db.query(AuditEvent).filter(AuditEvent.run_id == run_id).order_by(AuditEvent.created_at.asc()).all()

def get_all_audits(db: Session, limit: int = 100):
    return db.query(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit).all()

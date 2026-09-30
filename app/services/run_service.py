import uuid
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from app.db.models import AgentRun, AuditEvent
from app.services.audit_service import create_audit_event

def generate_run_id() -> str:
    return str(uuid.uuid4())

def create_run(db: Session, *, status: str = "started", email_id: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None, run_id: Optional[str] = None) -> AgentRun:
    rid = run_id or generate_run_id()
    meta_str = json.dumps(metadata, ensure_ascii=False, default=str)[:4000] if metadata else None
    run = AgentRun(run_id=rid, status=status, email_id=email_id, meta_data=meta_str)
    db.add(run)
    db.commit()
    db.refresh(run)
    # Every important state change must create audit event
    create_audit_event(db, run_id=rid, event_type="RUN_CREATED", status="success", safe_metadata={"status": status, "email_id": email_id})
    return run

def update_run_status(db: Session, run_id: str, new_status: str, safe_metadata: Optional[Dict[str, Any]] = None) -> AgentRun:
    run = db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
    if not run:
        raise ValueError(f"Run {run_id} not found")
    old_status = run.status
    run.status = new_status
    run.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    create_audit_event(db, run_id=run_id, event_type="RUN_STATUS_CHANGED", status="success", safe_metadata={"old_status": old_status, "new_status": new_status, **(safe_metadata or {})})
    return run

def get_run(db: Session, run_id: str) -> Optional[AgentRun]:
    return db.query(AgentRun).filter(AgentRun.run_id == run_id).first()

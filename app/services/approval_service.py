import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from app.db.models import DraftReply, Approval, AgentRun, AuditEvent
from app.services.audit_service import create_audit_event
from app.core.logging import get_logger

log = get_logger(__name__)

# State machine: RECEIVED -> CLASSIFIED -> DRAFTED -> PENDING_APPROVAL -> APPROVED/REJECTED -> SENDABLE
VALID_TRANSITIONS = {
    "started": ["drafted"],
    "drafted": ["pending_approval"],
    "pending_approval": ["approved", "rejected"],
    "approved": ["sendable", "rejected"],  # can reject after approve before send
    "rejected": ["pending_approval"],  # re-approval cycle
}

class ApprovalError(Exception):
    pass

def _now():
    return datetime.now(timezone.utc)

def request_approval(db: Session, *, run_id: str, draft_id: int, reason: Optional[str] = None) -> Approval:
    """Create PENDING_APPROVAL — explicit, belongs to exact run/draft, recorded in audit."""
    draft = db.query(DraftReply).filter(DraftReply.id == draft_id).first()
    if not draft:
        raise ApprovalError("Draft not found")
    if draft.run_id != run_id:
        raise ApprovalError("run_id does not match draft.run_id — approval belongs to specific run/draft")
    # Check existing approval for this exact pair
    existing = db.query(Approval).filter(Approval.run_id == run_id, Approval.draft_id == draft_id).first()
    if existing:
        if existing.status == "pending":
            return existing
        # If already approved/rejected, do not allow duplicate request for same draft without invalidation
        raise ApprovalError(f"Approval already exists for this run/draft with status {existing.status}")
    # Ensure draft hasn't been modified after previous approval attempt: check if draft updated_at > any prior approval? Simplified: only one approval per draft due to unique constraint
    ap = Approval(run_id=run_id, draft_id=draft_id, status="pending", reason=reason, created_at=_now())
    db.add(ap)
    # Update run status
    run = db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
    if run:
        run.status = "pending_approval"
        run.updated_at = _now()
    db.commit()
    db.refresh(ap)
    create_audit_event(db, run_id=run_id, event_type="APPROVAL_REQUESTED", status="success", safe_metadata={"draft_id": draft_id, "approval_id": ap.id})
    log.info("Approval requested", extra={"extra_data": {"run_id": run_id, "draft_id": draft_id, "approval_id": ap.id}})
    return ap

def approve(db: Session, *, approval_id: int, approver: str, reason: Optional[str] = None) -> Approval:
    ap = db.query(Approval).filter(Approval.id == approval_id).first()
    if not ap:
        raise ApprovalError("Approval not found")
    if ap.status != "pending":
        raise ApprovalError(f"Cannot approve — approval status is {ap.status}, must be pending")
    # Verify draft still exists and hasn't changed: compare draft updated_at vs approval created_at
    draft = db.query(DraftReply).filter(DraftReply.id == ap.draft_id).first()
    if not draft:
        raise ApprovalError("Draft not found for approval")
    if draft.updated_at and ap.created_at and draft.updated_at > ap.created_at:
        # Invalidate — draft changed
        ap.status = "rejected"
        ap.reason = "auto-invalidated: draft modified after approval request"
        ap.decided_at = _now()
        db.commit()
        create_audit_event(db, run_id=ap.run_id, event_type="APPROVAL_REJECTED", status="success", safe_metadata={"approval_id": ap.id, "reason": "draft_modified_invalidation", "draft_id": draft.id})
        raise ApprovalError("Draft was modified after approval request — approval invalidated. Request new approval.")
    ap.status = "approved"
    ap.approver = approver
    ap.reason = reason
    ap.decided_at = _now()
    run = db.query(AgentRun).filter(AgentRun.run_id == ap.run_id).first()
    if run:
        run.status = "approved"
        run.updated_at = _now()
    db.commit()
    db.refresh(ap)
    create_audit_event(db, run_id=ap.run_id, event_type="APPROVAL_GRANTED", status="success", safe_metadata={"approval_id": ap.id, "approver": approver, "draft_id": ap.draft_id})
    log.info("Approval granted", extra={"extra_data": {"approval_id": ap.id, "run_id": ap.run_id, "approver": approver}})
    return ap

def reject(db: Session, *, approval_id: int, approver: str, reason: Optional[str] = None) -> Approval:
    ap = db.query(Approval).filter(Approval.id == approval_id).first()
    if not ap:
        raise ApprovalError("Approval not found")
    if ap.status != "pending":
        raise ApprovalError(f"Cannot reject — approval status is {ap.status}")
    ap.status = "rejected"
    ap.approver = approver
    ap.reason = reason
    ap.decided_at = _now()
    run = db.query(AgentRun).filter(AgentRun.run_id == ap.run_id).first()
    if run:
        run.status = "rejected"
        run.updated_at = _now()
    db.commit()
    db.refresh(ap)
    create_audit_event(db, run_id=ap.run_id, event_type="APPROVAL_REJECTED", status="success", safe_metadata={"approval_id": ap.id, "approver": approver, "draft_id": ap.draft_id})
    log.info("Approval rejected", extra={"extra_data": {"approval_id": ap.id, "run_id": ap.run_id}})
    return ap

def invalidate_on_draft_change(db: Session, draft_id: int):
    """Changing a draft invalidates previous approval — must be called on draft update."""
    ap = db.query(Approval).filter(Approval.draft_id == draft_id).first()
    if ap and ap.status in ("pending", "approved"):
        old_status = ap.status
        ap.status = "rejected"
        ap.reason = (ap.reason or "") + " | auto-invalidated: draft modified"
        ap.decided_at = _now()
        db.commit()
        create_audit_event(db, run_id=ap.run_id, event_type="APPROVAL_REJECTED", status="success", safe_metadata={"approval_id": ap.id, "reason": "draft_modified", "old_status": old_status, "draft_id": draft_id})
        # Revert run to drafted
        run = db.query(AgentRun).filter(AgentRun.run_id == ap.run_id).first()
        if run:
            run.status = "drafted"
            run.updated_at = _now()
            db.commit()
        log.info("Approval invalidated due to draft change", extra={"extra_data": {"draft_id": draft_id, "approval_id": ap.id, "run_id": ap.run_id}})
        return ap
    return None

def can_send(db: Session, *, run_id: str, draft_id: int) -> bool:
    """Gate check — no bypass. Must be exact draft/run, approved, not changed."""
    from app.core.config import get_settings
    s = get_settings()
    if not s.approval_required:
        # Default must keep approval enabled; if disabled explicitly, log warning
        log.warning("Approval gate disabled via config — bypass allowed but risky", extra={"extra_data": {"run_id": run_id}})
        return True
    ap = db.query(Approval).filter(Approval.run_id == run_id, Approval.draft_id == draft_id).first()
    if not ap:
        return False
    if ap.status != "approved":
        return False
    draft = db.query(DraftReply).filter(DraftReply.id == draft_id).first()
    if not draft or draft.run_id != run_id:
        return False
    # Draft must not have changed after approval
    if draft.updated_at and ap.decided_at and draft.updated_at > ap.decided_at:
        return False
    # Approval must belong to exact run/draft, not reused
    if ap.run_id != run_id or ap.draft_id != draft_id:
        return False
    return True

def get_approval(db: Session, approval_id: int) -> Optional[Approval]:
    return db.query(Approval).filter(Approval.id == approval_id).first()

def get_approval_for_draft(db: Session, draft_id: int) -> Optional[Approval]:
    return db.query(Approval).filter(Approval.draft_id == draft_id).first()

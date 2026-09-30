import json
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.db.models import EmailMessage, DraftReply, Approval, ThreadState

# Repository/service functions — no DB logic directly in route handlers (per spec)

def create_email_message(db: Session, *, sender: str, subject: Optional[str] = None, body_text: Optional[str] = None, gmail_message_id: Optional[str] = None, thread_id: Optional[str] = None, recipients: Optional[list] = None) -> EmailMessage:
    rec_str = json.dumps(recipients) if recipients else None
    snippet = (body_text[:200] if body_text else None)
    msg = EmailMessage(
        sender=sender, subject=subject, body_text=body_text, body_snippet=snippet,
        gmail_message_id=gmail_message_id, thread_id=thread_id, recipients=rec_str,
        received_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg

def get_email_by_gmail_id(db: Session, gmail_message_id: str) -> Optional[EmailMessage]:
    return db.query(EmailMessage).filter(EmailMessage.gmail_message_id == gmail_message_id).first()

def create_draft(db: Session, *, run_id: str, body: str, subject: Optional[str] = None, email_id: Optional[int] = None, language: str = "en", tone: str = "professional", confidence: Optional[float] = None, citations: Optional[list] = None, risk_flags: Optional[Dict[str, Any]] = None) -> DraftReply:
    draft = DraftReply(
        run_id=run_id, email_id=email_id, subject=subject, body=body,
        language=language, tone=tone, confidence=confidence,
        citations=json.dumps(citations) if citations else None,
        risk_flags=json.dumps(risk_flags) if risk_flags else None,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft

def get_draft(db: Session, draft_id: int) -> Optional[DraftReply]:
    return db.query(DraftReply).filter(DraftReply.id == draft_id).first()

def create_approval(db: Session, *, run_id: str, draft_id: int, status: str = "pending", approver: Optional[str] = None, reason: Optional[str] = None) -> Approval:
    approval = Approval(run_id=run_id, draft_id=draft_id, status=status, approver=approver, reason=reason)
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval

def decide_approval(db: Session, approval_id: int, new_status: str, approver: Optional[str] = None, reason: Optional[str] = None) -> Approval:
    ap = db.query(Approval).filter(Approval.id == approval_id).first()
    if not ap:
        raise ValueError("Approval not found")
    ap.status = new_status
    ap.approver = approver
    ap.reason = reason
    ap.decided_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ap)
    return ap

def upsert_thread_state(db: Session, *, thread_id: str, last_run_id: Optional[str] = None, last_email_id: Optional[int] = None) -> ThreadState:
    existing = db.query(ThreadState).filter(ThreadState.thread_id == thread_id).first()
    if existing:
        if last_run_id: existing.last_run_id = last_run_id
        if last_email_id: existing.last_email_id = last_email_id
        existing.message_count = (existing.message_count or 0) + 1
        existing.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(existing)
        return existing
    ts = ThreadState(thread_id=thread_id, last_run_id=last_run_id, last_email_id=last_email_id, message_count=1)
    db.add(ts)
    db.commit()
    db.refresh(ts)
    return ts

def get_thread_state(db: Session, thread_id: str) -> Optional[ThreadState]:
    return db.query(ThreadState).filter(ThreadState.thread_id == thread_id).first()

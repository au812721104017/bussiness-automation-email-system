import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session
from app.db.models import AgentRun, EmailMessage, DraftReply, Approval, OutboundMessage
from app.schemas.email import NormalizedEmail
from app.services.run_service import generate_run_id, create_run, update_run_status, get_run
from app.services.audit_service import create_audit_event
from app.services.classifier import ClassifierService, get_classifier
from app.rag.retrieval import RetrievalService
from app.services.reply_generator import ReplyGeneratorService
from app.db.repository import create_email_message, create_draft as repo_create_draft, upsert_thread_state
from app.services.approval_service import request_approval, can_send
from app.services.gmail_send_service import GmailSendService
from app.services.gmail_service import GmailService
from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

STATES = ["received", "normalized", "classified", "retrieved", "drafted", "pending_approval", "approved", "sendable", "sent", "failed", "skipped"]

class OrchestrationError(Exception):
    pass

class AgentOrchestrator:
    """Coordinates fetch->normalize->classify->retrieve->draft->approval->send with run_id traceability, idempotency, audit, duplicate prevention."""

    def __init__(self, *, gmail_service: Optional[GmailService] = None, classifier: Optional[ClassifierService] = None, retrieval: Optional[RetrievalService] = None, reply_service: Optional[ReplyGeneratorService] = None, send_service: Optional[GmailSendService] = None):
        self.gmail = gmail_service or GmailService()
        self.classifier = classifier or get_classifier()
        self.retrieval = retrieval or RetrievalService()
        self.reply = reply_service or ReplyGeneratorService()
        self.sender = send_service or GmailSendService()

    def _get_or_create_email(self, db: Session, normalized: NormalizedEmail) -> EmailMessage:
        # Duplicate prevention: if gmail_message_id already exists, return existing and mark duplicate
        existing = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == normalized.message_id).first() if normalized.message_id else None
        if existing:
            log.info("Duplicate email detected — returning existing", extra={"extra_data": {"message_id": normalized.message_id}})
            return existing
        # Also check thread state for dedup? For now store message
        em = create_email_message(db, sender=normalized.sender, subject=normalized.subject, body_text=normalized.body_text, gmail_message_id=normalized.message_id, thread_id=normalized.thread_id, recipients=normalized.recipients)
        if normalized.thread_id:
            upsert_thread_state(db, thread_id=normalized.thread_id, last_email_id=em.id)
        create_audit_event(db, run_id="system", event_type="EMAIL_RECEIVED", status="success", safe_metadata={"message_id": normalized.message_id, "thread_id": normalized.thread_id, "sender_domain": normalized.sender_email.split("@")[-1] if normalized.sender_email and "@" in normalized.sender_email else None})
        return em

    def run_for_email(self, db: Session, *, normalized: NormalizedEmail, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Full workflow until pending_approval. Idempotent: if run already exists for this message_id, return it."""
        # Idempotency: check if AgentRun already exists for this gmail_message_id (via EmailMessage)
        existing_email = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == normalized.message_id).first() if normalized.message_id else None
        if existing_email:
            prior_run = db.query(AgentRun).filter(AgentRun.email_id == existing_email.id).order_by(AgentRun.created_at.desc()).first()
            prior_draft = db.query(DraftReply).filter(DraftReply.run_id == prior_run.run_id).first() if prior_run else None
            if prior_run and prior_draft:
                # Check if not modified — idempotent return
                log.info("Idempotent skip — existing run/draft for message", extra={"extra_data": {"run_id": prior_run.run_id, "message_id": normalized.message_id}})
                create_audit_event(db, run_id=prior_run.run_id, event_type="RUN_SKIPPED_DUPLICATE", status="success", safe_metadata={"message_id": normalized.message_id})
                return {"run_id": prior_run.run_id, "draft_id": prior_draft.id, "status": prior_run.status, "duplicate": True, "approval": db.query(Approval).filter(Approval.draft_id == prior_draft.id).first()}

        # Create new run with unique run_id
        rid = run_id or generate_run_id()
        email = self._get_or_create_email(db, normalized)
        run = create_run(db, run_id=rid, status="started", email_id=email.id, metadata={"message_id": normalized.message_id, "thread_id": normalized.thread_id})
        # Re-attach email_id if created after
        if run.email_id != email.id:
            run.email_id = email.id
            db.commit()
        upsert_thread_state(db, thread_id=normalized.thread_id or "no_thread", last_run_id=rid, last_email_id=email.id)

        # State: normalized
        create_audit_event(db, run_id=rid, event_type="EMAIL_NORMALIZED", status="success", safe_metadata={"message_id": normalized.message_id})
        update_run_status(db, rid, "normalized")

        # Classify
        try:
            classification = self.classifier.classify(normalized)
            create_audit_event(db, run_id=rid, event_type="EMAIL_CLASSIFIED", status="success", safe_metadata={"intent": classification.intent, "confidence": classification.confidence, "risk": classification.risk_level})
            update_run_status(db, rid, "classified", safe_metadata={"intent": classification.intent})
        except Exception as e:
            create_audit_event(db, run_id=rid, event_type="EMAIL_CLASSIFIED", status="failed", safe_metadata={"error": type(e).__name__})
            update_run_status(db, rid, "failed", safe_metadata={"error": type(e).__name__})
            raise OrchestrationError(f"Classification failed: {e}") from e

        # Retrieve trusted knowledge
        rag_context = None
        rag_sources = []
        try:
            query = f"{normalized.subject or ''} {normalized.body_text or ''}".strip()[:2000] or "general"
            ctx = self.retrieval.retrieve_as_context(query)
            rag_context = ctx["context"][:3000] if ctx["context"] else None
            rag_sources = [s.get("source") for s in ctx["sources"]]
            create_audit_event(db, run_id=rid, event_type="KNOWLEDGE_RETRIEVED", status="success", safe_metadata={"sources": rag_sources, "top_k": len(rag_sources)})
            update_run_status(db, rid, "retrieved")
        except Exception as e:
            create_audit_event(db, run_id=rid, event_type="KNOWLEDGE_RETRIEVED", status="failed", safe_metadata={"error": type(e).__name__})
            rag_context = None

        # Generate draft
        try:
            business_ctx = {"retrieved_knowledge": rag_context, "sources": rag_sources} if rag_context else None
            reply = self.reply.generate(email=normalized, classification=classification, business_context=business_ctx)
            # Ensure citations traceability: if rag sources and no citations, add
            if rag_sources and not reply.citations:
                reply.citations = rag_sources[:3]
                reply.safety_flags["rag_sources"] = rag_sources
            create_audit_event(db, run_id=rid, event_type="DRAFT_GENERATED", status="success", safe_metadata={"confidence": reply.confidence, "risk": reply.risk_level, "citations": reply.citations})
        except Exception as e:
            create_audit_event(db, run_id=rid, event_type="DRAFT_GENERATED", status="failed", safe_metadata={"error": type(e).__name__})
            update_run_status(db, rid, "failed")
            raise

        # Save draft
        draft = repo_create_draft(db, run_id=rid, email_id=email.id, body=reply.body, subject=reply.subject or f"Re: {normalized.subject or ''}", language=reply.language, tone=reply.tone, confidence=reply.confidence, citations=reply.citations, risk_flags=reply.safety_flags)
        create_audit_event(db, run_id=rid, event_type="DRAFT_CREATED", status="success", safe_metadata={"draft_id": draft.id, "confidence": reply.confidence})
        update_run_status(db, rid, "drafted")

        # Auto-send only when the approval gate is explicitly disabled via config.
        # Reuses the existing send service, which re-checks can_send() internally.
        if not get_settings().approval_required:
            # Auto-reply must always go to the original sender (From header).
            # Never reply to normalized.recipients — for inbound mail that is the To
            # header, i.e. this agent's own account, which would loop mail back to us.
            auto_to = [normalized.sender_email] if normalized.sender_email else []
            if auto_to:
                outbound = self.sender.send_approved_draft(db, draft_id=draft.id, run_id=rid, to=auto_to, thread_id=normalized.thread_id)
                log.info("Auto-sent draft (approval_required=false)", extra={"extra_data": {"run_id": rid, "draft_id": draft.id, "outbound_id": outbound.id, "intent": classification.intent}})
                return {"run_id": rid, "draft_id": draft.id, "outbound_id": outbound.id, "status": "sent", "classification": classification.dict(), "reply": reply.dict(), "duplicate": False}

        # Request approval (PENDING_APPROVAL)
        try:
            approval = request_approval(db, run_id=rid, draft_id=draft.id)
            # request_approval already audits and sets run to pending_approval
        except Exception as e:
            create_audit_event(db, run_id=rid, event_type="APPROVAL_REQUESTED", status="failed", safe_metadata={"error": str(e)[:200]})
            raise

        log.info("Orchestration completed to pending_approval", extra={"extra_data": {"run_id": rid, "draft_id": draft.id, "intent": classification.intent}})
        return {"run_id": rid, "draft_id": draft.id, "approval_id": approval.id if 'approval' in locals() else None, "status": "pending_approval", "classification": classification.dict(), "reply": reply.dict(), "duplicate": False}

    def run_for_gmail_message_id(self, db: Session, *, gmail_message_id: str, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Fetch raw Gmail message, normalize, then run_for_email. Idempotent."""
        raw = self.gmail.get_message(gmail_message_id)
        normalized = self.gmail.normalize_message(raw)
        return self.run_for_email(db, normalized=normalized, run_id=run_id)

    def send_if_approved(self, db: Session, *, run_id: str, draft_id: int, to: List[str], thread_id: Optional[str] = None) -> OutboundMessage:
        """Send only after valid approval — gate enforced, records provider ID, audit."""
        if not can_send(db, run_id=run_id, draft_id=draft_id):
            raise PermissionError("Send blocked — no valid approved approval for exact draft/run or draft modified")
        outbound = self.sender.send_approved_draft(db, draft_id=draft_id, run_id=run_id, to=to, thread_id=thread_id)
        # send service already audits and updates run to sent
        return outbound

    def get_run_trace(self, db: Session, run_id: str) -> Dict[str, Any]:
        """Full lifecycle trace by run_id — for auditability."""
        run = get_run(db, run_id)
        if not run:
            raise ValueError("Run not found")
        from app.db.models import AuditEvent, DraftReply, Approval, OutboundMessage
        audits = db.query(AuditEvent).filter(AuditEvent.run_id == run_id).order_by(AuditEvent.created_at.asc()).all()
        drafts = db.query(DraftReply).filter(DraftReply.run_id == run_id).all()
        approvals = db.query(Approval).filter(Approval.run_id == run_id).all()
        outbounds = db.query(OutboundMessage).filter(OutboundMessage.run_id == run_id).all()
        return {
            "run": {"run_id": run.run_id, "status": run.status, "email_id": run.email_id, "created_at": run.created_at.isoformat() if run.created_at else None, "updated_at": run.updated_at.isoformat() if run.updated_at else None},
            "audits": [{"event_type": a.event_type, "status": a.status, "created_at": a.created_at.isoformat() if a.created_at else None, "safe_metadata": a.safe_metadata} for a in audits],
            "drafts": [{"id": d.id, "subject": d.subject, "confidence": d.confidence} for d in drafts],
            "approvals": [{"id": a.id, "status": a.status, "draft_id": a.draft_id} for a in approvals],
            "outbounds": [{"id": o.id, "status": o.status, "provider_message_id": o.provider_message_id} for o in outbounds],
        }

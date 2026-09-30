from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, Dict, Any
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.schemas.email import NormalizedEmail
from app.schemas.classification import ClassificationResult
from app.schemas.reply import ReplyGenerationResult
from app.services.reply_generator import ReplyGeneratorService
from app.services.classifier import ClassifierService, get_classifier
from app.db.repository import create_draft as repo_create_draft
from app.services.run_service import generate_run_id, create_run
from app.services.audit_service import create_audit_event
from app.db.models import DraftReply
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/drafts", tags=["drafts"])

class GenerateDraftRequest(BaseModel):
    message_id: str = "test"
    sender: str = "user@example.com"
    subject: Optional[str] = None
    body_text: Optional[str] = None
    snippet: Optional[str] = None
    business_context: Optional[Dict[str, Any]] = None
    conversation_context: Optional[str] = None
    # Optional pre-computed classification to reuse; if not provided, classifier runs
    classification: Optional[ClassificationResult] = None

def get_reply_service() -> ReplyGeneratorService:
    return ReplyGeneratorService()

@router.post("/generate", response_model=ReplyGenerationResult, summary="Generate reply draft (stored with run_id, not sent)")
async def generate_draft(req: GenerateDraftRequest, svc: ReplyGeneratorService = Depends(get_reply_service), db: Session = Depends(get_db)):
    try:
        email = NormalizedEmail(message_id=req.message_id, sender=req.sender, subject=req.subject, body_text=req.body_text, snippet=req.snippet)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Classify if not provided
    classification = req.classification
    if not classification:
        classification = get_classifier().classify(email)
    # RAG: if no explicit business_context, retrieve approved knowledge (Phase 6) for traceability
    business_ctx = req.business_context
    rag_sources = []
    if business_ctx is None:
        try:
            from app.rag.retrieval import RetrievalService
            query = f"{req.subject or ''} {req.body_text or ''}".strip() or "general"
            retrieval = RetrievalService()
            ctx = retrieval.retrieve_as_context(query)
            if ctx["context"]:
                business_ctx = {"retrieved_knowledge": ctx["context"][:3000], "sources": [s.get("source") for s in ctx["sources"]]}
                rag_sources = [s.get("source") for s in ctx["sources"]]
                # Preserve source metadata for citation traceability
                log.info("RAG context injected", extra={"extra_data": {"sources": rag_sources, "query_len": len(query)}})
        except Exception as e:
            log.warning(f"RAG retrieval skipped: {type(e).__name__}")
            business_ctx = None
    result = svc.generate(email=email, classification=classification, business_context=business_ctx, conversation_context=req.conversation_context)
    # If RAG sources exist and LLM did not cite, auto-add citations for traceability
    if rag_sources and not result.citations:
        result.citations = rag_sources[:3]
        result.safety_flags["rag_sources"] = rag_sources

    # Persist draft with run_id (never send)
    try:
        run_id = generate_run_id()
        # Create run and audit for traceability
        run = create_run(db, run_id=run_id, status="drafted", metadata={"intent": classification.intent, "message_id": email.message_id})
        # Store draft
        draft = repo_create_draft(db, run_id=run_id, body=result.body, subject=result.subject, language=result.language, tone=result.tone, confidence=result.confidence, citations=result.citations, risk_flags=result.safety_flags)
        create_audit_event(db, run_id=run_id, event_type="DRAFT_CREATED", status="success", safe_metadata={"draft_id": draft.id, "intent": classification.intent, "confidence": result.confidence, "risk": result.risk_level, "needs_review": result.safety_flags.get("needs_review")})
        log.info("Draft stored", extra={"extra_data": {"run_id": run_id, "draft_id": draft.id, "intent": classification.intent}})
    except Exception as e:
        log.error(f"Draft persist failed: {type(e).__name__}")
        # Still return generated result even if persist fails (graceful)
        pass
    return result

@router.get("", summary="List recent drafts")
async def list_drafts(limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db)):
    drafts = db.query(DraftReply).order_by(DraftReply.created_at.desc()).limit(limit).all()
    return {"drafts": [{"id": d.id, "run_id": d.run_id, "subject": d.subject, "confidence": d.confidence, "created_at": d.created_at.isoformat() if d.created_at else None} for d in drafts]}

@router.get("/{draft_id}", summary="Get draft by id (review)")
async def get_draft(draft_id: int, db: Session = Depends(get_db)):
    d = db.query(DraftReply).filter(DraftReply.id == draft_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Draft not found")
    # Include approval status for human-in-the-loop review (safe — no tokens)
    from app.db.models import Approval
    ap = db.query(Approval).filter(Approval.draft_id == draft_id).first()
    return {"id": d.id, "run_id": d.run_id, "email_id": d.email_id, "subject": d.subject, "body": d.body, "language": d.language, "tone": d.tone, "confidence": d.confidence, "citations": d.citations, "risk_flags": d.risk_flags, "created_at": d.created_at.isoformat() if d.created_at else None, "updated_at": d.updated_at.isoformat() if d.updated_at else None, "approval": {"approval_id": ap.id, "status": ap.status, "approver": ap.approver} if ap else None}

@router.put("/{draft_id}", summary="Update draft — invalidates previous approval (Phase 7)")
async def update_draft(draft_id: int, body: Dict[str, Any], db: Session = Depends(get_db)):
    d = db.query(DraftReply).filter(DraftReply.id == draft_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Draft not found")
    # Update allowed fields
    if "body" in body and isinstance(body["body"], str):
        d.body = body["body"]
    if "subject" in body:
        d.subject = body["subject"]
    from datetime import datetime, timezone
    d.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(d)
    # Invalidate approval on change
    from app.services.approval_service import invalidate_on_draft_change
    invalidated = invalidate_on_draft_change(db, draft_id)
    log.info("Draft updated, approval invalidated if existed", extra={"extra_data": {"draft_id": draft_id, "invalidated": bool(invalidated)}})
    return {"id": d.id, "run_id": d.run_id, "subject": d.subject, "body": d.body, "updated_at": d.updated_at.isoformat(), "approval_invalidated": bool(invalidated), "approval": {"status": invalidated.status if invalidated else None}}

@router.get("/{draft_id}/review", summary="Review draft + classification + approval + audit (admin)")
async def review_draft(draft_id: int, db: Session = Depends(get_db)):
    from app.db.models import Approval, AuditEvent, AgentRun
    d = db.query(DraftReply).filter(DraftReply.id == draft_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Draft not found")
    ap = db.query(Approval).filter(Approval.draft_id == draft_id).first()
    run = db.query(AgentRun).filter(AgentRun.run_id == d.run_id).first()
    audits = db.query(AuditEvent).filter(AuditEvent.run_id == d.run_id).order_by(AuditEvent.created_at.asc()).all()
    return {
        "draft": {"id": d.id, "run_id": d.run_id, "subject": d.subject, "body": d.body[:2000], "confidence": d.confidence, "risk_flags": d.risk_flags, "citations": d.citations},
        "run": {"run_id": run.run_id if run else d.run_id, "status": run.status if run else None},
        "approval": {"approval_id": ap.id, "status": ap.status, "approver": ap.approver, "decided_at": ap.decided_at.isoformat() if ap and ap.decided_at else None} if ap else None,
        "audit_trail": [{"event_type": a.event_type, "status": a.status, "created_at": a.created_at.isoformat() if a.created_at else None} for a in audits],
        "can_send": ap.status == "approved" if ap else False,
    }

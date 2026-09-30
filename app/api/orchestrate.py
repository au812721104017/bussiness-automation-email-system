from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models import EmailMessage
from app.schemas.email import NormalizedEmail
from app.services.orchestrator import AgentOrchestrator
from app.services.gmail_service import GmailService
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/orchestrate", tags=["orchestration"])

def get_orch() -> AgentOrchestrator:
    return AgentOrchestrator()

class OrchestrateEmailRequest(BaseModel):
    message_id: str = "test_orch"
    sender: str = "user@example.com"
    subject: Optional[str] = None
    body_text: Optional[str] = None
    snippet: Optional[str] = None
    thread_id: Optional[str] = None

@router.post("/run", summary="Orchestrate: fetch→normalize→classify→retrieve→draft→pending_approval (idempotent, run_id traceable)")
async def orchestrate_run(body: OrchestrateEmailRequest, orch: AgentOrchestrator = Depends(get_orch), db: Session = Depends(get_db)):
    try:
        normalized = NormalizedEmail(message_id=body.message_id, sender=body.sender, subject=body.subject, body_text=body.body_text, snippet=body.snippet, thread_id=body.thread_id, recipients=[])
        result = orch.run_for_email(db, normalized=normalized)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])

@router.post("/run/gmail/{gmail_message_id}", summary="Orchestrate from Gmail message ID (fetch + normalize + full workflow)")
async def orchestrate_gmail(gmail_message_id: str, orch: AgentOrchestrator = Depends(get_orch), db: Session = Depends(get_db)):
    try:
        result = orch.run_for_gmail_message_id(db, gmail_message_id=gmail_message_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])

class SendApprovedRequest(BaseModel):
    run_id: str
    draft_id: int
    to: List[str]
    thread_id: Optional[str] = None

@router.post("/send", summary="Send approved draft via orchestration (gate-checked)")
async def orchestrate_send(body: SendApprovedRequest, orch: AgentOrchestrator = Depends(get_orch), db: Session = Depends(get_db)):
    try:
        out = orch.send_if_approved(db, run_id=body.run_id, draft_id=body.draft_id, to=body.to, thread_id=body.thread_id)
        return {"outbound_id": out.id, "status": out.status, "provider_message_id": out.provider_message_id, "run_id": out.run_id}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])

@router.get("/trace/{run_id}", summary="Full lifecycle trace by run_id")
async def trace(run_id: str, orch: AgentOrchestrator = Depends(get_orch), db: Session = Depends(get_db)):
    try:
        return orch.get_run_trace(db, run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

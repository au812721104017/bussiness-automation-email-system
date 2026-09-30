from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.services.gmail_send_service import GmailSendService
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/send", tags=["send"])

class SendRequest(BaseModel):
    draft_id: int
    run_id: str
    to: List[str]
    thread_id: Optional[str] = None

def get_sender() -> GmailSendService:
    return GmailSendService()

@router.post("", summary="Send approved draft via Gmail (gate-checked — no auto-send without approval)")
async def send_draft(body: SendRequest, svc: GmailSendService = Depends(get_sender), db: Session = Depends(get_db)):
    # Recipients validated inside service; thread supported
    try:
        outbound = svc.send_approved_draft(db, draft_id=body.draft_id, run_id=body.run_id, to=body.to, thread_id=body.thread_id)
        return {"outbound_id": outbound.id, "status": outbound.status, "provider_message_id": outbound.provider_message_id, "run_id": outbound.run_id, "draft_id": outbound.draft_id, "thread_id": outbound.thread_id}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/outbound/{outbound_id}", summary="Get send status")
async def get_outbound(outbound_id: int, db: Session = Depends(get_db)):
    from app.db.models import OutboundMessage
    m = db.query(OutboundMessage).filter(OutboundMessage.id == outbound_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Outbound not found")
    return {"outbound_id": m.id, "draft_id": m.draft_id, "run_id": m.run_id, "status": m.status, "provider_message_id": m.provider_message_id, "thread_id": m.thread_id, "sent_at": m.sent_at.isoformat() if m.sent_at else None, "error": m.error[:200] if m.error else None}

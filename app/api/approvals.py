from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models import Approval, DraftReply, AgentRun
from app.services.approval_service import request_approval, approve, reject, can_send, get_approval, ApprovalError, invalidate_on_draft_change
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/approvals", tags=["approvals"])

class RequestApprovalBody(BaseModel):
    run_id: str
    draft_id: int
    reason: Optional[str] = None

class DecisionBody(BaseModel):
    approver: str
    reason: Optional[str] = None

@router.post("/request", summary="Request approval for draft (PENDING_APPROVAL)")
async def req_approval(body: RequestApprovalBody, db: Session = Depends(get_db)):
    try:
        ap = request_approval(db, run_id=body.run_id, draft_id=body.draft_id, reason=body.reason)
        return {"approval_id": ap.id, "run_id": ap.run_id, "draft_id": ap.draft_id, "status": ap.status, "created_at": ap.created_at.isoformat() if ap.created_at else None}
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{approval_id}/approve", summary="Approve draft (explicit)")
async def approve_endpoint(approval_id: int, body: DecisionBody, db: Session = Depends(get_db)):
    try:
        ap = approve(db, approval_id=approval_id, approver=body.approver, reason=body.reason)
        return {"approval_id": ap.id, "run_id": ap.run_id, "draft_id": ap.draft_id, "status": ap.status, "approver": ap.approver, "decided_at": ap.decided_at.isoformat() if ap.decided_at else None}
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{approval_id}/reject", summary="Reject draft")
async def reject_endpoint(approval_id: int, body: DecisionBody, db: Session = Depends(get_db)):
    try:
        ap = reject(db, approval_id=approval_id, approver=body.approver, reason=body.reason)
        return {"approval_id": ap.id, "status": ap.status, "approver": ap.approver}
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/{approval_id}", summary="Get approval status (audit traceable)")
async def get_one(approval_id: int, db: Session = Depends(get_db)):
    ap = get_approval(db, approval_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Approval not found")
    return {"approval_id": ap.id, "run_id": ap.run_id, "draft_id": ap.draft_id, "status": ap.status, "approver": ap.approver, "reason": ap.reason, "created_at": ap.created_at.isoformat() if ap.created_at else None, "decided_at": ap.decided_at.isoformat() if ap.decided_at else None}

@router.get("", summary="List approvals for run")
async def list_for_run(run_id: str = Query(...), db: Session = Depends(get_db)):
    aps = db.query(Approval).filter(Approval.run_id == run_id).all()
    return {"approvals": [{"approval_id": a.id, "run_id": a.run_id, "draft_id": a.draft_id, "status": a.status} for a in aps]}

@router.get("/draft/{draft_id}", summary="Get approval for draft")
async def for_draft(draft_id: int, db: Session = Depends(get_db)):
    ap = db.query(Approval).filter(Approval.draft_id == draft_id).first()
    if not ap:
        raise HTTPException(status_code=404, detail="No approval for draft")
    return {"approval_id": ap.id, "run_id": ap.run_id, "draft_id": ap.draft_id, "status": ap.status}

@router.get("/gate/can-send", summary="Check if send is allowed (gate — must be approved exact draft/run, not reused)")
async def gate_check(run_id: str, draft_id: int, db: Session = Depends(get_db)):
    allowed = can_send(db, run_id=run_id, draft_id=draft_id)
    return {"run_id": run_id, "draft_id": draft_id, "can_send": allowed, "gate": "approval_required"}

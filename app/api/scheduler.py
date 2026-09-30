from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.jobs.scheduler import get_scheduler

router = APIRouter(prefix="/scheduler", tags=["scheduler"])

@router.get("/status", summary="Scheduler status (enabled, interval, running, gate)")
async def status():
    return get_scheduler().status()

@router.post("/start", summary="Start background polling (graceful, interval-based)")
async def start():
    svc = get_scheduler()
    ok = svc.start()
    if not ok and not svc.enabled:
        raise HTTPException(status_code=400, detail="Scheduler not enabled — set SCHEDULER_ENABLED=true in .env")
    if not ok:
        raise HTTPException(status_code=409, detail="Scheduler already running")
    return {"started": True, "status": svc.status()}

@router.post("/stop", summary="Stop polling gracefully")
async def stop():
    svc = get_scheduler()
    ok = svc.stop()
    if not ok:
        raise HTTPException(status_code=400, detail="Scheduler not running")
    return {"stopped": True, "status": svc.status()}

@router.post("/trigger", summary="Trigger one poll synchronously (audit, dedup, no bypass)")
async def trigger(max_results: int = Query(5, ge=1, le=20), db: Session = Depends(get_db)):
    svc = get_scheduler()
    stats = svc.trigger_once(db)
    return stats

@router.get("/polls", summary="Recent scheduler poll audits")
async def recent_polls(limit: int = Query(5, ge=1, le=20), db: Session = Depends(get_db)):
    from app.db.models import AuditEvent
    audits = db.query(AuditEvent).filter(AuditEvent.event_type.like("SCHEDULER%")).order_by(AuditEvent.created_at.desc()).limit(limit).all()
    return {"polls": [{"event_type": a.event_type, "status": a.status, "run_id": a.run_id, "created_at": a.created_at.isoformat() if a.created_at else None, "safe_metadata": a.safe_metadata} for a in audits]}

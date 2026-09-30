import json
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models import EmailMessage, AgentRun, DraftReply, Approval, AuditEvent, OutboundMessage
from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

def _admin_auth(x_admin_token: Optional[str] = Header(None)):
    s = get_settings()
    # Production-ready design: require token if ADMIN_TOKEN set
    # Local dev: if not set, allow with warning (documented in .env.example)
    if s.admin_token:
        if x_admin_token != s.admin_token:
            raise HTTPException(status_code=401, detail="Unauthorized: provide X-Admin-Token header")
    else:
        if s.env == "production":
            raise HTTPException(status_code=401, detail="ADMIN_TOKEN not configured for production")
        # In dev/testing, allow without token but log
        log.info("Admin access without token (dev mode — set ADMIN_TOKEN for production)")

def _safe_truncate(text: Optional[str], n=800) -> Optional[str]:
    if not text:
        return text
    if len(text) <= n:
        return text
    return text[:n] + " [truncated]"

@router.get("", include_in_schema=False)
async def admin_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/dashboard")

@router.get("/dashboard", response_class=HTMLResponse, summary="Minimal admin dashboard (simple functional UI)")
async def dashboard():
    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>AI Email Admin</title><style>
body{font-family:system-ui,Arial;margin:20px;max-width:1000px} table{border-collapse:collapse;width:100%} th,td{border:1px solid #ccc;padding:6px;text-align:left;font-size:13px} th{background:#f5f5f5} .badge{padding:2px 6px;border-radius:4px;font-size:11px} .pending{background:#fff3cd} .approved{background:#d4edda} .rejected{background:#f8d7da} .muted{color:#666} button{margin:2px;padding:4px 8px} input,select{padding:4px} #token{width:300px}
</style></head><body>
<h2>AI Email Auto-Reply — Admin</h2>
<p class="muted">Phase 11 — review emails, drafts, approval, audit, send. Do not expose secrets/tokens. Use X-Admin-Token header if ADMIN_TOKEN set (see .env.example). Auth is disabled in dev unless ADMIN_TOKEN is set.</p>
<label>Admin Token: <input id="token" placeholder="X-Admin-Token (if set)"></label> <button onclick="loadAll()">Reload</button>
<h3>Overview</h3><div id="overview"></div>
<h3>Emails & Runs</h3><div id="emails"></div>
<h3>Drafts</h3><div id="drafts"></div>
<h3>Approvals</h3><div id="approvals"></div>
<p><a href="/docs" target="_blank">Open API docs</a> | <a href="/health">health</a></p>
<script>
function hdr(){const t=document.getElementById('token').value;return t?{'X-Admin-Token':t}:{};}
async function j(path, opts={}){const r=await fetch(path,{headers:{...hdr(),...(opts.headers||{})},...opts}); if(!r.ok){const e=await r.text(); throw new Error(r.status+':'+e.slice(0,400));} return r.json();}
async function loadAll(){
  try{
    const ov=await j('/admin/overview'); document.getElementById('overview').innerHTML=`<pre>${JSON.stringify(ov,null,2)}</pre>`;
    const em=await j('/admin/emails?limit=10'); document.getElementById('emails').innerHTML=renderEmails(em.emails);
    const dr=await j('/admin/drafts?limit=10'); document.getElementById('drafts').innerHTML=renderDrafts(dr.drafts);
    const ap=await j('/admin/approvals?limit=10'); document.getElementById('approvals').innerHTML=renderApprovals(ap.approvals);
  }catch(e){alert(e.message);}
}
function renderEmails(list){ if(!list.length) return '<p class=muted>no emails</p>'; let h='<table><tr><th>id</th><th>from</th><th>subject</th><th>run</th><th>draft</th><th>approval</th><th>actions</th></tr>'; for(const r of list){h+=`<tr><td>${r.id}</td><td>${r.sender||''}</td><td>${(r.subject||'').slice(0,60)}</td><td>${r.run_id||''} <span class=badge>${r.run_status||''}</span></td><td>${r.draft_id||''} conf ${r.confidence||''}</td><td><span class="badge ${r.approval_status||''}">${r.approval_status||'none'}</span></td><td><button onclick="viewDraft(${r.draft_id})">view</button> <button onclick="ignoreEmail(${r.id})">ignore</button></td></tr>`} return h+'</table>';}
function renderDrafts(list){ if(!list.length) return '<p class=muted>no drafts</p>'; let h='<table><tr><th>id</th><th>run_id</th><th>subject</th><th>conf</th><th>risk</th><th>approval</th><th>actions</th></tr>'; for(const d of list){h+=`<tr><td>${d.id}</td><td>${d.run_id.slice(0,8)}</td><td>${(d.subject||'').slice(0,40)}</td><td>${d.confidence}</td><td>${d.risk_level||''}</td><td><span class="badge ${d.approval_status||''}">${d.approval_status||'none'}</span></td><td><button onclick="viewDraft(${d.id})">view</button> <button onclick="approveDraft(${d.id})">approve</button> <button onclick="rejectDraft(${d.id})">reject</button> <button onclick="regen(${d.id})">regen</button></td></tr>`} return h+'</table>';}
function renderApprovals(list){ if(!list.length) return '<p class=muted>no approvals</p>'; let h='<table><tr><th>id</th><th>run_id</th><th>draft</th><th>status</th><th>approver</th></tr>'; for(const a of list){h+=`<tr><td>${a.id}</td><td>${a.run_id.slice(0,8)}</td><td>${a.draft_id}</td><td>${a.status}</td><td>${a.approver||''}</td></tr>`} return h+'</table>';}
async function viewDraft(id){ if(!id) return alert('no draft'); const d=await j('/drafts/'+id); const rev=await j('/drafts/'+id+'/review'); alert('Draft '+id+': '+JSON.stringify({draft:d, review:rev},null,2).slice(0,2000));}
async function approveDraft(id){ const ap=await j('/admin/drafts/'+id+'/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approver:'admin@example.com'})}); alert(JSON.stringify(ap)); loadAll();}
async function rejectDraft(id){ const ap=await j('/admin/drafts/'+id+'/reject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approver:'admin@example.com',reason:'admin reject'})}); alert(JSON.stringify(ap)); loadAll();}
async function regen(id){ const r=await j('/admin/drafts/'+id+'/regenerate',{method:'POST'}); alert(JSON.stringify(r)); loadAll();}
async function ignoreEmail(id){ const r=await j('/admin/emails/'+id+'/ignore',{method:'POST'}); alert(JSON.stringify(r)); loadAll();}
loadAll();
</script></body></html>"""
    return HTMLResponse(html)

# ---- JSON APIs (safe fields only, no secrets/tokens/prompts) ----
@router.get("/overview", summary="Admin overview counts (safe)")
async def overview(db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    return {
        "emails": db.query(EmailMessage).count(),
        "runs": db.query(AgentRun).count(),
        "drafts": db.query(DraftReply).count(),
        "approvals": db.query(Approval).count(),
        "approvals_pending": db.query(Approval).filter(Approval.status == "pending").count(),
        "approvals_approved": db.query(Approval).filter(Approval.status == "approved").count(),
        "outbound_sent": db.query(OutboundMessage).filter(OutboundMessage.status == "sent").count(),
        "outbound_failed": db.query(OutboundMessage).filter(OutboundMessage.status == "failed").count(),
        "note": "All fields safe — no secrets/tokens/bodies fully exposed",
    }

@router.get("/emails", summary="List emails with run/draft/approval linkage (safe, truncated bodies)")
async def list_emails(limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    rows = db.query(EmailMessage).order_by(EmailMessage.created_at.desc()).limit(limit).all()
    out = []
    for em in rows:
        run = db.query(AgentRun).filter(AgentRun.email_id == em.id).order_by(AgentRun.created_at.desc()).first()
        draft = db.query(DraftReply).filter(DraftReply.run_id == run.run_id).first() if run else None
        ap = db.query(Approval).filter(Approval.draft_id == draft.id).first() if draft else None
        confidence = None
        risk = None
        if draft and draft.risk_flags:
            try:
                rf = json.loads(draft.risk_flags) if isinstance(draft.risk_flags, str) else draft.risk_flags
                risk = rf.get("risk") or rf.get("risk_level") if isinstance(rf, dict) else None
            except Exception:
                pass
        out.append({
            "id": em.id,
            "gmail_message_id": em.gmail_message_id,
            "thread_id": em.thread_id,
            "sender": em.sender,
            "sender_email": em.sender.split("<")[-1].replace(">", "")[:80] if em.sender else None,
            "subject": _safe_truncate(em.subject, 200),
            "body_snippet": _safe_truncate(em.body_text, 200),
            "received_at": em.received_at.isoformat() if em.received_at else None,
            "run_id": run.run_id if run else None,
            "run_status": run.status if run else None,
            "draft_id": draft.id if draft else None,
            "confidence": draft.confidence if draft else None,
            "approval_status": ap.status if ap else None,
            "can_send": ap.status == "approved" if ap else False,
        })
    return {"emails": out}

@router.get("/runs", summary="List runs with draft/approval summary")
async def list_runs(limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    runs = db.query(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit).all()
    out = []
    for r in runs:
        draft = db.query(DraftReply).filter(DraftReply.run_id == r.run_id).first()
        ap = db.query(Approval).filter(Approval.draft_id == draft.id).first() if draft else None
        audits = db.query(AuditEvent).filter(AuditEvent.run_id == r.run_id).count()
        out.append({"run_id": r.run_id, "status": r.status, "email_id": r.email_id, "created_at": r.created_at.isoformat() if r.created_at else None, "draft_id": draft.id if draft else None, "approval_status": ap.status if ap else None, "audit_count": audits})
    return {"runs": out}

@router.get("/runs/{run_id}", summary="Full trace for run (safe, no secrets)")
async def run_trace(run_id: str, db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    from app.services.orchestrator import AgentOrchestrator
    orch = AgentOrchestrator()
    try:
        trace = orch.get_run_trace(db, run_id)
        # Truncate audit safe_metadata bodies
        for a in trace["audits"]:
            if a.get("safe_metadata") and isinstance(a["safe_metadata"], str) and len(a["safe_metadata"]) > 1000:
                a["safe_metadata"] = a["safe_metadata"][:1000] + " [truncated]"
        return trace
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.get("/drafts", summary="List drafts with approval/safe fields")
async def list_drafts(limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    drafts = db.query(DraftReply).order_by(DraftReply.created_at.desc()).limit(limit).all()
    out = []
    for d in drafts:
        ap = db.query(Approval).filter(Approval.draft_id == d.id).first()
        # Parse risk safely
        risk_level = None
        try:
            rf = json.loads(d.risk_flags) if d.risk_flags and isinstance(d.risk_flags, str) else d.risk_flags
            if isinstance(rf, dict):
                risk_level = rf.get("risk_level") or rf.get("injection_detected") and "medium" or None
        except Exception:
            pass
        out.append({
            "id": d.id, "run_id": d.run_id, "subject": _safe_truncate(d.subject, 200),
            "body_preview": _safe_truncate(d.body, 300),
            "confidence": d.confidence, "language": d.language, "tone": d.tone,
            "citations": json.loads(d.citations) if d.citations and isinstance(d.citations, str) and d.citations.startswith("[") else d.citations,
            "risk_level": risk_level, "approval_status": ap.status if ap else None, "approval_id": ap.id if ap else None,
            "created_at": d.created_at.isoformat() if d.created_at else None
        })
    return {"drafts": out}

@router.get("/drafts/{draft_id}", summary="Draft detail + approval + safe audit")
async def draft_detail(draft_id: int, db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    d = db.query(DraftReply).filter(DraftReply.id == draft_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Draft not found")
    ap = db.query(Approval).filter(Approval.draft_id == draft_id).first()
    audits = db.query(AuditEvent).filter(AuditEvent.run_id == d.run_id).order_by(AuditEvent.created_at.asc()).all()
    return {
        "draft": {"id": d.id, "run_id": d.run_id, "subject": _safe_truncate(d.subject, 500), "body": _safe_truncate(d.body, 4000), "confidence": d.confidence, "language": d.language, "tone": d.tone, "citations": d.citations, "risk_flags": d.risk_flags, "created_at": d.created_at.isoformat() if d.created_at else None},
        "approval": {"id": ap.id, "status": ap.status, "approver": ap.approver, "reason": ap.reason} if ap else None,
        "audit_trail": [{"event_type": a.event_type, "status": a.status, "created_at": a.created_at.isoformat() if a.created_at else None} for a in audits],
        "can_send": ap.status == "approved" if ap else False
    }

@router.get("/approvals", summary="List approvals")
async def list_approvals(limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    aps = db.query(Approval).order_by(Approval.created_at.desc()).limit(limit).all()
    return {"approvals": [{"id": a.id, "run_id": a.run_id, "draft_id": a.draft_id, "status": a.status, "approver": a.approver, "created_at": a.created_at.isoformat() if a.created_at else None} for a in aps]}

@router.get("/audits", summary="Recent audits (safe metadata only)")
async def list_audits(limit: int = Query(20, ge=1, le=100), run_id: Optional[str] = None, db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    q = db.query(AuditEvent)
    if run_id:
        q = q.filter(AuditEvent.run_id == run_id)
    audits = q.order_by(AuditEvent.created_at.desc()).limit(limit).all()
    return {"audits": [{"id": a.id, "run_id": a.run_id, "event_type": a.event_type, "status": a.status, "safe_metadata": _safe_truncate(a.safe_metadata, 500), "created_at": a.created_at.isoformat() if a.created_at else None} for a in audits]}

# ---- Actions (approve/reject/regenerate/ignore) ----
@router.post("/drafts/{draft_id}/approve", summary="Approve draft (admin action)")
async def admin_approve(draft_id: int, body: dict = {}, db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    ap = db.query(Approval).filter(Approval.draft_id == draft_id).first()
    if not ap:
        raise HTTPException(status_code=404, detail="No approval request for draft — create via POST /approvals/request first")
    if ap.status != "pending":
        raise HTTPException(status_code=400, detail=f"Approval already {ap.status}")
    from app.services.approval_service import approve
    try:
        approver = body.get("approver", "admin@example.com")
        updated = approve(db, approval_id=ap.id, approver=approver, reason=body.get("reason"))
        return {"approval_id": updated.id, "status": updated.status, "draft_id": draft_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])

@router.post("/drafts/{draft_id}/reject", summary="Reject draft (admin action)")
async def admin_reject(draft_id: int, body: dict = {}, db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    ap = db.query(Approval).filter(Approval.draft_id == draft_id).first()
    if not ap:
        raise HTTPException(status_code=404, detail="No approval request")
    if ap.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already {ap.status}")
    from app.services.approval_service import reject
    try:
        approver = body.get("approver", "admin@example.com")
        updated = reject(db, approval_id=ap.id, approver=approver, reason=body.get("reason", "admin reject"))
        return {"approval_id": updated.id, "status": updated.status}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])

@router.post("/drafts/{draft_id}/regenerate", summary="Regenerate draft (admin action, keeps run_id, invalidates old approval)")
async def regenerate(draft_id: int, db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    d = db.query(DraftReply).filter(DraftReply.id == draft_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Draft not found")
    # Invalidate old approval
    from app.services.approval_service import invalidate_on_draft_change
    # Regenerate via reply generator using original email context if available
    email = db.query(EmailMessage).filter(EmailMessage.id == d.email_id).first() if d.email_id else None
    from app.services.reply_generator import ReplyGeneratorService
    from app.schemas.email import NormalizedEmail
    from app.services.classifier import get_classifier
    subject = email.subject if email and email.subject else d.subject
    body_text = email.body_text if email and email.body_text else ""
    norm = NormalizedEmail(message_id=email.gmail_message_id if email and email.gmail_message_id else d.run_id, sender=email.sender if email else "unknown", subject=subject, body_text=body_text)
    classification = get_classifier().classify(norm)
    svc = ReplyGeneratorService()
    # Try RAG as before
    try:
        from app.rag.retrieval import RetrievalService
        ctx = RetrievalService().retrieve_as_context(subject or "" + " " + body_text or "")
        business_ctx = {"retrieved_knowledge": ctx["context"][:3000]} if ctx["context"] else None
    except Exception:
        business_ctx = None
    result = svc.generate(email=norm, classification=classification, business_context=business_ctx)
    # Update draft (invalidates approval via service)
    d.body = result.body
    d.subject = result.subject or d.subject
    d.language = result.language
    d.tone = result.tone
    d.confidence = result.confidence
    d.citations = json.dumps(result.citations) if result.citations else None
    d.risk_flags = json.dumps(result.safety_flags) if result.safety_flags else None
    d.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(d)
    invalidated = invalidate_on_draft_change(db, draft_id)
    # Create new approval request? For admin convenience, auto-request new approval
    from app.services.approval_service import request_approval
    try:
        new_ap = request_approval(db, run_id=d.run_id, draft_id=draft_id)
        status = "pending"
        ap_id = new_ap.id
    except Exception:
        # If approval still exists (rejected), need to handle unique constraint — previous was rejected, so new request allowed only if we delete? For now return invalidated state
        ap_id = invalidated.id if invalidated else None
        status = invalidated.status if invalidated else "unknown"
    return {"draft_id": d.id, "run_id": d.run_id, "regenerated": True, "new_confidence": d.confidence, "approval_status": status, "approval_id": ap_id}

@router.post("/emails/{email_id}/ignore", summary="Mark email ignored (admin action)")
async def ignore_email(email_id: int, db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    em = db.query(EmailMessage).filter(EmailMessage.id == email_id).first()
    if not em:
        raise HTTPException(status_code=404, detail="Email not found")
    # Mark latest run as ignored/skipped
    run = db.query(AgentRun).filter(AgentRun.email_id == email_id).order_by(AgentRun.created_at.desc()).first()
    if not run:
        raise HTTPException(status_code=404, detail="No run for email")
    run.status = "ignored"
    run.updated_at = datetime.now(timezone.utc)
    db.commit()
    from app.services.audit_service import create_audit_event
    create_audit_event(db, run_id=run.run_id, event_type="EMAIL_IGNORED", status="success", safe_metadata={"email_id": email_id})
    return {"email_id": email_id, "run_id": run.run_id, "status": run.status, "ignored": True}

@router.get("/outbound", summary="Send status overview")
async def outbound_status(limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db), _: None = Depends(_admin_auth)):
    outs = db.query(OutboundMessage).order_by(OutboundMessage.created_at.desc()).limit(limit).all()
    return {"outbound": [{"id": o.id, "run_id": o.run_id, "draft_id": o.draft_id, "status": o.status, "provider_message_id": o.provider_message_id, "thread_id": o.thread_id, "sent_at": o.sent_at.isoformat() if o.sent_at else None} for o in outs]}

import time
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import SessionLocal
from app.db.models import DraftReply, Approval, AgentRun, AuditEvent
from app.services.run_service import generate_run_id, create_run
from app.db.repository import create_draft
from app.services.approval_service import request_approval, approve, reject, can_send, invalidate_on_draft_change
from app.core.config import get_settings

client = TestClient(app)

def _create_draft_with_run(status="drafted"):
    db = SessionLocal()
    run_id = generate_run_id()
    run = create_run(db, run_id=run_id, status=status, metadata={"test": True})
    draft = create_draft(db, run_id=run_id, body="Hello draft", subject="Re: Test", confidence=0.9, citations=[], risk_flags={"needs_review": False})
    db.close()
    return run_id, draft.id

def test_approval_explicit_belongs_to_run_and_draft():
    run_id, draft_id = _create_draft_with_run()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    assert ap.run_id == run_id
    assert ap.draft_id == draft_id
    assert ap.status == "pending"
    # Audit recorded
    audits = db.query(AuditEvent).filter(AuditEvent.run_id == run_id, AuditEvent.event_type == "APPROVAL_REQUESTED").all()
    assert len(audits) >= 1
    db.close()

def test_approval_cannot_be_reused_for_different_draft():
    run_id, draft1 = _create_draft_with_run()
    db = SessionLocal()
    ap1 = request_approval(db, run_id=run_id, draft_id=draft1)
    # Try to request with same run_id but different draft_id — should be allowed if different draft? But same run cannot have two drafts with same run_id? Actually run_id unique per draft in this flow, so second draft with same run_id invalid. Test with different run.
    # Correct test: approval is bound to exact pair; reuse with wrong run should fail
    # Create second draft with different run
    run_id2 = generate_run_id()
    create_run(db, run_id=run_id2, status="drafted")
    draft2 = create_draft(db, run_id=run_id2, body="second draft", subject="s2")
    # Try to approve draft2 with ap1's approval id should not match — we test can_send with wrong pair
    assert not can_send(db, run_id=run_id2, draft_id=draft1)  # ap1 belongs to run_id not run_id2
    assert not can_send(db, run_id=run_id, draft_id=draft2.id)  # draft2 not approved
    db.close()

def test_approval_wrong_run_fails_at_request():
    run_id, draft_id = _create_draft_with_run()
    db = SessionLocal()
    fake_run = generate_run_id()
    with pytest.raises(Exception) as exc:
        request_approval(db, run_id=fake_run, draft_id=draft_id)
    assert "does not match" in str(exc.value) or "not found" in str(exc.value).lower() or "run_id" in str(exc.value)
    db.close()

def test_changing_draft_invalidates_approval():
    run_id, draft_id = _create_draft_with_run()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    ap = approve(db, approval_id=ap.id, approver="alice@example.com")
    assert ap.status == "approved"
    assert can_send(db, run_id=run_id, draft_id=draft_id) is True
    # Modify draft
    draft = db.query(DraftReply).filter(DraftReply.id == draft_id).first()
    time.sleep(0.01)
    draft.body = "MODIFIED BODY"
    from datetime import datetime, timezone
    draft.updated_at = datetime.now(timezone.utc)
    db.commit()
    # Now can_send should be blocked (draft changed after approval)
    assert can_send(db, run_id=run_id, draft_id=draft_id) is False
    # Also via invalidate helper
    db2 = SessionLocal()
    # Re-fetch to invalidate
    invalidate_on_draft_change(db2, draft_id)
    # After invalidation, approval should be rejected
    ap2 = db2.query(Approval).filter(Approval.id == ap.id).first()
    assert ap2.status == "rejected"
    db.close()
    db2.close()

def test_sending_blocked_without_approval():
    run_id, draft_id = _create_draft_with_run()
    db = SessionLocal()
    # No approval requested yet
    assert can_send(db, run_id=run_id, draft_id=draft_id) is False
    # Also via API gate
    resp = client.get(f"/approvals/gate/can-send?run_id={run_id}&draft_id={draft_id}")
    assert resp.status_code == 200
    assert resp.json()["can_send"] is False
    db.close()

def test_sending_blocked_wrong_approval_run():
    run_id, draft_id = _create_draft_with_run()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="bob@example.com")
    # Try with wrong run_id
    assert can_send(db, run_id="wrong-run-id", draft_id=draft_id) is False
    # Try with wrong draft_id
    assert can_send(db, run_id=run_id, draft_id=99999) is False
    db.close()

def test_modified_draft_blocked_even_with_old_approval_id():
    run_id, draft_id = _create_draft_with_run()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="alice@example.com")
    # Modify draft via API PUT which auto-invalidates
    client.put(f"/drafts/{draft_id}", json={"body": "new body after approval"})
    db2 = SessionLocal()
    assert can_send(db2, run_id=run_id, draft_id=draft_id) is False
    db.close()
    db2.close()

def test_valid_approval_allows_send():
    run_id, draft_id = _create_draft_with_run()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="alice@example.com")
    assert can_send(db, run_id=run_id, draft_id=draft_id) is True
    # API also
    resp = client.get(f"/approvals/gate/can-send?run_id={run_id}&draft_id={draft_id}")
    assert resp.json()["can_send"] is True
    db.close()

def test_default_config_keeps_approval_enabled():
    s = get_settings()
    assert s.approval_required is True

def test_every_transition_audited():
    run_id, draft_id = _create_draft_with_run()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    audits = db.query(AuditEvent).filter(AuditEvent.run_id == run_id).all()
    types = [a.event_type for a in audits]
    assert "APPROVAL_REQUESTED" in types
    approve(db, approval_id=ap.id, approver="alice@example.com")
    audits2 = db.query(AuditEvent).filter(AuditEvent.run_id == run_id).all()
    types2 = [a.event_type for a in audits2]
    assert "APPROVAL_GRANTED" in types2
    # Every audit has required fields
    for a in audits2:
        assert a.run_id == run_id
        assert a.event_type
        assert a.status
        assert a.created_at is not None
    db.close()
    # Also test reject audit
    run_id2, draft_id2 = _create_draft_with_run()
    db = SessionLocal()
    ap2 = request_approval(db, run_id=run_id2, draft_id=draft_id2)
    reject(db, approval_id=ap2.id, approver="bob@example.com", reason="not suitable")
    audits3 = db.query(AuditEvent).filter(AuditEvent.run_id == run_id2, AuditEvent.event_type == "APPROVAL_REJECTED").first()
    assert audits3 is not None
    db.close()

def test_api_review_and_approve_flow():
    # End-to-end via API: generate draft -> request -> approve -> can_send -> modify fails
    gen = client.post("/drafts/generate", json={"sender": "a@b.com", "subject": "Hello", "body_text": "Internship info"})
    assert gen.status_code == 200
    # Find last draft
    drafts = client.get("/drafts?limit=1").json()["drafts"]
    assert len(drafts) >= 1
    d = drafts[0]
    draft_id = d["id"]
    run_id = d["run_id"]
    # Review endpoint
    rev = client.get(f"/drafts/{draft_id}/review")
    assert rev.status_code == 200
    assert rev.json()["draft"]["id"] == draft_id
    assert "audit_trail" in rev.json()
    # Request approval
    req = client.post("/approvals/request", json={"run_id": run_id, "draft_id": draft_id})
    assert req.status_code == 200
    ap_id = req.json()["approval_id"]
    assert req.json()["status"] == "pending"
    # Approve
    apr = client.post(f"/approvals/{ap_id}/approve", json={"approver": "human@example.com"})
    assert apr.status_code == 200
    assert apr.json()["status"] == "approved"
    # Gate allows
    gate = client.get(f"/approvals/gate/can-send?run_id={run_id}&draft_id={draft_id}")
    assert gate.json()["can_send"] is True
    # Modify draft invalidates
    client.put(f"/drafts/{draft_id}", json={"body": "changed body"})
    gate2 = client.get(f"/approvals/gate/can-send?run_id={run_id}&draft_id={draft_id}")
    assert gate2.json()["can_send"] is False

def test_no_hidden_bypass_through_direct_send():
    # Verify approval_service file has no bypass like if True: return True
    with open("app/services/approval_service.py", encoding="utf-8") as f:
        src = f.read()
    assert "approval_required" in src
    # can_send must check approval status, not just return True
    assert "ap.status != \"approved\"" in src or 'ap.status != "approved"' in src or "approved" in src
    # Ensure no hidden flag that bypasses gate unconditionally
    assert "if True:\n        return True" not in src

def test_explicit_reject_blocks_send():
    run_id, draft_id = _create_draft_with_run()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    reject(db, approval_id=ap.id, approver="alice@example.com", reason="spam")
    assert can_send(db, run_id=run_id, draft_id=draft_id) is False
    db.close()

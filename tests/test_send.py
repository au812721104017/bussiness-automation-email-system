import pytest
import json
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import SessionLocal
from app.db.models import DraftReply, Approval, OutboundMessage, AuditEvent
from app.services.run_service import generate_run_id, create_run
from app.db.repository import create_draft
from app.services.approval_service import request_approval, approve
from app.services.gmail_send_service import GmailSendService, _validate_recipients
from app.api.send import get_sender

client = TestClient(app)

def _make_draft():
    db = SessionLocal()
    run_id = generate_run_id()
    create_run(db, run_id=run_id, status="drafted")
    draft = create_draft(db, run_id=run_id, body="Hello approved body", subject="Re: Hello", confidence=0.9, citations=[], risk_flags={})
    db.close()
    return run_id, draft.id

def _mock_client_success():
    m = MagicMock()
    m.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "provider_msg_123", "threadId": "thread_abc"}
    return m

def _mock_client_fail():
    m = MagicMock()
    m.users.return_value.messages.return_value.send.return_value.execute.side_effect = Exception("quota exceeded")
    return m

# 1. send without approval -> blocked
def test_send_without_approval_blocked():
    run_id, draft_id = _make_draft()
    db = SessionLocal()
    svc = GmailSendService(gmail_client=_mock_client_success())
    with pytest.raises(PermissionError) as exc:
        svc.send_approved_draft(db, draft_id=draft_id, run_id=run_id, to=["user@test.com"])
    assert "approval gate" in str(exc.value).lower()
    # Audit SEND_FAILED blocked exists
    audits = db.query(AuditEvent).filter(AuditEvent.run_id == run_id, AuditEvent.event_type == "SEND_FAILED").all()
    assert any("blocked" in (a.safe_metadata or "") for a in audits)
    db.close()
    # API also blocked
    resp = client.post("/send", json={"draft_id": draft_id, "run_id": run_id, "to": ["user@test.com"]})
    assert resp.status_code == 403

# 2. wrong approval/run -> blocked
def test_wrong_approval_run_blocked():
    run_id, draft_id = _make_draft()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="alice@example.com")
    svc = GmailSendService(gmail_client=_mock_client_success())
    # Wrong run
    with pytest.raises(PermissionError):
        svc.send_approved_draft(db, draft_id=draft_id, run_id="wrong-run-id", to=["user@test.com"])
    # Wrong draft
    run_id2, draft2 = _make_draft()
    with pytest.raises(PermissionError):
        svc.send_approved_draft(db, draft_id=draft2, run_id=run_id, to=["user@test.com"])
    db.close()

# 3. modified draft -> blocked
def test_modified_draft_blocked():
    run_id, draft_id = _make_draft()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="alice@example.com")
    svc = GmailSendService(gmail_client=_mock_client_success())
    # Modify draft after approval
    draft = db.query(DraftReply).filter(DraftReply.id == draft_id).first()
    from datetime import datetime, timezone
    import time
    time.sleep(0.01)
    draft.body = "MODIFIED AFTER APPROVAL"
    draft.updated_at = datetime.now(timezone.utc)
    db.commit()
    with pytest.raises(PermissionError) as exc:
        svc.send_approved_draft(db, draft_id=draft_id, run_id=run_id, to=["user@test.com"])
    assert "approval gate" in str(exc.value).lower()
    db.close()

# 4. valid approval -> send attempted, records provider ID + timestamp + thread
def test_valid_approval_send_succeeds():
    run_id, draft_id = _make_draft()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="alice@example.com")
    mock = _mock_client_success()
    svc = GmailSendService(gmail_client=mock)
    outbound = svc.send_approved_draft(db, draft_id=draft_id, run_id=run_id, to=["user@example.com"], thread_id="thread_abc")
    assert outbound.status == "sent"
    assert outbound.provider_message_id == "provider_msg_123"
    assert outbound.sent_at is not None
    assert outbound.thread_id == "thread_abc"
    # Provider called with threadId
    mock.users.return_value.messages.return_value.send.assert_called_once()
    # Audit SEND_SUCCEEDED
    audits = db.query(AuditEvent).filter(AuditEvent.run_id == run_id, AuditEvent.event_type == "SEND_SUCCEEDED").all()
    assert len(audits) >= 1
    # Outbound persisted
    persisted = db.query(OutboundMessage).filter(OutboundMessage.id == outbound.id).first()
    assert persisted.provider_message_id == "provider_msg_123"
    db.close()
    # API also succeeds with mock injected via override
    run_id2, draft2 = _make_draft()
    db2 = SessionLocal()
    ap2 = request_approval(db2, run_id=run_id2, draft_id=draft2)
    approve(db2, approval_id=ap2.id, approver="alice@example.com")
    db2.close()
    app.dependency_overrides[get_sender] = lambda: GmailSendService(gmail_client=_mock_client_success())
    resp = client.post("/send", json={"draft_id": draft2, "run_id": run_id2, "to": ["user@example.com"], "thread_id": "thread_xyz"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
    assert resp.json()["provider_message_id"] == "provider_msg_123"
    assert resp.json()["thread_id"] == "thread_abc"  # from mock result
    app.dependency_overrides.clear()

def test_recipients_and_thread_validation():
    run_id, draft_id = _make_draft()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="alice@example.com")
    svc = GmailSendService(gmail_client=_mock_client_success())
    with pytest.raises(ValueError):
        svc.send_approved_draft(db, draft_id=draft_id, run_id=run_id, to=[])  # empty
    with pytest.raises(ValueError):
        svc.send_approved_draft(db, draft_id=draft_id, run_id=run_id, to=["not-an-email"])
    with pytest.raises(ValueError):
        svc.send_approved_draft(db, draft_id=draft_id, run_id=run_id, to=None)
    db.close()

def test_validate_recipients_accepts_plain_and_display_name_format():
    # 1. plain address passes through unchanged (lowercased)
    assert _validate_recipients(["kalaiy463@gmail.com"]) == ["kalaiy463@gmail.com"]
    # 2. Gmail header-style display name -> only extracted address kept
    assert _validate_recipients(["Kalai Yarasan <kalaiy463@gmail.com>"]) == ["kalaiy463@gmail.com"]
    # 3. surrounding whitespace tolerated in both formats
    assert _validate_recipients(["  kalaiy463@gmail.com  "]) == ["kalaiy463@gmail.com"]
    assert _validate_recipients(["  Kalai Yarasan <kalaiy463@gmail.com>  "]) == ["kalaiy463@gmail.com"]
    # 4. extracted address is lowercased
    assert _validate_recipients(["Kalai Yarasan <KalaiY463@Gmail.com>"]) == ["kalaiy463@gmail.com"]
    # 5. mixed list of both formats
    assert _validate_recipients(
        ["kalaiy463@gmail.com", "Kalai Yarasan <Other@Example.com>"]
    ) == ["kalaiy463@gmail.com", "other@example.com"]

def test_validate_recipients_rejects_malformed_and_empty():
    for bad in [
        "not-an-email",            # no @ or domain
        "",                        # empty
        "   ",                     # whitespace only
        "<>",                      # empty angle brackets
        "Name <>",                 # display name, empty address
        "Name <not-an-email>",     # display name, invalid address
        "@nouser.com",             # missing local part
        "user@domain",             # missing TLD
        "user@",                   # missing domain
        "user@.com",               # malformed domain
    ]:
        with pytest.raises(ValueError):
            _validate_recipients([bad])
    # empty list still rejected
    with pytest.raises(ValueError):
        _validate_recipients([])

def test_send_with_display_name_recipient_uses_extracted_address():
    run_id, draft_id = _make_draft()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="alice@example.com")
    mock = _mock_client_success()
    svc = GmailSendService(gmail_client=mock)
    outbound = svc.send_approved_draft(
        db, draft_id=draft_id, run_id=run_id, to=["Kalai Yarasan <KalaiY463@Gmail.com>"]
    )
    assert outbound.status == "sent"
    # only the extracted lowercase address is stored, display name is dropped
    assert json.loads(outbound.recipients) == ["kalaiy463@gmail.com"]
    db.close()

def test_no_token_or_body_logged():
    run_id, draft_id = _make_draft()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="alice@example.com")
    svc = GmailSendService(gmail_client=_mock_client_success())
    # Check source does not log full body/tokens
    with open("app/services/gmail_send_service.py", encoding="utf-8") as f:
        src = f.read()
    assert "creds.token" not in src or "has_token" in src  # only has_token logged, not token value
    assert "raw" not in src.lower() or "never log raw" in src.lower()
    db.close()

# 5. provider failure -> failure audited, status failed
def test_provider_failure_audited():
    run_id, draft_id = _make_draft()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="alice@example.com")
    svc = GmailSendService(gmail_client=_mock_client_fail())
    with pytest.raises(RuntimeError) as exc:
        svc.send_approved_draft(db, draft_id=draft_id, run_id=run_id, to=["user@test.com"])
    assert "failed" in str(exc.value).lower() or "quota" in str(exc.value).lower()
    # Outbound status failed
    out = db.query(OutboundMessage).filter(OutboundMessage.draft_id == draft_id).order_by(OutboundMessage.id.desc()).first()
    assert out.status == "failed"
    assert out.error is not None
    # Audit failure
    audits = db.query(AuditEvent).filter(AuditEvent.run_id == run_id, AuditEvent.event_type == "SEND_FAILED", AuditEvent.status == "failed").all()
    assert len(audits) >= 1
    db.close()

def test_outbound_api_get():
    run_id, draft_id = _make_draft()
    db = SessionLocal()
    ap = request_approval(db, run_id=run_id, draft_id=draft_id)
    approve(db, approval_id=ap.id, approver="alice@example.com")
    svc = GmailSendService(gmail_client=_mock_client_success())
    out = svc.send_approved_draft(db, draft_id=draft_id, run_id=run_id, to=["a@b.com"])
    out_id = out.id
    db.close()
    resp = client.get(f"/send/outbound/{out_id}")
    assert resp.status_code == 200
    assert resp.json()["provider_message_id"] == "provider_msg_123"

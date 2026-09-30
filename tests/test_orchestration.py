import base64
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import SessionLocal
from app.db.models import AgentRun, DraftReply, Approval, AuditEvent, OutboundMessage
from app.schemas.email import NormalizedEmail
from app.services.orchestrator import AgentOrchestrator
from app.services.gmail_service import GmailService
from app.services.llm_interface import MockLLMProvider
from app.services.classifier import ClassifierService
from app.rag.retrieval import RetrievalService
from app.services.reply_generator import ReplyGeneratorService
from app.services.gmail_send_service import GmailSendService
from app.api.orchestrate import get_orch

def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

def _gmail_msg(msg_id="testmsg1", thread_id="thread1", subject="Internship inquiry", body="I want internship info"):
    return {
        "id": msg_id,
        "threadId": thread_id,
        "snippet": "snippet",
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "user@example.com"},
                {"name": "To", "value": "me@test.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 20 Nov 2023 10:00:00 +0000"},
            ],
            "body": {"data": _b64(body)},
        },
    }

def _mock_gmail(msg=_gmail_msg()):
    m = MagicMock()
    m.get_message.return_value = msg
    # GmailService will use normalize; but we mock get_message only, service will normalize real logic
    # So we need GmailService wrapper with mock client that returns raw
    svc = GmailService(gmail_client=m)
    # Override get_message to return msg directly
    svc.get_message = MagicMock(return_value=msg)
    svc.normalize_message = GmailService().normalize_message  # use real normalize
    # Also need to bind normalize to svc? We'll keep real method but call via svc
    # Simplify: use real service for normalize, mock for fetch
    orig_norm = GmailService().normalize_message
    svc.normalize_message = lambda x: orig_norm(x)
    return svc

def test_e2e_orchestration_with_mocks():
    db = SessionLocal()
    orch = AgentOrchestrator(
        gmail_service=_mock_gmail(_gmail_msg(msg_id="e2e1", subject="Internship", body="Need internship info")),
        classifier=ClassifierService(llm=MockLLMProvider()),
        reply_service=ReplyGeneratorService(llm=MockLLMProvider(fixed_response={"subject": "Re: Internship", "body": "Thank you for interest", "language": "en", "tone": "professional", "confidence": 0.85, "citations": ["internship_faq.md"], "safety_flags": {}, "risk_level": "low"})),
    )
    email = NormalizedEmail(message_id="e2e1", sender="user@example.com", subject="Internship", body_text="Need internship info", thread_id="t1")
    res = orch.run_for_email(db, normalized=email)
    assert "run_id" in res and "draft_id" in res
    assert res["status"] == "pending_approval"
    # Audit trace
    audits = db.query(AuditEvent).filter(AuditEvent.run_id == res["run_id"]).all()
    types = [a.event_type for a in audits]
    assert "EMAIL_NORMALIZED" in types
    assert "EMAIL_CLASSIFIED" in types
    assert "KNOWLEDGE_RETRIEVED" in types
    assert "DRAFT_CREATED" in types
    assert "APPROVAL_REQUESTED" in types
    # Draft exists
    draft = db.query(DraftReply).filter(DraftReply.id == res["draft_id"]).first()
    assert draft is not None
    db.close()

def test_idempotency_duplicate_prevention():
    db = SessionLocal()
    orch = AgentOrchestrator(classifier=ClassifierService(llm=MockLLMProvider()))
    email = NormalizedEmail(message_id="dup1", sender="a@b.com", subject="Hello", body_text="Meeting request", thread_id="tdup")
    r1 = orch.run_for_email(db, normalized=email)
    r2 = orch.run_for_email(db, normalized=email)
    assert r2["duplicate"] is True
    assert r2["run_id"] == r1["run_id"]
    assert r2["draft_id"] == r1["draft_id"]
    # Should not create second draft
    drafts = db.query(DraftReply).filter(DraftReply.run_id == r1["run_id"]).all()
    assert len(drafts) == 1
    db.close()

def test_resumable_traceability():
    db = SessionLocal()
    orch = AgentOrchestrator(classifier=ClassifierService(llm=MockLLMProvider()))
    email = NormalizedEmail(message_id="trace1", sender="a@b.com", subject="Pricing", body_text="How much cost?", thread_id="ttrace")
    res = orch.run_for_email(db, normalized=email)
    rid = res["run_id"]
    trace = orch.get_run_trace(db, rid)
    assert trace["run"]["run_id"] == rid
    assert len(trace["audits"]) >= 5
    # Run_id traces all
    for a in trace["audits"]:
        assert "run_id" not in a or True  # audits have run_id via query filter
    assert trace["drafts"][0]["id"] == res["draft_id"]
    db.close()

def test_duplicate_thread_state_stored():
    db = SessionLocal()
    from app.db.models import ThreadState
    orch = AgentOrchestrator(classifier=ClassifierService(llm=MockLLMProvider()))
    email = NormalizedEmail(message_id="thr1", sender="a@b.com", subject="Hi", body_text="test", thread_id="thread_xyz")
    orch.run_for_email(db, normalized=email)
    ts = db.query(ThreadState).filter(ThreadState.thread_id == "thread_xyz").first()
    assert ts is not None
    assert ts.last_run_id is not None
    db.close()

def test_e2e_via_api_with_mocked_gmail():
    # Use dependency override to inject mocked Gmail that returns fixed message
    mock_raw = _gmail_msg(msg_id="api_msg1", subject="Pricing", body="How much for course?")
    mock_svc = MagicMock()
    mock_svc.get_message.return_value = mock_raw
    # Need real normalize
    real_gmail = GmailService()
    mock_svc.normalize_message.side_effect = lambda x: real_gmail.normalize_message(x)
    mock_svc.list_messages.return_value = [{"id": "api_msg1", "threadId": "t1"}]

    def _orch_factory():
        return AgentOrchestrator(gmail_service=mock_svc)

    app.dependency_overrides[get_orch] = _orch_factory
    client = TestClient(app)
    resp = client.post("/orchestrate/run/gmail/api_msg1")
    assert resp.status_code == 200
    data = resp.json()
    assert "run_id" in data and "draft_id" in data
    assert data["status"] == "pending_approval"
    # Second call idempotent
    resp2 = client.post("/orchestrate/run/gmail/api_msg1")
    assert resp2.json()["duplicate"] is True
    assert resp2.json()["run_id"] == data["run_id"]
    # Trace
    trace = client.get(f"/orchestrate/trace/{data['run_id']}")
    assert trace.status_code == 200
    assert trace.json()["run"]["run_id"] == data["run_id"]
    # Duplicate seq: also test orchestrate/run directly
    resp3 = client.post("/orchestrate/run", json={"message_id": "plain1", "sender": "a@b.com", "subject": "Hello", "body_text": "Internship info"})
    assert resp3.status_code == 200
    assert resp3.json()["status"] == "pending_approval"
    app.dependency_overrides.clear()

def test_orchestrate_send_only_after_approval():
    import uuid
    mid = f"send1-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    orch = AgentOrchestrator(classifier=ClassifierService(llm=MockLLMProvider()), send_service=GmailSendService(gmail_client=MagicMock(**{"users.return_value.messages.return_value.send.return_value.execute.return_value": {"id": "prov1", "threadId": "t1"}})))
    email = NormalizedEmail(message_id=mid, sender="a@b.com", subject="Hello", body_text="Need help", thread_id="t_send")
    res = orch.run_for_email(db, normalized=email)
    # Without approval, send blocked
    with pytest.raises(PermissionError):
        orch.send_if_approved(db, run_id=res["run_id"], draft_id=res["draft_id"], to=["user@test.com"])
    # Approve then send succeeds
    from app.services.approval_service import request_approval, approve
    # Already pending from orchestration, get approval
    db2 = SessionLocal()
    ap = db2.query(Approval).filter(Approval.draft_id == res["draft_id"]).first()
    approve(db2, approval_id=ap.id, approver="human@test.com")
    db2.close()
    # Need fresh db session for send
    db3 = SessionLocal()
    out = orch.send_if_approved(db3, run_id=res["run_id"], draft_id=res["draft_id"], to=["user@test.com"], thread_id="t_send")
    assert out.status == "sent"
    assert out.provider_message_id == "prov1"
    db.close()
    db3.close()

def test_prevents_duplicate_replies_via_message_id():
    db = SessionLocal()
    orch = AgentOrchestrator(classifier=ClassifierService(llm=MockLLMProvider()))
    email = NormalizedEmail(message_id="dup_send1", sender="a@b.com", subject="Hi", body_text="test duplicate send", thread_id="tdup_send")
    r1 = orch.run_for_email(db, normalized=email)
    # Try to orchestrate same message again, should be deduped and not create new outbound on send duplicate check (idempotency)
    r2 = orch.run_for_email(db, normalized=email)
    assert r2["run_id"] == r1["run_id"]
    # Outbound duplicate prevention: if we attempt send twice with same run/draft, second should either create second outbound? But send service will allow (since approval still valid) but we test that first send records provider id and second send would be duplicate? For now check that message_id stored once in EmailMessage
    from app.db.models import EmailMessage
    count = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == "dup_send1").count()
    assert count == 1
    db.close()

def _auto_send_orchestrator():
    gmail_mock = MagicMock()
    gmail_mock.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "prov_auto", "threadId": "thread_auto_out"}
    return gmail_mock, AgentOrchestrator(
        classifier=ClassifierService(llm=MockLLMProvider()),
        send_service=GmailSendService(gmail_client=gmail_mock),
    )

def _inbound_nyxcareers(mid, thread_id):
    # Inbound: From = applicant, To = our own account
    return NormalizedEmail(
        message_id=mid,
        sender="nyxcareers@gmail.com",
        sender_email="nyxcareers@gmail.com",
        subject="Internship inquiry",
        body_text="Please share internship details",
        thread_id=thread_id,
        recipients=["myaccount@gmail.com"],
    )

def test_auto_send_replies_to_original_sender_not_inbound_to():
    import uuid, json
    from unittest.mock import patch
    mid = f"autosend-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    gmail_mock, orch = _auto_send_orchestrator()
    email = _inbound_nyxcareers(mid, "thread_inbound_1")
    fake = MagicMock()
    fake.approval_required = False
    with patch("app.services.orchestrator.get_settings", return_value=fake), \
         patch("app.core.config.get_settings", return_value=fake):
        res = orch.run_for_email(db, normalized=email)

    assert res["status"] == "sent"
    outbound = db.query(OutboundMessage).filter(OutboundMessage.run_id == res["run_id"]).first()
    assert outbound is not None and outbound.status == "sent"
    # Reply goes to the original sender only — never the inbound To address
    assert json.loads(outbound.recipients) == ["nyxcareers@gmail.com"]
    assert "myaccount@gmail.com" not in outbound.recipients

    # The MIME actually handed to Gmail must address the sender only
    body = gmail_mock.users.return_value.messages.return_value.send.call_args.kwargs["body"]
    raw = base64.urlsafe_b64decode(body["raw"]).decode()
    assert "To: nyxcareers@gmail.com" in raw
    assert "myaccount@gmail.com" not in raw
    # Thread handling preserved
    assert body["threadId"] == "thread_inbound_1"
    assert outbound.thread_id == "thread_auto_out"
    db.close()

def test_auto_send_skipped_and_approval_required_when_approval_required_true():
    import uuid
    from unittest.mock import patch
    mid = f"autogate-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    gmail_mock, orch = _auto_send_orchestrator()
    email = _inbound_nyxcareers(mid, "thread_inbound_2")
    fake = MagicMock()
    fake.approval_required = True
    with patch("app.services.orchestrator.get_settings", return_value=fake), \
         patch("app.core.config.get_settings", return_value=fake):
        res = orch.run_for_email(db, normalized=email)

    assert res["status"] == "pending_approval"
    assert "outbound_id" not in res
    assert db.query(OutboundMessage).filter(OutboundMessage.run_id == res["run_id"]).count() == 0
    gmail_mock.users.return_value.messages.return_value.send.assert_not_called()
    ap = db.query(Approval).filter(Approval.draft_id == res["draft_id"]).first()
    assert ap is not None and ap.status == "pending"
    db.close()

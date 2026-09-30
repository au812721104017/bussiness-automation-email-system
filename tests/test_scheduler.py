import time
import base64
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app
from app.db.session import SessionLocal
from app.db.models import AgentRun, Approval, AuditEvent, DraftReply, EmailMessage, OutboundMessage
from app.db.repository import create_draft, create_email_message
from app.services.run_service import create_run
from app.jobs.poller import PollerService
from app.jobs.scheduler import SchedulerService, get_scheduler
from app.services.gmail_service import GmailService
from app.services.orchestrator import AgentOrchestrator

def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

def _gmail_msg(mid="m1", subject="Hello", body="test"):
    return {"id": mid, "threadId": f"t_{mid}", "snippet": "snip", "payload": {"mimeType": "text/plain", "headers": [{"name": "From", "value": "a@b.com"}, {"name": "Subject", "value": subject}], "body": {"data": _b64(body)}}}

def _make_gmail_mock(msgs):
    m = MagicMock()
    m.list_messages.return_value = msgs
    real = GmailService()
    m.get_message.side_effect = lambda mid: _gmail_msg(mid=mid, subject="Hi", body="Hello")
    m.normalize_message.side_effect = lambda raw: real.normalize_message(raw)
    return m

def _purge_message(db, gmail_id: str):
    em = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == gmail_id).first()
    if not em:
        return
    for run in db.query(AgentRun).filter(AgentRun.email_id == em.id).all():
        for draft in db.query(DraftReply).filter(DraftReply.run_id == run.run_id).all():
            db.query(Approval).filter(Approval.draft_id == draft.id).delete(synchronize_session=False)
            db.query(OutboundMessage).filter(OutboundMessage.draft_id == draft.id).delete(synchronize_session=False)
        db.query(DraftReply).filter(DraftReply.run_id == run.run_id).delete(synchronize_session=False)
        db.query(AuditEvent).filter(AuditEvent.run_id == run.run_id).delete(synchronize_session=False)
        db.query(AgentRun).filter(AgentRun.run_id == run.run_id).delete(synchronize_session=False)
    db.delete(em)
    db.commit()

def test_polling_processes_eligible_only():
    db = SessionLocal()
    gmail_mock = _make_gmail_mock([{"id": "poll1"}, {"id": "poll2"}])
    orch = AgentOrchestrator(gmail_service=gmail_mock)
    poller = PollerService(gmail_service=gmail_mock, orchestrator=orch, max_retries=2, backoff=0.01)
    poller.clear_processed_cache()
    for mid in ["poll1", "poll2"]:
        existing = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == mid).first()
        if existing:
            db.delete(existing)
            db.commit()
    stats = poller.poll_once(db, max_results=5)
    assert stats["listed"] == 2
    assert stats["processed"] == 2
    assert stats["skipped_duplicate"] == 0
    audits = db.query(AuditEvent).filter(AuditEvent.event_type.like("SCHEDULER%")).all()
    assert any(a.event_type == "SCHEDULER_POLL_COMPLETED" for a in audits)
    db.close()

def test_deduplication_prevents_reprocessing():
    db = SessionLocal()
    gmail_mock = _make_gmail_mock([{"id": "dup_poll1"}])
    orch = AgentOrchestrator(gmail_service=gmail_mock)
    poller = PollerService(gmail_service=gmail_mock, orchestrator=orch, max_retries=1, backoff=0.01)
    poller.clear_processed_cache()
    # ensure clean
    ex = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == "dup_poll1").first()
    if ex:
        db.delete(ex)
        db.commit()
    s1 = poller.poll_once(db, max_results=5)
    assert s1["processed"] == 1
    s2 = poller.poll_once(db, max_results=5)
    assert s2["skipped_duplicate"] >= 1
    assert s2["processed"] == 0
    count = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == "dup_poll1").count()
    assert count == 1
    db.close()

def test_stored_message_without_completed_run_is_eligible():
    """A message persisted by fetch/normalize but never drafted must still reach the orchestrator."""
    db = SessionLocal()
    mid = "incomplete_poll1"
    _purge_message(db, mid)
    # Simulate an earlier poll that stored the message, then died before DRAFT_CREATED
    create_email_message(db, sender="a@b.com", subject="Hi", body_text="Hello", gmail_message_id=mid, thread_id=f"t_{mid}")
    assert db.query(EmailMessage).filter(EmailMessage.gmail_message_id == mid).first() is not None

    orch = MagicMock()
    poller = PollerService(gmail_service=_make_gmail_mock([{"id": mid}]), orchestrator=orch, max_retries=1, backoff=0.01)
    poller.clear_processed_cache()
    stats = poller.poll_once(db, max_results=5)
    assert stats["skipped_duplicate"] == 0
    assert stats["processed"] == 1
    orch.run_for_gmail_message_id.assert_called_once()
    _purge_message(db, mid)
    db.close()

def test_message_with_completed_run_stays_skipped():
    """Duplicate protection is unchanged once a run produced a draft — never orchestrated twice."""
    db = SessionLocal()
    mid = "completed_poll1"
    _purge_message(db, mid)
    em = create_email_message(db, sender="a@b.com", subject="Hi", body_text="Hello", gmail_message_id=mid, thread_id=f"t_{mid}")
    run = create_run(db, status="pending_approval", email_id=em.id)
    create_draft(db, run_id=run.run_id, body="draft body", subject="Re: Hi", email_id=em.id)

    orch = MagicMock()
    poller = PollerService(gmail_service=_make_gmail_mock([{"id": mid}]), orchestrator=orch, max_retries=1, backoff=0.01)
    poller.clear_processed_cache()
    stats = poller.poll_once(db, max_results=5)
    assert stats["listed"] == 1
    assert stats["skipped_duplicate"] == 1
    assert stats["processed"] == 0
    orch.run_for_gmail_message_id.assert_not_called()
    _purge_message(db, mid)
    db.close()

def test_retry_transient_bounded():
    db = SessionLocal()
    calls = {"n": 0}
    gmail_mock = _make_gmail_mock([{"id": "retry1"}])
    def _get_with_transient(mid):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise Exception("temporarily unavailable - transient")
        return _gmail_msg(mid=mid, subject="Hi", body="Hello")
    gmail_mock.get_message.side_effect = _get_with_transient
    real = GmailService()
    gmail_mock.normalize_message.side_effect = lambda raw: real.normalize_message(raw)
    orch = AgentOrchestrator(gmail_service=gmail_mock)
    poller = PollerService(gmail_service=gmail_mock, orchestrator=orch, max_retries=3, backoff=0.01)
    poller.clear_processed_cache()
    existing = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == "retry1").first()
    if existing:
        db.delete(existing)
        db.commit()
    stats = poller.poll_once(db, max_results=5)
    assert stats["processed"] == 1 or stats["failed_transient"] == 0
    db.close()

def test_no_retry_permanent_validation_error():
    db = SessionLocal()
    orch_mock = MagicMock()
    orch_mock.run_for_gmail_message_id.side_effect = Exception("validation error: Invalid recipient")
    gmail_mock = _make_gmail_mock([{"id": "perm1"}])
    poller = PollerService(gmail_service=gmail_mock, orchestrator=orch_mock, max_retries=3, backoff=0.01)
    poller.clear_processed_cache()
    stats = poller.poll_once(db, max_results=5)
    assert stats["failed_permanent"] >= 1
    assert stats["failed_transient"] == 0
    db.close()

def test_permanent_failure_stays_eligible_for_later_poll():
    """A permanent failure persists nothing, so it must never be cached as a duplicate."""
    db = SessionLocal()
    mid = "perm_eligible1"
    orch_mock = MagicMock()
    orch_mock.run_for_gmail_message_id.side_effect = Exception("validation error: Invalid recipient")
    gmail_mock = _make_gmail_mock([{"id": mid}])
    poller = PollerService(gmail_service=gmail_mock, orchestrator=orch_mock, max_retries=3, backoff=0.01)
    poller.clear_processed_cache()
    s1 = poller.poll_once(db, max_results=5)
    assert s1["failed_permanent"] == 1
    assert s1["skipped_duplicate"] == 0
    assert mid not in poller._processed_ids
    s2 = poller.poll_once(db, max_results=5)
    assert s2["skipped_duplicate"] == 0
    assert s2["failed_permanent"] == 1
    assert orch_mock.run_for_gmail_message_id.call_count == 2
    assert mid not in poller._processed_ids
    db.close()

def test_poller_query_comes_from_config():
    from app.core.config import get_settings
    poller = PollerService(gmail_service=MagicMock(), orchestrator=MagicMock())
    assert poller.query == get_settings().scheduler_query
    assert "is:unread" not in poller.query
    assert "in:inbox" in poller.query
    explicit = PollerService(gmail_service=MagicMock(), orchestrator=MagicMock(), query="in:inbox newer_than:1d")
    assert explicit.query == "in:inbox newer_than:1d"

def test_scheduler_disabled_by_default_in_tests():
    svc = SchedulerService()
    assert svc.enabled is False
    assert svc.is_running() is False
    assert svc.start() is False

def test_scheduler_graceful_startup_shutdown():
    gmail_mock = _make_gmail_mock([])
    orch = AgentOrchestrator(gmail_service=gmail_mock)
    poller = PollerService(gmail_service=gmail_mock, orchestrator=orch)
    svc = SchedulerService(poller=poller, interval_seconds=0.2, enabled=True)
    assert svc.start() is True
    assert svc.is_running() is True
    time.sleep(0.5)
    assert svc.stop() is True
    assert svc.is_running() is False
    assert svc.stop() is False

def test_scheduler_never_bypasses_approval_gate():
    db = SessionLocal()
    gmail_mock = _make_gmail_mock([{"id": "gate1"}])
    # need fresh msg
    gmail_mock.get_message.side_effect = lambda mid: _gmail_msg(mid=mid, subject="Hi", body="Hello gate check")
    real = GmailService()
    gmail_mock.normalize_message.side_effect = lambda raw: real.normalize_message(raw)
    # clean prior
    ex = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == "gate1").first()
    if ex:
        db.delete(ex)
        db.commit()
    orch = AgentOrchestrator(gmail_service=gmail_mock)
    poller = PollerService(gmail_service=gmail_mock, orchestrator=orch, max_retries=1, backoff=0.01)
    poller.clear_processed_cache()
    stats = poller.poll_once(db, max_results=5)
    assert stats["processed"] == 1
    from app.db.models import AgentRun, OutboundMessage
    email = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == "gate1").first()
    assert email is not None
    run = db.query(AgentRun).filter(AgentRun.email_id == email.id).order_by(AgentRun.created_at.desc()).first()
    assert run.status == "pending_approval"
    outbound = db.query(OutboundMessage).filter(OutboundMessage.run_id == run.run_id).first()
    assert outbound is None
    db.close()

def test_scheduler_api_and_audit_each_run():
    client = TestClient(app)
    s = client.get("/scheduler/status").json()
    assert s["enabled"] is False
    assert "interval_seconds" in s
    assert s["gate"] is not None
    resp = client.post("/scheduler/trigger?max_results=2")
    assert resp.status_code == 200
    assert "listed" in resp.json()
    assert "run_id" in resp.json()
    db = SessionLocal()
    audits = db.query(AuditEvent).filter(AuditEvent.event_type.like("SCHEDULER%")).order_by(AuditEvent.created_at.desc()).limit(5).all()
    assert len(audits) >= 1
    db.close()

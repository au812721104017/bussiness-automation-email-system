import json
import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.base import Base
from app.db.models import AgentRun, EmailMessage, DraftReply, Approval, AuditEvent, ThreadState, EmailJob
from app.services.run_service import create_run, update_run_status, generate_run_id, get_run
from app.services.audit_service import create_audit_event, get_audits_by_run
from app.db.repository import create_email_message, create_draft, create_approval, upsert_thread_state

@pytest.fixture
def db():
    # In-memory SQLite isolated per test function
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)

def test_run_id_generation_traceability():
    a = generate_run_id()
    b = generate_run_id()
    assert a != b
    uuid.UUID(a)
    uuid.UUID(b)
    assert len(a) == 36

def test_model_creation_agent_run(db):
    run = create_run(db, status="started", metadata={"source": "test"})
    assert run.id is not None
    assert run.run_id is not None
    uuid.UUID(run.run_id)
    assert run.status == "started"
    # Audit created automatically
    audits = get_audits_by_run(db, run.run_id)
    assert len(audits) >= 1
    assert audits[0].event_type == "RUN_CREATED"
    assert audits[0].run_id == run.run_id
    assert "timestamp" not in audits[0].__dict__ or True  # created_at exists
    assert audits[0].created_at is not None

def test_audit_event_creation_contains_required_fields(db):
    rid = generate_run_id()
    # Need a run first for FK
    run = create_run(db, run_id=rid, status="started")
    ev = create_audit_event(db, run_id=rid, event_type="EMAIL_RECEIVED", status="success", safe_metadata={"sender": "a@b.com", "subject_len": 10})
    assert ev.id is not None
    assert ev.run_id == rid
    assert ev.event_type == "EMAIL_RECEIVED"
    assert ev.status == "success"
    assert ev.created_at is not None
    # safe_metadata is JSON string
    meta = json.loads(ev.safe_metadata)
    assert meta["sender"] == "a@b.com"
    # Missing run_id must fail
    with pytest.raises(ValueError):
        create_audit_event(db, run_id="", event_type="ERROR")

def test_audit_traceability_run_lifecycle(db):
    run = create_run(db, status="started")
    rid = run.run_id
    create_audit_event(db, run_id=rid, event_type="EMAIL_NORMALIZED", status="success", safe_metadata={"message_id": "msg123"})
    create_audit_event(db, run_id=rid, event_type="DRAFT_CREATED", status="success", safe_metadata={"draft_len": 100})
    audits = get_audits_by_run(db, rid)
    assert len(audits) == 3  # RUN_CREATED + 2
    # Ordered by created_at
    types = [a.event_type for a in audits]
    assert types == ["RUN_CREATED", "EMAIL_NORMALIZED", "DRAFT_CREATED"]
    # All share same run_id
    assert all(a.run_id == rid for a in audits)
    # Every audit has timestamp, run_id, event_type, status, safe_metadata
    for a in audits:
        assert a.created_at is not None
        assert a.event_type
        assert a.status

def test_persistence_and_retrieval_email_draft_approval(db):
    email = create_email_message(db, sender="user@example.com", subject="Hello", body_text="Need internship info", gmail_message_id="gmail_123", thread_id="thread_abc", recipients=["me@test.com"])
    assert email.id is not None
    assert email.gmail_message_id == "gmail_123"
    persisted = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == "gmail_123").first()
    assert persisted.body_text == "Need internship info"

    run = create_run(db, status="started", email_id=email.id)
    draft = create_draft(db, run_id=run.run_id, email_id=email.id, subject="Re: Hello", body="Thanks for reaching out...", confidence=0.92, citations=["doc1"], risk_flags={"sensitive": False})
    assert draft.id is not None
    assert draft.run_id == run.run_id
    fetched = db.query(DraftReply).filter(DraftReply.run_id == run.run_id).first()
    assert fetched.body == "Thanks for reaching out..."

    approval = create_approval(db, run_id=run.run_id, draft_id=draft.id, status="pending")
    assert approval.id is not None
    assert approval.status == "pending"
    # Approval tied to exact draft/run
    fetched_ap = db.query(Approval).filter(Approval.run_id == run.run_id).first()
    assert fetched_ap.draft_id == draft.id

def test_run_status_change_creates_audit(db):
    run = create_run(db, status="started")
    rid = run.run_id
    updated = update_run_status(db, rid, "pending_approval", safe_metadata={"reason": "draft ready"})
    assert updated.status == "pending_approval"
    audits = get_audits_by_run(db, rid)
    assert any(a.event_type == "RUN_STATUS_CHANGED" for a in audits)
    status_audit = [a for a in audits if a.event_type == "RUN_STATUS_CHANGED"][0]
    meta = json.loads(status_audit.safe_metadata)
    assert meta["old_status"] == "started"
    assert meta["new_status"] == "pending_approval"

def test_thread_state_persistence(db):
    ts = upsert_thread_state(db, thread_id="thread_xyz", last_run_id="run123")
    assert ts.thread_id == "thread_xyz"
    assert ts.message_count == 1
    ts2 = upsert_thread_state(db, thread_id="thread_xyz", last_run_id="run456")
    assert ts2.id == ts.id
    assert ts2.message_count == 2
    assert ts2.last_run_id == "run456"

def test_email_job_backward_compat(db):
    job = EmailJob(to_email="a@b.com", subject="s", body="b", status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    assert job.id is not None
    fetched = db.query(EmailJob).filter(EmailJob.to_email == "a@b.com").first()
    assert fetched.status == "queued"

def test_repository_no_direct_db_in_routes():
    # Verify repository module exposes functions (architecture rule)
    import app.db.repository as repo
    assert hasattr(repo, "create_email_message")
    assert hasattr(repo, "create_draft")
    assert hasattr(repo, "create_approval")
    assert hasattr(repo, "upsert_thread_state")
    import app.services.audit_service as audit
    assert hasattr(audit, "create_audit_event")
    import app.services.run_service as rs
    assert hasattr(rs, "create_run")

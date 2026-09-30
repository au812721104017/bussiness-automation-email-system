import uuid
import json
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from app.db.base import Base

def _utcnow():
    return datetime.now(timezone.utc)

def _uuid():
    return str(uuid.uuid4())

class AgentRun(Base):
    __tablename__ = "agent_runs"
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(36), unique=True, index=True, nullable=False, default=_uuid)
    status = Column(String(32), nullable=False, default="started")  # started, classified, drafted, pending_approval, completed, failed
    email_id = Column(Integer, ForeignKey("email_messages.id", ondelete="SET NULL"), nullable=True)
    meta_data = Column("metadata", Text, nullable=True)  # JSON string for SQLite portability
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    email = relationship("EmailMessage", back_populates="runs")
    audits = relationship("AuditEvent", back_populates="run", cascade="all, delete-orphan")
    drafts = relationship("DraftReply", back_populates="run", cascade="all, delete-orphan")

class EmailMessage(Base):
    __tablename__ = "email_messages"
    id = Column(Integer, primary_key=True, index=True)
    gmail_message_id = Column(String, unique=True, index=True, nullable=True)
    thread_id = Column(String, index=True, nullable=True)
    sender = Column(String, nullable=False, default="")
    recipients = Column(Text, nullable=True)  # JSON list string
    subject = Column(String, nullable=True)
    body_text = Column(Text, nullable=True)
    body_snippet = Column(String(500), nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    runs = relationship("AgentRun", back_populates="email")
    drafts = relationship("DraftReply", back_populates="email")

class DraftReply(Base):
    __tablename__ = "draft_replies"
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(36), ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False, index=True)
    email_id = Column(Integer, ForeignKey("email_messages.id", ondelete="SET NULL"), nullable=True)
    subject = Column(String, nullable=True)
    body = Column(Text, nullable=False, default="")
    language = Column(String(10), default="en", nullable=False)
    tone = Column(String(32), default="professional", nullable=False)
    confidence = Column(Float, nullable=True)
    citations = Column(Text, nullable=True)  # JSON string
    risk_flags = Column(Text, nullable=True)  # JSON string
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    run = relationship("AgentRun", back_populates="drafts")
    email = relationship("EmailMessage", back_populates="drafts")
    approval = relationship("Approval", back_populates="draft", uselist=False, cascade="all, delete-orphan")

class Approval(Base):
    __tablename__ = "approvals"
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(36), ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False, index=True, unique=True)
    draft_id = Column(Integer, ForeignKey("draft_replies.id", ondelete="CASCADE"), nullable=False, unique=True)
    status = Column(String(20), nullable=False, default="pending")  # pending, approved, rejected
    approver = Column(String, nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    decided_at = Column(DateTime(timezone=True), nullable=True)

    run = relationship("AgentRun")
    draft = relationship("DraftReply", back_populates="approval")

    __table_args__ = (
        UniqueConstraint("run_id", "draft_id", name="uq_approval_run_draft"),
    )

class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(36), ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(64), nullable=False, index=True)  # RUN_CREATED, EMAIL_RECEIVED, etc
    status = Column(String(32), nullable=False, default="success")
    safe_metadata = Column(Text, nullable=True)  # JSON string - must not contain secrets/PII raw bodies
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    run = relationship("AgentRun", back_populates="audits")

    __table_args__ = (
        Index("ix_audit_run_time", "run_id", "created_at"),
    )

class ThreadState(Base):
    __tablename__ = "thread_states"
    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(String, unique=True, index=True, nullable=False)
    last_run_id = Column(String(36), ForeignKey("agent_runs.run_id", ondelete="SET NULL"), nullable=True)
    last_email_id = Column(Integer, ForeignKey("email_messages.id", ondelete="SET NULL"), nullable=True)
    message_count = Column(Integer, default=0, nullable=False)
    state = Column(Text, nullable=True)  # JSON string
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    id = Column(Integer, primary_key=True, index=True)
    draft_id = Column(Integer, ForeignKey("draft_replies.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(String(36), ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False, index=True)
    provider_message_id = Column(String, nullable=True, index=True)
    status = Column(String(20), nullable=False, default="queued")  # queued, sent, failed
    recipients = Column(Text, nullable=True)  # JSON list, safe (only domain logged)
    thread_id = Column(String, nullable=True, index=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

# Backward compatibility: preserve Phase 1 EmailJob table
class EmailJob(Base):
    __tablename__ = "email_jobs"
    id = Column(Integer, primary_key=True, index=True)
    to_email = Column(String, index=True)
    subject = Column(String)
    body = Column(Text)
    status = Column(String, default="pending")
    created_at = Column(DateTime(timezone=True), default=_utcnow)

import base64
import re
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from email.mime.text import MIMEText
from email.utils import formatdate, parseaddr
from sqlalchemy.orm import Session
from app.db.models import DraftReply, Approval, OutboundMessage, AgentRun
from app.services.approval_service import can_send
from app.services.audit_service import create_audit_event
from app.core.logging import get_logger

log = get_logger(__name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _validate_recipients(to_list: List[str]) -> List[str]:
    if not to_list:
        raise ValueError("Recipients required")
    cleaned = []
    for addr in to_list:
        addr = addr.strip()
        # Accept both "user@x.com" and "Display Name <user@x.com>"; use only the extracted address
        _, extracted = parseaddr(addr)
        extracted = (extracted or "").strip()
        if not EMAIL_RE.match(extracted):
            raise ValueError(f"Invalid recipient: {addr}")
        cleaned.append(extracted.lower())
    return cleaned

class GmailSendService:
    """Abstraction — rest of app must not call googleapiclient directly for send. Enforces approval gate."""

    def __init__(self, gmail_client: Optional[Any] = None):
        # Optional injected mock; else lazy build from token
        self._client = gmail_client

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from app.core.config import get_settings
            s = get_settings()
            import os
            if not os.path.exists(s.gmail_token_path):
                raise FileNotFoundError("Gmail token not found — complete OAuth")
            scopes = [sc.strip() for sc in (s.gmail_send_scopes or s.gmail_scopes).split(",") if sc.strip()]
            creds = Credentials.from_authorized_user_file(s.gmail_token_path, scopes=scopes)
            log.info("Gmail send client built", extra={"extra_data": {"has_token": bool(creds.token)}})
            return build("gmail", "v1", credentials=creds)
        except ImportError as e:
            raise RuntimeError("google-api-python-client not installed") from e

    def send_approved_draft(self, db: Session, *, draft_id: int, run_id: str, to: Optional[List[str]] = None, thread_id: Optional[str] = None) -> OutboundMessage:
        # Critical rule: no send unless approval valid, active, exact draft/run, not changed
        if not can_send(db, run_id=run_id, draft_id=draft_id):
            # Audit failure safely (no PII)
            try:
                create_audit_event(db, run_id=run_id, event_type="SEND_FAILED", status="blocked", safe_metadata={"draft_id": draft_id, "reason": "approval_gate_blocked"})
            except Exception:
                pass
            raise PermissionError(f"Sending blocked by approval gate — no valid approved approval for run {run_id} draft {draft_id}")

        draft = db.query(DraftReply).filter(DraftReply.id == draft_id).first()
        if not draft:
            raise ValueError("Draft not found")
        # Validate draft belongs to run
        if draft.run_id != run_id:
            raise PermissionError("Draft run_id mismatch — not exact pair")
        # Validate recipients: use draft's related email or explicit to, or fallback to safe placeholder
        recipients = to
        if not recipients:
            # Try to derive from draft risk? For Phase 8, require explicit to
            raise ValueError("Recipients required for send — provide 'to' list")
        recipients = _validate_recipients(recipients)

        # Thread handling: if thread_id provided, validate it matches draft's context if any
        effective_thread = thread_id or draft.email_id and None  # for now accept thread_id as is, no cross-check needed beyond format
        if effective_thread is not None and not isinstance(effective_thread, str):
            raise ValueError("thread_id must be string")

        # Build MIME — never log full body or tokens
        msg = MIMEText(draft.body or "")
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = draft.subject or "(no subject)"
        msg["Date"] = formatdate(localtime=True)
        # If reply-in-thread, add headers
        if effective_thread:
            msg["In-Reply-To"] = effective_thread
            # Gmail uses threadId param, not header, but we keep header for compatibility

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        # Record outbound attempt
        outbound = OutboundMessage(draft_id=draft_id, run_id=run_id, status="queued", recipients=json.dumps(recipients), thread_id=effective_thread)
        db.add(outbound)
        db.commit()
        db.refresh(outbound)

        create_audit_event(db, run_id=run_id, event_type="SEND_ATTEMPTED", status="success", safe_metadata={"draft_id": draft_id, "outbound_id": outbound.id, "thread_id": effective_thread, "recipient_count": len(recipients)})

        try:
            client = self._get_client()
            body = {"raw": raw}
            if effective_thread:
                body["threadId"] = effective_thread
            # Never log raw
            log.info("Gmail send attempting", extra={"extra_data": {"draft_id": draft_id, "run_id": run_id[:8], "thread_id": effective_thread, "recipient_domains": [r.split("@")[-1] for r in recipients]}})
            result = client.users().messages().send(userId="me", body=body).execute()
            provider_id = result.get("id")
            outbound.provider_message_id = provider_id
            outbound.status = "sent"
            outbound.sent_at = datetime.now(timezone.utc)
            outbound.thread_id = result.get("threadId") or effective_thread
            db.commit()
            create_audit_event(db, run_id=run_id, event_type="SEND_SUCCEEDED", status="success", safe_metadata={"draft_id": draft_id, "outbound_id": outbound.id, "provider_message_id": provider_id})
            # Update run status
            run = db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
            if run:
                run.status = "sent"
                run.updated_at = datetime.now(timezone.utc)
                db.commit()
            log.info("Gmail send succeeded", extra={"extra_data": {"provider_message_id": provider_id, "draft_id": draft_id}})
            return outbound
        except Exception as e:
            # Record failure safely, never log tokens/bodies
            outbound.status = "failed"
            outbound.error = str(e)[:500]
            db.commit()
            create_audit_event(db, run_id=run_id, event_type="SEND_FAILED", status="failed", safe_metadata={"draft_id": draft_id, "outbound_id": outbound.id, "error_type": type(e).__name__})
            log.error(f"Gmail send failed: {type(e).__name__}")
            raise RuntimeError(f"Gmail send failed: {type(e).__name__}: {str(e)[:200]}") from e

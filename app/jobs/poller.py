import time
from typing import List, Dict, Any, Optional, Set
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.db.models import AgentRun, DraftReply, EmailMessage
from app.services.gmail_service import GmailService
from app.services.orchestrator import AgentOrchestrator
from app.services.audit_service import create_audit_event
from app.services.run_service import generate_run_id
from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

# Retry classification
TRANSIENT_ERRORS = ("timeout", "temporarily", "unavailable", "rate", "connection", "network", "502", "503", "504", "quota")
PERMANENT_ERRORS = ("validation", "valueerror", "invalid", "not found", "permission", "approval gate")

def _is_transient(msg: str) -> bool:
    lower = msg.lower()
    return any(k in lower for k in TRANSIENT_ERRORS)

def _is_permanent(msg: str) -> bool:
    lower = msg.lower()
    return any(k in lower for k in PERMANENT_ERRORS)

class PollerService:
    """One poll iteration: list Gmail, deduplicate via EmailMessage.gmail_message_id, orchestrate eligible only, bounded retries, audit, never bypass gate."""

    def __init__(self, gmail_service: Optional[GmailService] = None, orchestrator: Optional[AgentOrchestrator] = None, max_retries: int = 3, backoff: float = 1.0, query: Optional[str] = None):
        self.gmail = gmail_service or GmailService()
        self.orch = orchestrator or AgentOrchestrator(gmail_service=self.gmail)
        self.max_retries = max_retries
        self.backoff = backoff
        # Single source of truth for the poll query — never hardcode a default here.
        self.query = query if query is not None else get_settings().scheduler_query
        # In-memory dedup for this process (supplement DB check)
        self._processed_ids: Set[str] = set()

    def _already_processed(self, db: Session, gmail_id: str) -> bool:
        if gmail_id in self._processed_ids:
            return True
        email = db.query(EmailMessage).filter(EmailMessage.gmail_message_id == gmail_id).first()
        if email is None:
            return False
        # An EmailMessage row is committed at fetch/normalize time, so its presence alone does not
        # mean processing completed. The completion marker is a run that reached DRAFT_CREATED --
        # the same predicate AgentOrchestrator.run_for_email uses to idempotently return a prior
        # run. Drafts are always committed before approval/send, so a sent or pending message
        # still matches here and is never processed twice; a run that died before drafting stays
        # eligible for a later poll.
        completed = (
            db.query(DraftReply)
            .join(AgentRun, DraftReply.run_id == AgentRun.run_id)
            .filter(AgentRun.email_id == email.id)
            .first()
        )
        return completed is not None

    def poll_once(self, db: Optional[Session] = None, *, max_results: int = 10) -> Dict[str, Any]:
        """Fetch list, process each unprocessed message with retry logic. Returns stats."""
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        run_id = generate_run_id()
        start = time.time()
        stats = {"run_id": run_id, "listed": 0, "processed": 0, "skipped_duplicate": 0, "failed_transient": 0, "failed_permanent": 0, "errors": []}
        try:
            create_audit_event(db, run_id=run_id, event_type="SCHEDULER_POLL_STARTED", status="success", safe_metadata={"query": self.query, "max_results": max_results})
        except Exception:
            pass

        try:
            messages = self.gmail.list_messages(query=self.query, max_results=max_results)
            stats["listed"] = len(messages) if messages else 0
        except Exception as e:
            msg = str(e)
            # Transient retry for listing itself
            retries = 0
            while _is_transient(msg) and retries < self.max_retries:
                retries += 1
                log.warning(f"Poll list transient error, retry {retries}/{self.max_retries}: {type(e).__name__}")
                time.sleep(self.backoff * retries)
                try:
                    messages = self.gmail.list_messages(query=self.query, max_results=max_results)
                    stats["listed"] = len(messages) if messages else 0
                    stats["failed_transient"] += 0  # cleared
                    msg = ""
                    break
                except Exception as e2:
                    msg = str(e2)
                    e = e2
                    continue
            if msg:
                stats["errors"].append(f"list_failed: {msg[:200]}")
                try:
                    create_audit_event(db, run_id=run_id, event_type="SCHEDULER_POLL_FAILED", status="failed", safe_metadata={"error": msg[:200], "retries": retries})
                except Exception:
                    pass
                if close_db:
                    db.close()
                return stats

        for item in messages or []:
            gmail_id = item.get("id")
            if not gmail_id:
                continue
            if self._already_processed(db, gmail_id):
                stats["skipped_duplicate"] += 1
                # Audit duplicate skip internally via run_id trace
                try:
                    create_audit_event(db, run_id=run_id, event_type="SCHEDULER_SKIP_DUPLICATE", status="success", safe_metadata={"gmail_id": gmail_id})
                except Exception:
                    pass
                continue
            # Process with bounded retries for transient errors
            attempt = 0
            while True:
                try:
                    # Use orchestrator to fetch+normalize+classify->draft (stops at pending_approval, never bypass gate)
                    self.orch.run_for_gmail_message_id(db, gmail_message_id=gmail_id)
                    stats["processed"] += 1
                    self._processed_ids.add(gmail_id)
                    try:
                        create_audit_event(db, run_id=run_id, event_type="SCHEDULER_MESSAGE_PROCESSED", status="success", safe_metadata={"gmail_id": gmail_id})
                    except Exception:
                        pass
                    break
                except Exception as e:
                    msg = str(e)
                    if _is_permanent(msg) or not _is_transient(msg):
                        # Permanent or non-transient: do not retry
                        stats["failed_permanent"] += 1
                        stats["errors"].append(f"{gmail_id}: {msg[:150]}")
                        try:
                            create_audit_event(db, run_id=run_id, event_type="SCHEDULER_MESSAGE_FAILED", status="failed", safe_metadata={"gmail_id": gmail_id, "error_type": type(e).__name__, "permanent": True})
                        except Exception:
                            pass
                        # Deliberately NOT added to _processed_ids: a permanent failure persists
                        # nothing, so DB idempotency must leave this id eligible for a later poll
                        # instead of reporting it forever as skipped_duplicate.
                        break
                    else:
                        # Transient: retry bounded
                        if attempt >= self.max_retries:
                            stats["failed_transient"] += 1
                            stats["errors"].append(f"{gmail_id} transient exceeded: {msg[:150]}")
                            try:
                                create_audit_event(db, run_id=run_id, event_type="SCHEDULER_MESSAGE_FAILED", status="failed", safe_metadata={"gmail_id": gmail_id, "error_type": type(e).__name__, "retries": attempt})
                            except Exception:
                                pass
                            break
                        attempt += 1
                        log.warning(f"Transient error for {gmail_id}, retry {attempt}/{self.max_retries}: {type(e).__name__}")
                        time.sleep(self.backoff * attempt)
                        continue

        # Final audit
        elapsed = time.time() - start
        try:
            create_audit_event(db, run_id=run_id, event_type="SCHEDULER_POLL_COMPLETED", status="success", safe_metadata={"processed": stats["processed"], "skipped": stats["skipped_duplicate"], "listed": stats["listed"], "failed_transient": stats["failed_transient"], "failed_permanent": stats["failed_permanent"], "elapsed_seconds": round(elapsed, 3)})
        except Exception:
            pass
        stats["elapsed_seconds"] = round(elapsed, 3)
        if close_db:
            db.close()
        log.info("Poll once completed", extra={"extra_data": {"run_id": run_id, "processed": stats["processed"], "skipped": stats["skipped_duplicate"]}})
        return stats

    def clear_processed_cache(self):
        self._processed_ids.clear()

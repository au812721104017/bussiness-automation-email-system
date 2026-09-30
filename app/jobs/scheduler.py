import time
import threading
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.logging import get_logger
from app.jobs.poller import PollerService
from app.db.session import SessionLocal

log = get_logger(__name__)

class SchedulerService:
    """Background polling with configurable interval, safe for local dev, graceful shutdown, disabled by default in tests."""

    def __init__(self, poller: Optional[PollerService] = None, interval_seconds: Optional[int] = None, enabled: Optional[bool] = None):
        s = get_settings()
        self.interval = interval_seconds if interval_seconds is not None else s.scheduler_interval_seconds
        self.enabled = enabled if enabled is not None else s.scheduler_enabled
        # In testing env, force disabled unless explicitly enabled
        if s.env == "testing" and enabled is None:
            self.enabled = False
        self.poller = poller or PollerService(max_retries=s.scheduler_max_retries, backoff=s.scheduler_retry_backoff_seconds, query=s.scheduler_query)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    def start(self) -> bool:
        if self._running:
            log.warning("Scheduler already running")
            return False
        if not self.enabled:
            log.info("Scheduler not enabled — set SCHEDULER_ENABLED=true to enable (disabled by default in tests)")
            return False
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="email-poller")
        self._thread.start()
        log.info(f"Scheduler started with interval {self.interval}s")
        return True

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                db = SessionLocal()
                stats = self.poller.poll_once(db)
                db.close()
                log.info("Scheduled poll completed", extra={"extra_data": {"stats": stats}})
            except Exception as e:
                log.error(f"Scheduler loop error: {type(e).__name__}: {str(e)[:200]}")
            # Wait with early exit on stop
            self._stop_event.wait(self.interval)
        self._running = False
        log.info("Scheduler loop exited")

    def stop(self, timeout: float = 5.0) -> bool:
        if not self._running:
            return False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("Scheduler thread did not exit in time")
                return False
        self._running = False
        log.info("Scheduler stopped gracefully")
        return True

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def trigger_once(self, db: Optional[Session] = None) -> Dict[str, Any]:
        """Synchronous single poll for manual trigger or tests — respects dedup/retry/audit but not interval."""
        return self.poller.poll_once(db)

    def status(self) -> Dict[str, Any]:
        s = get_settings()
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval,
            "running": self.is_running(),
            "env": s.env,
            "query": s.scheduler_query,
            "gate": "approval_required — scheduler never bypasses gate (stops at pending_approval)",
        }

# Global singleton (disabled by default)
_scheduler: Optional[SchedulerService] = None

def get_scheduler() -> SchedulerService:
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService()
    return _scheduler

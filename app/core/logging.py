import json
import logging
import sys
import uuid
from datetime import datetime, timezone

class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Redact PII/tokens from message before logging (Phase 12: PII-safe)
        msg = record.getMessage()
        # Basic redaction without importing heavy deps
        import re
        msg = re.sub(r"([a-zA-Z0-9._%+\-]+)@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", r"***@\2", msg)
        msg = re.sub(r"sk-[a-zA-Z0-9]{8,}", "sk-***", msg)
        msg = re.sub(r"ya29\.[a-zA-Z0-9_\-]+", "ya29.***", msg)
        # Truncate long messages that might contain body
        if len(msg) > 1000:
            msg = msg[:1000] + " [truncated for PII safety]"
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": msg,
            "module": record.module,
            "funcName": record.funcName,
            "line": record.lineno,
        }
        if hasattr(record, "run_id"):
            log_data["run_id"] = record.run_id
        if hasattr(record, "extra_data"):
            # Redact extra_data if it contains bodies/tokens (Phase 12: ensure no full body logged)
            extra = getattr(record, "extra_data", None)
            if isinstance(extra, dict):
                # Shallow copy and truncate any string values >500 chars
                safe_extra = {}
                for k, v in extra.items():
                    if isinstance(v, str) and len(v) > 500:
                        safe_extra[k] = v[:500] + " [truncated]"
                    else:
                        safe_extra[k] = v
                log_data["extra"] = safe_extra
            else:
                log_data["extra"] = extra
        if record.exc_info and record.exc_info[0] is not None:
            log_data["exception"] = self.formatException(record.exc_info)[:2000]
        return json.dumps(log_data, ensure_ascii=False)


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    level = getattr(logging, log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Reduce noisy libs
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logger = logging.getLogger("ai_email_agent")
    logger.setLevel(level)
    return logger


def get_logger(name: str = "ai_email_agent") -> logging.Logger:
    return logging.getLogger(name)


def generate_run_id() -> str:
    return str(uuid.uuid4())

import base64
import os
import re
import json
import html as html_lib
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from email.utils import parsedate_to_datetime

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.email import NormalizedEmail

log = get_logger(__name__)
settings = get_settings()

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

EMAIL_RE = re.compile(r"<([^>]+)>|([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})")

def _extract_email(raw: str) -> Optional[str]:
    if not raw:
        return None
    m = EMAIL_RE.search(raw)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip().lower() or None

def _decode_b64(data: str) -> str:
    # Gmail body is base64url without padding
    if not data:
        return ""
    # Pad
    data = data.replace("-", "+").replace("_", "/")
    pad = len(data) % 4
    if pad:
        data += "=" * (4 - pad)
    try:
        return base64.b64decode(data).decode("utf-8", errors="ignore")
    except Exception:
        return ""

def _strip_html(html_text: str) -> str:
    # Minimal sanitization — strip tags, unescape
    if not html_text:
        return ""
    # Remove script/style
    html_text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:10000]

def _parse_headers(headers: List[Dict[str, str]]) -> Dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in headers if h.get("name")}

def _extract_body(payload: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Return (text, html) from Gmail payload, handling multipart recursively."""
    if not payload:
        return None, None
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data")
    parts = payload.get("parts", [])

    if mime == "text/plain" and data:
        return _decode_b64(data), None
    if mime == "text/html" and data:
        html_body = _decode_b64(data)
        return None, html_body
    # multipart
    text_parts = []
    html_parts = []
    for part in parts:
        t, h = _extract_body(part)
        if t:
            text_parts.append(t)
        if h:
            html_parts.append(h)
    text = "\n".join(text_parts) if text_parts else None
    html_text = "\n".join(html_parts) if html_parts else None
    # Also check nested body data at current level for multipart/mixed wrapping
    if not text and not html_text and data:
        # Could be plain fallback
        decoded = _decode_b64(data)
        if decoded:
            if mime.startswith("text/"):
                text = decoded
            else:
                text = decoded
    return text, html_text


class GmailService:
    """
    Abstraction over Google SDK details. Rest of app must not import googleapiclient directly.
    Read-only only in Phase 3: list, fetch, normalize, thread. No send/modify.
    Injectable client for mocking tests: pass gmail_client (mock) to constructor.
    """
    def __init__(self, gmail_client: Optional[Any] = None):
        self._client = gmail_client  # googleapiclient.discovery.build("gmail","v1",...)
        self._settings = settings

    # ---------- Auth (Phase 3: secure, tokens from local storage, never logged) ----------
    def is_configured(self) -> bool:
        return bool(self._settings.gmail_client_id and self._settings.gmail_client_secret)

    def get_auth_url(self) -> Dict[str, str]:
        if not self.is_configured():
            return {"configured": False, "detail": "Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env (see GMAIL_SETUP.md)"}
        # Real OAuth URL generation requires google-auth-oauthlib; return template if lib missing
        try:
            from google_auth_oauthlib.flow import Flow
            scopes = [s.strip() for s in self._settings.gmail_scopes.split(",") if s.strip()]
            if not scopes:
                scopes = [GMAIL_READONLY_SCOPE]
            flow = Flow.from_client_config(
                {
                    "web": {
                        "client_id": self._settings.gmail_client_id,
                        "client_secret": self._settings.gmail_client_secret,
                        "redirect_uris": [self._settings.gmail_redirect_uri],
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                },
                scopes=scopes,
            )
            flow.redirect_uri = self._settings.gmail_redirect_uri
            url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
            log.info("Gmail auth URL generated", extra={"extra_data": {"scopes": scopes, "redirect_uri": self._settings.gmail_redirect_uri}})
            return {"configured": True, "auth_url": url, "scopes": scopes}
        except ImportError:
            log.warning("google-auth-oauthlib not installed; returning placeholder auth URL")
            return {"configured": True, "auth_url": f"https://accounts.google.com/o/oauth2/auth?client_id={self._settings.gmail_client_id}&redirect_uri={self._settings.gmail_redirect_uri}&scope={GMAIL_READONLY_SCOPE}&response_type=code", "scopes": [GMAIL_READONLY_SCOPE], "note": "Install google-auth-oauthlib google-api-python-client for full flow"}
        except Exception as e:
            log.error(f"Auth URL error: {type(e).__name__}")
            return {"configured": False, "detail": str(e)[:200]}

    def is_authenticated(self) -> bool:
        return os.path.exists(self._settings.gmail_token_path)

    def _get_client(self):
        if self._client is not None:
            return self._client
        # Lazy build from token.json — never log token
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            if not os.path.exists(self._settings.gmail_token_path):
                raise FileNotFoundError(f"Token not found at {self._settings.gmail_token_path}. Complete OAuth first.")
            creds = Credentials.from_authorized_user_file(self._settings.gmail_token_path, scopes=[s.strip() for s in self._settings.gmail_scopes.split(",") if s.strip()] or [GMAIL_READONLY_SCOPE])
            # Redact token in logs
            log.info("Gmail client built from token file", extra={"extra_data": {"token_path": self._settings.gmail_token_path, "has_token": bool(creds.token)}})  # never log token value
            return build("gmail", "v1", credentials=creds)
        except ImportError as e:
            raise RuntimeError("google-api-python-client not installed. Install with: pip install google-api-python-client google-auth google-auth-oauthlib") from e
        except Exception as e:
            raise RuntimeError(f"Gmail client not available: {type(e).__name__}: {str(e)[:200]}") from e

    # ---------- Read-only operations ----------
    def list_messages(self, query: Optional[str] = None, max_results: int = 10) -> List[Dict[str, Any]]:
        client = self._get_client()
        try:
            # Never log full query if it contains PII; log safe length
            log.info("Gmail list_messages", extra={"extra_data": {"max_results": max_results, "has_query": bool(query), "query_len": len(query) if query else 0}})
            result = client.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
            messages = result.get("messages", []) or []
            # Fetch metadata only for list? Return minimal
            log.info("Gmail list result", extra={"extra_data": {"count": len(messages)}})
            return messages
        except Exception as e:
            log.error(f"Gmail list failed: {type(e).__name__}")
            raise RuntimeError(f"Gmail list failed: {str(e)[:300]}") from e

    def get_message(self, message_id: str) -> Dict[str, Any]:
        if not message_id or not message_id.strip():
            raise ValueError("message_id is required")
        client = self._get_client()
        try:
            log.info("Gmail get_message", extra={"extra_data": {"message_id": message_id}})
            msg = client.users().messages().get(userId="me", id=message_id, format="full").execute()
            return msg
        except Exception as e:
            log.error(f"Gmail get_message failed: {type(e).__name__}")
            raise RuntimeError(f"Gmail get failed: {str(e)[:300]}") from e

    def get_thread(self, thread_id: str) -> Dict[str, Any]:
        if not thread_id or not thread_id.strip():
            raise ValueError("thread_id is required")
        client = self._get_client()
        try:
            log.info("Gmail get_thread", extra={"extra_data": {"thread_id": thread_id}})
            thread = client.users().threads().get(userId="me", id=thread_id, format="full").execute()
            return thread
        except Exception as e:
            log.error(f"Gmail get_thread failed: {type(e).__name__}")
            raise RuntimeError(f"Gmail thread get failed: {str(e)[:300]}") from e

    # ---------- Normalization (safe, never log full body) ----------
    def normalize_message(self, gmail_message: Dict[str, Any]) -> NormalizedEmail:
        if not gmail_message:
            raise ValueError("gmail_message is required")
        msg_id = str(gmail_message.get("id") or gmail_message.get("message_id") or "")
        if not msg_id:
            raise ValueError("gmail_message missing id")
        thread_id = gmail_message.get("threadId")
        snippet = gmail_message.get("snippet")
        internal_date = gmail_message.get("internalDate")
        size = gmail_message.get("sizeEstimate")
        payload = gmail_message.get("payload", {})
        headers_list = payload.get("headers", []) if payload else []
        headers = _parse_headers(headers_list)
        subject = headers.get("subject")
        from_raw = headers.get("from", "")
        to_raw = headers.get("to", "")
        cc_raw = headers.get("cc", "")
        bcc_raw = headers.get("bcc", "")
        date_raw = headers.get("date", "")

        # Recipients parsing (split by comma)
        def _split(raw: str) -> List[str]:
            if not raw:
                return []
            return [p.strip() for p in raw.split(",") if p.strip()]
        recipients = _split(to_raw)
        cc = _split(cc_raw)
        bcc = _split(bcc_raw)
        sender_email = _extract_email(from_raw)

        # Body extraction
        body_text, body_html = _extract_body(payload)
        if not body_text and body_html:
            # Fallback: strip HTML to text (never invent)
            body_text = _strip_html(body_html)
        # Never log full bodies; caller may store snippet only in audit
        if body_text and len(body_text) > 20000:
            body_text = body_text[:20000] + " [truncated]"
        if body_html and len(body_html) > 50000:
            body_html = body_html[:50000] + " [truncated]"

        # Date parsing
        received_at = None
        if date_raw:
            try:
                dt = parsedate_to_datetime(date_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                received_at = dt
            except Exception:
                received_at = None
        if not received_at and internal_date:
            try:
                ms = int(internal_date)
                received_at = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
            except Exception:
                pass

        log.info("Gmail normalized", extra={"extra_data": {"message_id": msg_id, "thread_id": thread_id, "has_body": bool(body_text), "body_len": len(body_text) if body_text else 0, "sender_domain": sender_email.split("@")[-1] if sender_email and "@" in sender_email else None}})

        return NormalizedEmail(
            message_id=msg_id,
            thread_id=thread_id,
            sender=from_raw,
            sender_email=sender_email,
            recipients=recipients,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            snippet=snippet,
            internal_date=int(internal_date) if internal_date and str(internal_date).isdigit() else None,
            received_at=received_at,
            headers=headers,
            size_estimate=int(size) if size else None,
        )

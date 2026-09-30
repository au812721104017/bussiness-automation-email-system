import base64
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.services.gmail_service import GmailService
from app.api.gmail import get_gmail_service

def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

# --- Sample Gmail message payloads ---
SAMPLE_SIMPLE = {
    "id": "msg123",
    "threadId": "threadA",
    "snippet": "Hello world snippet",
    "internalDate": "1700000000000",
    "sizeEstimate": 1234,
    "payload": {
        "mimeType": "text/plain",
        "headers": [
            {"name": "From", "value": "Alice <alice@example.com>"},
            {"name": "To", "value": "me@test.com, bob@test.com"},
            {"name": "Subject", "value": "Internship inquiry"},
            {"name": "Date", "value": "Mon, 20 Nov 2023 10:00:00 +0000"},
        ],
        "body": {"data": _b64("Hello, I need internship info")},
    },
}

SAMPLE_MULTIPART = {
    "id": "msg456",
    "threadId": "threadB",
    "snippet": "multipart html",
    "payload": {
        "mimeType": "multipart/alternative",
        "headers": [
            {"name": "From", "value": "Bob@example.com"},
            {"name": "Subject", "value": "Pricing question"},
            {"name": "To", "value": "me@test.com"},
        ],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("plain text body")}},
            {"mimeType": "text/html", "body": {"data": _b64("<html><body><p>html body</p><script>alert(1)</script></body></html>")}},
        ],
    },
}

SAMPLE_HTML_ONLY = {
    "id": "msg789",
    "threadId": "threadC",
    "snippet": "html only",
    "payload": {
        "mimeType": "text/html",
        "headers": [
            {"name": "From", "value": "Carol <carol@test.org>"},
            {"name": "To", "value": "me@test.com"},
            {"name": "Subject", "value": "Meeting request"},
            {"name": "Date", "value": "invalid date"},
        ],
        "body": {"data": _b64("<h1>Hello</h1><p>Meeting tomorrow?</p>")},
    },
}

def test_normalize_simple_text():
    svc = GmailService()
    norm = svc.normalize_message(SAMPLE_SIMPLE)
    assert norm.message_id == "msg123"
    assert norm.thread_id == "threadA"
    assert norm.sender == "Alice <alice@example.com>"
    assert norm.sender_email == "alice@example.com"
    assert norm.subject == "Internship inquiry"
    assert "internship" in norm.body_text.lower()
    assert norm.snippet == "Hello world snippet"
    assert norm.recipients == ["me@test.com", "bob@test.com"]
    assert norm.received_at is not None

def test_normalize_multipart_prefers_text_strips_html():
    svc = GmailService()
    norm = svc.normalize_message(SAMPLE_MULTIPART)
    assert norm.body_text == "plain text body"
    # html should still be captured but script stripped in fallback path not used here
    assert norm.body_html is not None
    assert "alert" in norm.body_html  # raw html kept
    assert norm.subject == "Pricing question"

def test_normalize_html_only_fallback_strips_tags():
    svc = GmailService()
    norm = svc.normalize_message(SAMPLE_HTML_ONLY)
    # body_html kept, body_text derived via stripping
    assert norm.body_html is not None
    assert norm.body_text is not None
    assert "Hello" in norm.body_text
    assert "<h1>" not in norm.body_text
    assert "script" not in norm.body_text.lower()

def test_normalize_missing_id_raises():
    svc = GmailService()
    with pytest.raises(ValueError):
        svc.normalize_message({})
    with pytest.raises(ValueError):
        svc.normalize_message(None)

def test_normalize_empty_headers_safe():
    msg = {"id": "x", "threadId": "t", "payload": {"headers": [], "body": {"data": _b64("hi")}, "mimeType": "text/plain"}}
    svc = GmailService()
    norm = svc.normalize_message(msg)
    assert norm.message_id == "x"
    assert norm.sender == ""
    assert norm.sender_email is None

def test_service_no_send_method():
    svc = GmailService()
    assert not hasattr(svc, "send_message") and not hasattr(svc, "send")  # Phase 3: must not send

def test_gmail_auth_url_not_configured_returns_hint():
    # Use fresh settings that have placeholder creds -> is_configured false if we inject None
    svc = GmailService()
    svc._settings.gmail_client_id = None
    svc._settings.gmail_client_secret = None
    res = svc.get_auth_url()
    assert res["configured"] is False

# --- API tests with mocked client (no network) ---
def _make_mock_client():
    mock = MagicMock()
    # users().messages().list().execute()
    mock.users.return_value.messages.return_value.list.return_value.execute.return_value = {"messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}]}
    mock.users.return_value.messages.return_value.get.return_value.execute.return_value = SAMPLE_SIMPLE
    mock.users.return_value.threads.return_value.get.return_value.execute.return_value = {"id": "threadA", "historyId": "100", "messages": [SAMPLE_SIMPLE, SAMPLE_MULTIPART]}
    return mock

@pytest.fixture
def client_with_mock():
    mock = _make_mock_client()
    svc = GmailService(gmail_client=mock)
    app.dependency_overrides[get_gmail_service] = lambda: svc
    with TestClient(app) as c:
        yield c, mock
    app.dependency_overrides.clear()

def test_api_list_messages_mocked(client_with_mock):
    c, _ = client_with_mock
    resp = c.get("/gmail/messages?max_results=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert "messages" in data

def test_api_get_message_normalizes(client_with_mock):
    c, _ = client_with_mock
    resp = c.get("/gmail/messages/msg123")
    assert resp.status_code == 200
    data = resp.json()
    assert data["raw_id"] == "msg123"
    assert data["normalized"]["message_id"] == "msg123"
    assert data["normalized"]["sender_email"] == "alice@example.com"
    assert "internship" in data["normalized"]["body_text"].lower()

def test_api_get_thread_mocked(client_with_mock):
    c, _ = client_with_mock
    resp = c.get("/gmail/threads/threadA")
    assert resp.status_code == 200
    data = resp.json()
    assert data["thread_id"] == "threadA"
    assert data["message_count"] == 2
    assert "normalized_preview" in data
    assert len(data["normalized_preview"]) == 2

def test_api_list_error_handling():
    failing = MagicMock()
    failing.users.return_value.messages.return_value.list.return_value.execute.side_effect = Exception("network down")
    app.dependency_overrides[get_gmail_service] = lambda: GmailService(gmail_client=failing)
    with TestClient(app) as c:
        resp = c.get("/gmail/messages")
        assert resp.status_code == 502
        assert "network down" in resp.json()["detail"] or "failed" in resp.json()["detail"].lower()
    app.dependency_overrides.clear()

def test_normalize_never_logs_tokens(caplog):
    # Ensure service does not leak token values in logs (we check no token substring)
    svc = GmailService()
    svc.normalize_message(SAMPLE_SIMPLE)
    # Body not leaked fully? Our log only has body_len, not full body, so caplog should not contain full body
    for rec in caplog.records:
        assert "sk-" not in rec.getMessage()
        assert "ya29." not in rec.getMessage()

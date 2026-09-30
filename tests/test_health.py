from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_returns_healthy():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "phase" in data
    assert data["phase"] == "15_final"
    assert "app" in data
    assert "version" in data
    assert "timestamp" in data
    assert data["checks"]["api"] == "ok"
    assert data["checks"]["config"] == "loaded"

def test_health_has_useful_fields():
    resp = client.get("/health")
    data = resp.json()
    # Must be useful: not empty, not error
    assert data["message"]
    assert data["env"] in {"development", "testing", "staging", "production"}

def test_root_exists():
    resp = client.get("/")
    assert resp.status_code == 200

def test_docs_available():
    # FastAPI OpenAPI docs
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]

def test_openapi_schema():
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "openapi" in schema
    assert "paths" in schema
    assert "/health" in schema["paths"]

def test_404_handling():
    resp = client.get("/nonexistent-route-xyz")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "http_error"
    assert body["status_code"] == 404
    assert "path" in body

def test_email_send_validation():
    # Invalid email should be 422 (Phase 1 input validation)
    resp = client.post("/email/send", params={"to": "invalid-email", "subject": "hi", "body": "test"})
    assert resp.status_code == 422
    assert "Invalid email" in resp.json()["detail"]

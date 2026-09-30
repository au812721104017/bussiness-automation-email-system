import os
import pytest

# Make scheduling disabled by default during tests (per spec)
os.environ.setdefault("ENV", "testing")

from fastapi.testclient import TestClient

@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c

import pytest
from pydantic import ValidationError
from app.core.config import Settings

def test_settings_defaults():
    s = Settings(_env_file=None, database_url="sqlite:///./test.db", openai_api_key=None)
    assert s.app_name
    assert s.phase == "15_final"
    assert s.database_url.startswith("sqlite://")

def test_invalid_database_url_fails():
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, database_url="invalid://bad")
    assert "database_url" in str(exc.value).lower() or "DATABASE_URL" in str(exc.value)

def test_invalid_port_fails():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="sqlite:///./test.db", port=99999)

def test_invalid_log_level_fails():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="sqlite:///./test.db", log_level="VERBOSE")

def test_invalid_env_fails():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="sqlite:///./test.db", env="unknown")

def test_env_overrides():
    s = Settings(_env_file=None, database_url="sqlite:///./override.db", env="testing", log_level="DEBUG")
    assert s.env == "testing"
    assert s.log_level == "DEBUG"
    assert s.database_url == "sqlite:///./override.db"

def test_scheduler_query_default_is_read_state_agnostic_inbox_only():
    s = Settings(_env_file=None, database_url="sqlite:///./test.db")
    q = s.scheduler_query
    assert "{window}" not in q
    assert "in:inbox" in q
    assert "-in:sent" in q and "-in:spam" in q and "-in:trash" in q
    assert "is:unread" not in q and "is:read" not in q
    assert f"newer_than:{s.scheduler_newer_than_days}d" in q

def test_scheduler_query_window_is_configurable():
    s = Settings(_env_file=None, database_url="sqlite:///./test.db", scheduler_newer_than_days=7)
    assert "{window}" not in s.scheduler_query
    assert "newer_than:7d" in s.scheduler_query

def test_scheduler_query_explicit_override_is_preserved():
    s = Settings(_env_file=None, database_url="sqlite:///./test.db", scheduler_query="in:inbox newer_than:30d")
    assert s.scheduler_query == "in:inbox newer_than:30d"

def test_invalid_newer_than_days_fails():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="sqlite:///./test.db", scheduler_newer_than_days=0)

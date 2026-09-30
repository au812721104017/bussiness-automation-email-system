import json
import logging
from app.core.logging import StructuredFormatter, generate_run_id

def test_run_id_generation():
    a = generate_run_id()
    b = generate_run_id()
    assert a != b
    assert len(a) == 36  # uuid4
    # valid uuid
    import uuid
    uuid.UUID(a)

def test_structured_formatter_outputs_json():
    fmt = StructuredFormatter()
    record = logging.LogRecord(name="test", level=logging.INFO, pathname=__file__, lineno=10, msg="hello world", args=(), exc_info=None)
    out = fmt.format(record)
    data = json.loads(out)
    assert data["message"] == "hello world"
    assert data["level"] == "INFO"
    assert "timestamp" in data
    assert data["logger"] == "test"

def test_formatter_includes_run_id():
    fmt = StructuredFormatter()
    record = logging.LogRecord(name="test", level=logging.INFO, pathname=__file__, lineno=10, msg="msg", args=(), exc_info=None)
    record.run_id = "test-run-123"
    out = json.loads(fmt.format(record))
    assert out["run_id"] == "test-run-123"

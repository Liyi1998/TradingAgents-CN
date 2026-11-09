"""Guard real-I/O regression tests against accidental production connections."""
import os


def require_isolated_database():
    from app.core.config import settings

    assert os.environ.get("TRADINGAGENTS_ISOLATED_TESTS") == "1", "explicit isolated test run required"
    assert settings.MONGODB_HOST in {"localhost", "127.0.0.1"}
    assert settings.MONGODB_PORT != 27017, "production/default MongoDB port is forbidden"
    assert any(word in settings.MONGO_DB.lower() for word in ("test", "validation"))

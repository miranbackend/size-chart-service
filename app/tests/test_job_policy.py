import pytest

from worker import Outcome, database_url, result_for


@pytest.mark.parametrize("status,result", [
    ("found", "found"),
    ("not_found", "not_found"),
    ("image_chart", "pending_ocr"),   # image found, never stored as a chart
    ("blocked", "blocked"),
    ("failed", "failed"),
    ("something_else", "failed"),
])
def test_result_for(status, result):
    assert result_for(Outcome(status)) == result


def test_database_url_rejects_asyncpg(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    with pytest.raises(SystemExit):
        database_url()
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db?sslmode=require")
    assert database_url().startswith("postgresql://")


@pytest.mark.parametrize("mode,enabled,expected", [
    (None, None, False),     # nothing set -> stopped
    ("0", None, False),
    ("1", None, True),
    ("0", "true", False),    # WORKER_MODE wins over WORKER_ENABLED
    (None, "true", True),    # old switch still works
    (None, "false", False),
    ("2", None, False),      # anything else -> stopped, never running
    ("yes", None, False),
])
def test_worker_mode(monkeypatch, mode, enabled, expected):
    from worker import _env_worker_enabled
    for name, value in (("WORKER_MODE", mode), ("WORKER_ENABLED", enabled)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert _env_worker_enabled() is expected

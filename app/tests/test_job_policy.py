import pytest

from extraction import Chart
from worker import Outcome, database_url, plan_job


def test_found_finishes_with_zero_retry():
    chart = Chart(sizes={}, unit="inch", method="embedded_json", source="myntra", confidence=0.95)
    plan = plan_job(Outcome("found", "https://x", chart=chart), attempts=1, max_attempts=5)
    assert (plan.job_status, plan.product_status, plan.job_error, plan.retry_seconds) == \
        ("found", "found", None, 0)


def test_not_found_has_null_job_error_and_long_retry():
    plan = plan_job(Outcome("not_found", error="no chart"), attempts=1, max_attempts=5)
    assert plan.job_status == "not_found" and plan.job_error is None
    assert plan.product_error == "no chart"
    assert plan.retry_seconds >= 24 * 3600


def test_image_chart_is_pending_ocr_never_found():
    plan = plan_job(Outcome("image_chart", images=[{"url": "https://x/chart.jpg"}]), 1, 5)
    assert plan.job_status == "not_found"          # allowed by the job CHECK constraint
    assert plan.product_status == "pending_ocr"
    assert "https://x/chart.jpg" in plan.product_error


def test_blocked_backs_off_and_keeps_http_status():
    first = plan_job(Outcome("blocked", error="captcha", http_status=429), 1, 5)
    second = plan_job(Outcome("blocked", error="captcha", http_status=429), 2, 5)
    assert first.job_status == "blocked" and first.http_status == 429
    assert second.retry_seconds == 2 * first.retry_seconds


def test_failed_backs_off_then_parks_when_exhausted():
    plans = [plan_job(Outcome("failed", error="boom"), n, 5).retry_seconds for n in (1, 2, 3, 5)]
    assert plans[0] < plans[1] < plans[2]
    assert plans[3] == 7 * 24 * 3600


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

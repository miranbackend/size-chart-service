import pytest

from worker import Outcome, database_url, result_for


@pytest.mark.parametrize("status,result", [
    ("found", "found"),
    ("not_found", "not_found"),
    ("image_chart", "pending_ocr"),   # image found, never stored as a chart
    ("generic_chart", "generic_chart"),  # chart not tied to the product: not saved
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


def test_requests_to_one_store_are_paced(monkeypatch):
    import time
    import worker
    monkeypatch.setattr(worker.SETTINGS, "per_host_delay_ms", 200)
    worker._last_request.clear()
    start = time.monotonic()
    worker.wait_for_host("https://store-a.example.com/products/1")
    worker.wait_for_host("https://store-b.example.com/products/1")   # other store: no wait
    assert time.monotonic() - start < 0.1
    worker.wait_for_host("https://store-a.example.com/products/2")   # same store: waits
    assert time.monotonic() - start >= 0.19
    worker.wait_for_host("https://store-a.example.com/products/3", crawl_delay=0.4)
    assert time.monotonic() - start >= 0.58                           # robots Crawl-delay wins



def test_rate_limiter_halves_pauses_and_recovers(monkeypatch):
    import time
    import worker
    limiter = worker.AdaptiveRateLimiter(4.0)
    limiter.record(429, retry_after=0.3)
    assert limiter.rate == 2.0
    limiter.record(429, retry_after=0.3)        # same burst: not halved again
    assert limiter.rate == 2.0
    start = time.monotonic()
    limiter.acquire()                           # waits out the Retry-After pause
    assert time.monotonic() - start >= 0.25
    for _ in range(worker.AdaptiveRateLimiter.OK_STREAK_TO_SPEED_UP):
        limiter.record(200)
    assert limiter.rate == 2.25                 # creeps back up


def test_rate_limiter_spaces_requests():
    import time
    import worker
    limiter = worker.AdaptiveRateLimiter(10.0)  # 10 requests / second
    start = time.monotonic()
    for _ in range(6):
        limiter.acquire()
    assert time.monotonic() - start >= 0.45


@pytest.mark.parametrize("value,seconds", [("30", 30.0), (None, None), ("", None), ("soon", None)])
def test_parse_retry_after(value, seconds):
    from worker import parse_retry_after
    assert parse_retry_after(value) == seconds

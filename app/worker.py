"""
Size-chart worker.

    size_chart_jobs (claim_size_chart_jobs)
        -> resolve retailer URL          (url_resolution.py)
        -> fetch over HTTP, browser only when needed
        -> extract product-specific chart (extraction.py)
        -> gsa_product_measurement upsert + finish_size_chart_job
           + gsa_product status, in one transaction

Usage:
    python worker.py                                  # queue loop (needs WORKER_ENABLED=true)
    python worker.py extract-url URL [--title T]      # fetch + extract one URL, no DB
    python worker.py extract-file PATH --url URL      # extract a saved HTML file, no DB
    python worker.py dry-run --product-id ID          # read one product from DB, no writes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from extraction import (
    Chart,
    PageResult,
    RetailerAdapter,
    adapter_for,
    clean_text,
    extract_page,
    is_google_url,
    site_from_url,
)
from url_resolution import resolve_product_url


# ============================================================
# SETTINGS
# ============================================================

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}")


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    worker_id: str = field(default_factory=lambda: os.getenv("WORKER_ID") or socket.gethostname())
    # Off unless explicitly enabled, so a deploy never starts draining the queue by accident.
    worker_enabled: bool = field(default_factory=lambda: _env_bool("WORKER_ENABLED", False))
    batch_size: int = field(default_factory=lambda: _env_int("BATCH_SIZE", 10))
    poll_seconds: int = field(default_factory=lambda: _env_int("POLL_SECONDS", 10))
    nav_timeout_ms: int = field(default_factory=lambda: _env_int("NAV_TIMEOUT_MS", 45000))
    http_timeout_seconds: int = field(default_factory=lambda: _env_int("HTTP_TIMEOUT_SECONDS", 25))
    db_pool_size: int = field(default_factory=lambda: _env_int("DB_POOL_SIZE", 5))
    browser_wait_ms: int = field(default_factory=lambda: _env_int("BROWSER_WAIT_MS", 1500))
    max_chart_rows: int = field(default_factory=lambda: _env_int("MAX_CHART_ROWS", 100))
    max_attempts: int = field(default_factory=lambda: _env_int("MAX_ATTEMPTS", 5))
    max_html_bytes: int = field(default_factory=lambda: _env_int("MAX_HTML_BYTES", 8_000_000))
    browser_enabled: bool = field(default_factory=lambda: _env_bool("BROWSER_ENABLED", True))
    browser_restart_every: int = field(default_factory=lambda: _env_int("BROWSER_RESTART_EVERY", 50))
    # Fetching google.com/search from a datacenter mostly yields "unusual
    # traffic" pages; only enable if you are permitted to and it helps.
    google_http_resolution: bool = field(
        default_factory=lambda: _env_bool("GOOGLE_HTTP_RESOLUTION", False)
    )


SETTINGS = Settings()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

# Retry delays (seconds). failed/blocked back off exponentially per attempt.
NOT_FOUND_RETRY = 7 * 24 * 3600
PENDING_OCR_RETRY = 7 * 24 * 3600
UNRESOLVED_RETRY = 7 * 24 * 3600
FAILED_RETRY_BASE, FAILED_RETRY_CAP = 30 * 60, 24 * 3600
BLOCKED_RETRY_BASE, BLOCKED_RETRY_CAP = 6 * 3600, 72 * 3600
EXHAUSTED_RETRY = 7 * 24 * 3600
MAX_ERROR_CHARS = 2000


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,  # stdout stays clean for the CLI's JSON output
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("size-chart-worker")
_LOG_JSON = os.getenv("LOG_FORMAT", "text").lower() == "json"


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """One log line per event: 'event k=v ...' or JSON with LOG_FORMAT=json."""
    fields = {k: v for k, v in fields.items() if v is not None}
    if _LOG_JSON:
        log.log(level, json.dumps({"event": event, **fields}, default=str))
    else:
        log.log(level, "%s %s", event, " ".join(f"{k}={v}" for k, v in fields.items()))


# ============================================================
# DATABASE
# ============================================================

_pool: ConnectionPool | None = None


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is required")
    scheme = url.split("://", 1)[0].lower()
    if "+" in scheme or scheme not in ("postgres", "postgresql"):
        raise SystemExit(
            f"DATABASE_URL scheme {scheme!r} is not usable by psycopg; "
            "use postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require"
        )
    return url


def get_pool() -> ConnectionPool:
    """Created lazily so a disabled worker holds no DB connections."""
    global _pool
    if _pool is None:
        url = database_url()
        # The pool only reports "couldn't get a connection"; one direct
        # attempt first surfaces the real cause (firewall, auth, SSL, host).
        try:
            psycopg.connect(url, connect_timeout=15).close()
        except psycopg.OperationalError as exc:
            raise SystemExit(f"cannot connect to DATABASE_URL: {clean_text(exc)}") from None
        _pool = ConnectionPool(
            url,
            min_size=1,
            max_size=max(1, SETTINGS.db_pool_size),
            kwargs={"autocommit": True},
            check=ConnectionPool.check_connection,
            timeout=30,
            name="size-chart",
            open=True,
        )
        _pool.wait(timeout=30)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# ============================================================
# HTTP
# ============================================================

_http_client: httpx.Client | None = None


def get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            headers=HTTP_HEADERS,
            follow_redirects=True,
            max_redirects=10,
            timeout=httpx.Timeout(SETTINGS.http_timeout_seconds, connect=10),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _http_client


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None = None
    html: str = ""
    error: str | None = None
    truncated: bool = False


_TEXT_CONTENT_TYPES = ("html", "json", "text/plain", "xml")


def fetch_http(client: httpx.Client, url: str) -> FetchResult:
    """GET with a byte cap so a huge or binary response cannot exhaust memory."""
    try:
        with client.stream("GET", url) as response:
            final_url = str(response.url)
            ctype = response.headers.get("content-type", "").lower()
            if ctype and not any(t in ctype for t in _TEXT_CONTENT_TYPES):
                return FetchResult(url, final_url, response.status_code,
                                   error=f"unsupported content-type {ctype[:60]}")
            chunks, total, truncated = [], 0, False
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > SETTINGS.max_html_bytes:
                    truncated = True
                    break
                chunks.append(chunk)
            html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            return FetchResult(url, final_url, response.status_code, html, truncated=truncated)
    except httpx.TooManyRedirects:
        return FetchResult(url, url, error="too_many_redirects")
    except httpx.HTTPError as exc:
        return FetchResult(url, url, error=f"{type(exc).__name__}: {exc}"[:500])


# ============================================================
# BROWSER
# ============================================================

class BrowserFetcher:
    """
    One Chromium per worker process, a fresh context per page, restarted
    every BROWSER_RESTART_EVERY pages to bound memory.
    """

    _BLOCKED_RESOURCES = frozenset({"image", "media", "font"})

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._pages = 0

    def _ensure(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        self.close()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        return self._browser

    def fetch(self, url: str, adapter: RetailerAdapter) -> FetchResult:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        browser = self._ensure()
        context = browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
        )
        context.set_default_timeout(SETTINGS.nav_timeout_ms)
        try:
            page = context.new_page()
            # Images/fonts are not needed: image charts are found from the DOM.
            page.route("**/*", lambda route: route.abort()
                       if route.request.resource_type in self._BLOCKED_RESOURCES
                       else route.continue_())
            response = page.goto(url, wait_until="domcontentloaded", timeout=SETTINGS.nav_timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=SETTINGS.browser_wait_ms)
            except PlaywrightTimeoutError:
                pass  # busy pages never go idle; the DOM is already loaded
            self._open_size_guide(page, adapter)
            html = page.content()
            truncated = len(html) > SETTINGS.max_html_bytes
            return FetchResult(url, page.url, response.status if response else None,
                               html[: SETTINGS.max_html_bytes], truncated=truncated)
        finally:
            context.close()
            self._pages += 1
            if self._pages >= SETTINGS.browser_restart_every:
                self.close()

    def _open_size_guide(self, page, adapter: RetailerAdapter) -> None:
        """Click the first visible size-guide control, then wait briefly for it to render."""
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        for selector in adapter.size_guide_selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() == 0 or not loc.is_visible(timeout=1000):
                    continue
                loc.click(timeout=2000)
            except PlaywrightError as exc:
                log.debug("size guide click failed selector=%s err=%s", selector, exc)
                continue
            try:
                page.wait_for_selector("table, [role=dialog]", timeout=SETTINGS.browser_wait_ms)
            except PlaywrightTimeoutError:
                pass
            return

    def close(self) -> None:
        for closer in (lambda: self._browser and self._browser.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception as exc:  # already dead; nothing else to release
                log.debug("browser close error: %s", exc)
        self._browser = None
        self._pw = None
        self._pages = 0


_browser: BrowserFetcher | None = None


def get_browser() -> BrowserFetcher | None:
    global _browser
    if not SETTINGS.browser_enabled:
        return None
    if _browser is None:
        _browser = BrowserFetcher()
    return _browser


# ============================================================
# EXTRACTION (fetch + parse one URL)
# ============================================================

@dataclass
class Outcome:
    """status: found | not_found | blocked | image_chart | failed"""
    status: str
    source_url: str | None = None
    chart: Chart | None = None
    error: str | None = None
    http_status: int | None = None
    images: list[dict[str, str]] = field(default_factory=list)


def _outcome_from_page(page: PageResult, fetched: FetchResult) -> Outcome | None:
    """Terminal outcome for a parsed page, or None to keep trying."""
    if page.status == "blocked":
        return Outcome("blocked", fetched.final_url, error=page.error, http_status=fetched.status)
    if page.status == "found":
        return Outcome("found", fetched.final_url, chart=page.chart, http_status=fetched.status)
    if page.status == "product_mismatch":
        return Outcome("not_found", fetched.final_url, error=f"product_mismatch: {page.error}",
                       http_status=fetched.status)
    if page.authoritative:
        status = "image_chart" if page.status == "image_chart" else "not_found"
        return Outcome(status, fetched.final_url, error=page.error, images=page.images,
                       http_status=fetched.status)
    return None


def extract_product(
    url: str,
    expected_title: str | None = None,
    http_client: httpx.Client | None = None,
    browser: BrowserFetcher | None = None,
) -> Outcome:
    """HTTP first; the browser only for pages that were reachable but had no chart."""
    adapter = adapter_for(url)
    client = http_client or get_http_client()
    images: list[dict[str, str]] = []
    errors: list[str] = []

    fetched = fetch_http(client, url)
    if fetched.error:
        errors.append(f"http: {fetched.error}")
    else:
        if fetched.status in (404, 410):
            return Outcome("not_found", fetched.final_url, http_status=fetched.status,
                           error=f"product page returned HTTP {fetched.status}")
        page = extract_page(
            fetched.html, fetched.final_url, requested_url=url, expected_title=expected_title,
            http_status=fetched.status, via="http", max_rows=SETTINGS.max_chart_rows,
        )
        terminal = _outcome_from_page(page, fetched)
        if terminal:
            return terminal
        images.extend(page.images)
        if fetched.status and fetched.status >= 400:
            errors.append(f"http: status {fetched.status}")

    if browser is not None and adapter.use_browser:
        try:
            rendered = browser.fetch(url, adapter)
        except Exception as exc:  # Playwright raises many error types; all mean "no render"
            errors.append(f"browser: {type(exc).__name__}: {clean_text(exc)[:300]}")
            browser.close()
        else:
            page = extract_page(
                rendered.html, rendered.final_url, requested_url=url, expected_title=expected_title,
                http_status=rendered.status, via="browser", max_rows=SETTINGS.max_chart_rows,
            )
            terminal = _outcome_from_page(page, rendered)
            if terminal:
                return terminal
            images.extend(i for i in page.images if i not in images)

    if images:
        return Outcome("image_chart", url, images=images[:5],
                       error="size chart image found; OCR not implemented")
    if errors and (fetched.error or (fetched.status or 0) >= 500):
        return Outcome("failed", url, error="; ".join(errors), http_status=fetched.status)
    return Outcome("not_found", url, error="; ".join(errors) or "no product-specific measurement chart",
                   http_status=fetched.status)


# ============================================================
# JOB OUTCOME POLICY
# ============================================================

@dataclass(frozen=True)
class JobPlan:
    job_status: str          # value passed to finish_size_chart_job
    product_status: str      # gsa_product.size_chart_status
    job_error: str | None
    product_error: str | None
    http_status: int | None
    retry_seconds: int


def _backoff(attempts: int, base: int, cap: int, max_attempts: int) -> int:
    if attempts >= max_attempts:
        return EXHAUSTED_RETRY
    return min(cap, base * 2 ** max(0, attempts - 1))


def _trim(text: str | None) -> str | None:
    return None if text is None else str(text)[:MAX_ERROR_CHARS]


def plan_job(outcome: Outcome, attempts: int, max_attempts: int | None = None) -> JobPlan:
    """
    Map an extraction outcome to queue + product status.

    The job table only accepts pending/processing/found/not_found/blocked/failed,
    so an image chart finishes the job as not_found and marks the *product*
    pending_ocr, with the candidate image URLs kept in size_chart_error.
    """
    max_attempts = max_attempts or SETTINGS.max_attempts
    error = _trim(outcome.error)

    if outcome.status == "found":
        return JobPlan("found", "found", None, None, None, 0)
    if outcome.status == "not_found":
        return JobPlan("not_found", "not_found", None, error, None, NOT_FOUND_RETRY)
    if outcome.status == "image_chart":
        detail = _trim("pending_ocr: size chart image found; OCR not implemented; images="
                       + json.dumps([i["url"] for i in outcome.images]))
        return JobPlan("not_found", "pending_ocr", None, detail, None, PENDING_OCR_RETRY)
    if outcome.status == "blocked":
        retry = _backoff(attempts, BLOCKED_RETRY_BASE, BLOCKED_RETRY_CAP, max_attempts)
        return JobPlan("blocked", "blocked", error or "blocked", error or "blocked",
                       outcome.http_status, retry)
    retry = _backoff(attempts, FAILED_RETRY_BASE, FAILED_RETRY_CAP, max_attempts)
    return JobPlan("failed", "failed", error or "failed", error or "failed", None, retry)


# ============================================================
# DATABASE WRITES
# ============================================================

def save_resolved_url(conn, product_id: int, resolution: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE gsa_product
        SET resolved_product_url = %s,
            resolved_retailer = %s,
            url_resolution_status = %s,
            url_resolution_error = %s,
            url_resolved_at = NOW()
        WHERE id = %s
        """,
        (
            resolution.get("resolved_url"),
            (resolution.get("retailer") or None) and resolution["retailer"][:200],
            resolution.get("status"),
            _trim(resolution.get("error")),
            product_id,
        ),
    )


_UPSERT_MEASUREMENT = """
    INSERT INTO gsa_product_measurement (
        product_id, size_label, measurements, unit, chart_type, source,
        source_url, extraction_method, raw_data, confidence
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (product_id, size_label) DO UPDATE SET
        measurements = EXCLUDED.measurements,
        unit = EXCLUDED.unit,
        chart_type = EXCLUDED.chart_type,
        source = EXCLUDED.source,
        source_url = EXCLUDED.source_url,
        extraction_method = EXCLUDED.extraction_method,
        raw_data = EXCLUDED.raw_data,
        confidence = EXCLUDED.confidence,
        updated_at = NOW()
"""


def save_chart(conn, product_id: int, chart: Chart, source_url: str) -> int:
    """
    Upsert one row per size and drop sizes that are no longer in the chart,
    so re-processing a product converges on exactly the current chart.
    """
    extracted_at = datetime.now(timezone.utc).isoformat()
    size_order = list(chart.sizes)
    params = []
    for label, measurements in chart.sizes.items():
        if len(label) > 100:
            continue
        raw = {
            "row": chart.raw_rows.get(label),
            "size_order": size_order,
            "measurement_basis": chart.measurement_basis,
            "evidence": chart.evidence,
            "chart": chart.raw,
            "extracted_at": extracted_at,
        }
        params.append((
            product_id, label, Jsonb(measurements), (chart.unit or None),
            chart.chart_type[:100], chart.source[:100], source_url,
            chart.method[:50], Jsonb(raw), chart.confidence,
        ))
    if len(params) < 2:
        raise ValueError(f"refusing to save chart with {len(params)} usable size rows")

    with conn.cursor() as cur:
        cur.executemany(_UPSERT_MEASUREMENT, params)
        cur.execute(
            "DELETE FROM gsa_product_measurement WHERE product_id = %s AND NOT (size_label = ANY(%s))",
            (product_id, [p[1] for p in params]),
        )
    return len(params)


def finish_job(conn, job_id: int, plan: JobPlan) -> None:
    conn.execute(
        "SELECT finish_size_chart_job(%s::bigint, %s::text, %s::text, %s::integer, %s::integer)",
        (job_id, plan.job_status, plan.job_error, plan.http_status, plan.retry_seconds),
    )


def update_product_status(conn, product_id: int, status: str, error: str | None) -> None:
    conn.execute(
        """
        UPDATE gsa_product
        SET size_chart_status = %s,
            size_chart_last_checked_at = NOW(),
            size_chart_error = %s
        WHERE id = %s
        """,
        (status, _trim(error), product_id),
    )


def finalize_job(
    job: dict[str, Any],
    outcome: Outcome,
    resolution: dict[str, Any] | None,
) -> JobPlan:
    """Persist everything for one job atomically."""
    plan = plan_job(outcome, job.get("attempts") or 1)
    with get_pool().connection() as conn, conn.transaction():
        if resolution is not None:
            save_resolved_url(conn, job["product_id"], resolution)
        if outcome.status == "found":
            save_chart(conn, job["product_id"], outcome.chart, outcome.source_url)
        finish_job(conn, job["job_id"], plan)
        # finish_size_chart_job also sets product status, but it cannot express
        # pending_ocr; setting it explicitly keeps both in agreement.
        update_product_status(conn, job["product_id"], plan.product_status, plan.product_error)
    return plan


# ============================================================
# QUEUE
# ============================================================

def claim_jobs(limit: int) -> list[dict[str, Any]]:
    """
    Claim via claim_size_chart_jobs(text, integer). The explicit casts pick
    the integer overload; psycopg would otherwise send a small Python int as
    int2 and silently hit the smallint overload.
    """
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM claim_size_chart_jobs(%s::text, %s::integer)",
                    (SETTINGS.worker_id, limit))
        claimed = cur.fetchall()
        job_ids = [row.get("job_id", row.get("id")) for row in claimed]
        if not job_ids:
            return []
        cur.execute(
            """
            SELECT j.id AS job_id, j.attempts, p.id AS product_id, p.product_url, p.title,
                   p.resolved_product_url, p.resolved_retailer, p.url_resolution_status,
                   CASE WHEN p.product_url ILIKE '%%google.%%' THEN p.raw END AS raw
            FROM size_chart_jobs j
            JOIN gsa_product p ON p.id = j.product_id
            WHERE j.id = ANY(%s)
            ORDER BY j.id
            """,
            (job_ids,),
        )
        return cur.fetchall()


def resolve_for_job(job: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """(resolution, changed). A previous successful retailer resolution is reused."""
    cached = job.get("resolved_product_url")
    if (job.get("url_resolution_status") == "resolved" and cached
            and not is_google_url(cached)):
        return {"status": "resolved", "resolved_url": cached,
                "retailer": job.get("resolved_retailer") or site_from_url(cached),
                "method": "cached"}, False
    resolution = resolve_product_url(
        job.get("product_url") or "", job.get("raw"),
        http_client=get_http_client(), allow_google_http=SETTINGS.google_http_resolution,
    )
    return resolution, True


def resolution_outcome(resolution: dict[str, Any]) -> Outcome | None:
    """Outcome when no retailer page can be processed, else None."""
    status = resolution.get("status")
    if status == "resolved":
        return None
    if status == "blocked":
        return Outcome("blocked", error=f"url_resolution: {resolution.get('error')}",
                       http_status=resolution.get("http_status"))
    if status == "unresolved":
        return Outcome("not_found", error=f"retailer_url_unresolved: {resolution.get('error')}")
    return Outcome("failed", error=f"url_resolution: {resolution.get('error')}")


def process_job(job: dict[str, Any]) -> JobPlan:
    started = time.monotonic()
    resolution: dict[str, Any] | None = None
    try:
        resolution, changed = resolve_for_job(job)
        outcome = resolution_outcome(resolution)
        if outcome is None:
            outcome = extract_product(resolution["resolved_url"], job.get("title"),
                                      get_http_client(), get_browser())
        if not changed:
            resolution = None
    except Exception as exc:
        log.exception("extraction error job=%s product=%s", job["job_id"], job["product_id"])
        outcome = Outcome("failed", error=f"{type(exc).__name__}: {exc}")

    plan = finalize_job(job, outcome, resolution)
    log_event(
        "job_done",
        job=job["job_id"], product=job["product_id"], attempts=job.get("attempts"),
        status=plan.product_status,
        site=site_from_url(outcome.source_url) if outcome.source_url else None,
        rows=len(outcome.chart.sizes) if outcome.chart else None,
        method=outcome.chart.method if outcome.chart else None,
        confidence=outcome.chart.confidence if outcome.chart else None,
        http=outcome.http_status, error=(outcome.error or "")[:200] or None,
        ms=int((time.monotonic() - started) * 1000),
    )
    return plan


def release_jobs(jobs: list[dict[str, Any]]) -> None:
    """Hand back claimed-but-unprocessed jobs on shutdown instead of waiting for stale recovery."""
    plan = JobPlan("failed", "pending", "worker shutdown before processing",
                   "worker shutdown before processing", None, 0)
    for job in jobs:
        try:
            with get_pool().connection() as conn, conn.transaction():
                finish_job(conn, job["job_id"], plan)
                update_product_status(conn, job["product_id"], "pending", None)
        except Exception:
            log.exception("could not release job=%s", job["job_id"])


_stop = threading.Event()


def process_once() -> int:
    jobs = claim_jobs(SETTINGS.batch_size)
    processed = 0
    for i, job in enumerate(jobs):
        if _stop.is_set():
            release_jobs(jobs[i:])
            break
        try:
            process_job(job)
        except Exception:
            # finalize_job itself failed (DB down?). The job stays 'processing'
            # and recover_stale_size_chart_jobs will hand it back.
            log.exception("could not persist job=%s product=%s", job["job_id"], job["product_id"])
        processed += 1
    return processed


# ============================================================
# MAIN LOOP
# ============================================================

def _install_signal_handlers() -> None:
    def handle(signum, _frame):
        log_event("shutdown_requested", signal=signum)
        _stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle)
        except (ValueError, OSError):
            pass


def run_worker() -> None:
    _install_signal_handlers()

    if not SETTINGS.worker_enabled:
        # App Platform restarts workers that exit, so stay alive while disabled.
        log_event("worker_disabled", worker=SETTINGS.worker_id,
                  hint="set WORKER_ENABLED=true to process the queue")
        while not _stop.wait(3600):
            log_event("worker_disabled", worker=SETTINGS.worker_id)
        return

    log_event("worker_started", worker=SETTINGS.worker_id, batch=SETTINGS.batch_size,
              browser=SETTINGS.browser_enabled, google_http=SETTINGS.google_http_resolution)
    try:
        while not _stop.is_set():
            try:
                if process_once() == 0:
                    _stop.wait(SETTINGS.poll_seconds)
            except Exception:
                log.exception("worker loop error")
                _stop.wait(SETTINGS.poll_seconds)
    finally:
        if _browser is not None:
            _browser.close()
        if _http_client is not None:
            _http_client.close()
        close_pool()
        log_event("worker_stopped", worker=SETTINGS.worker_id)


# ============================================================
# CLI helpers (no queue side effects)
# ============================================================

def _print_outcome(outcome: Outcome) -> None:
    out = {
        "status": outcome.status,
        "source_url": outcome.source_url,
        "http_status": outcome.http_status,
        "error": outcome.error,
        "images": outcome.images or None,
        "chart": outcome.chart.as_dict() if outcome.chart else None,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cli_extract_url(url: str, title: str | None, use_browser: bool) -> None:
    resolution = resolve_product_url(url, None, http_client=get_http_client(),
                                     allow_google_http=SETTINGS.google_http_resolution)
    outcome = resolution_outcome(resolution) or extract_product(
        resolution["resolved_url"], title, get_http_client(),
        get_browser() if use_browser else None)
    _print_outcome(outcome)


def cli_extract_file(path: str, url: str, title: str | None) -> None:
    with open(path, encoding="utf-8", errors="replace") as f:
        html = f.read()
    page = extract_page(html, url, requested_url=url, expected_title=title,
                        http_status=200, max_rows=SETTINGS.max_chart_rows)
    fetched = FetchResult(url, url, 200)
    outcome = _outcome_from_page(page, fetched) or Outcome(
        page.status, url, error=page.error, images=page.images)
    _print_outcome(outcome)


def cli_dry_run(product_id: int, use_browser: bool) -> None:
    """Resolve + extract one product from the DB without writing anything."""
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT 0 AS job_id, 1 AS attempts, id AS product_id, product_url, title, raw,
                   resolved_product_url, resolved_retailer, url_resolution_status
            FROM gsa_product WHERE id = %s
            """,
            (product_id,),
        )
        job = cur.fetchone()
    if not job:
        raise SystemExit(f"product {product_id} not found")
    resolution, _changed = resolve_for_job(job)
    print(json.dumps({"resolution": {k: v for k, v in resolution.items() if k != "candidates"}},
                     indent=2, default=str))
    outcome = resolution_outcome(resolution) or extract_product(
        resolution["resolved_url"], job.get("title"), get_http_client(),
        get_browser() if use_browser else None)
    _print_outcome(outcome)
    print(json.dumps({"would_store": plan_job(outcome, 1).__dict__}, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Size-chart worker")
    sub = parser.add_subparsers(dest="command")

    p_url = sub.add_parser("extract-url", help="fetch and extract one URL (no DB)")
    p_url.add_argument("url")
    p_url.add_argument("--title")
    p_url.add_argument("--no-browser", action="store_true")

    p_file = sub.add_parser("extract-file", help="extract a saved HTML file (no DB, no network)")
    p_file.add_argument("path")
    p_file.add_argument("--url", required=True, help="the page's product URL")
    p_file.add_argument("--title")

    p_dry = sub.add_parser("dry-run", help="process one product from the DB without writing")
    p_dry.add_argument("--product-id", type=int, required=True)
    p_dry.add_argument("--no-browser", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "extract-url":
        cli_extract_url(args.url, args.title, not args.no_browser)
    elif args.command == "extract-file":
        cli_extract_file(args.path, args.url, args.title)
    elif args.command == "dry-run":
        cli_dry_run(args.product_id, not args.no_browser)
    else:
        run_worker()


if __name__ == "__main__":
    main()

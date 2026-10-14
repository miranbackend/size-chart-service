"""
Size-chart worker for the `products` table.

    products (active, not yet checked or due for a re-check)
        -> claimed via product_size_chart_checks.result = 'processing'
        -> fetch the store page over HTTP, browser only when needed
        -> extract a product-specific chart (extraction.py)
        -> product_size_charts upsert + product_size_chart_checks result,
           in one transaction

Usage:
    python worker.py                                  # queue loop: WORKER_MODE=1 start, 0 stop
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

from ocr import read_chart_image
from extraction import (
    Chart,
    PageResult,
    RetailerAdapter,
    adapter_for,
    clean_text,
    extract_page,
    extract_shopify_product,
    KIWI_API,
    kiwi_page_result,
    kiwi_request_params,
    relentless_page_result,
    is_google_url,
    shopify_json_url,
)


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


def _env_worker_enabled() -> bool:
    """
    WORKER_MODE=1 starts processing size charts, WORKER_MODE=0 stops.
    Without WORKER_MODE, the older WORKER_ENABLED=true still works.
    Any other value is treated as stopped, never as running.
    """
    mode = os.getenv("WORKER_MODE", "").strip()
    if not mode:
        return _env_bool("WORKER_ENABLED", False)
    if mode not in ("0", "1"):
        logging.getLogger("size-chart-worker").error("invalid WORKER_MODE=%r (use 0 or 1); staying stopped", mode)
    return mode == "1"


@dataclass
class Settings:
    worker_id: str = field(default_factory=lambda: os.getenv("WORKER_ID") or socket.gethostname())
    # Off unless explicitly enabled, so a deploy never starts draining the queue by accident.
    worker_enabled: bool = field(default_factory=_env_worker_enabled)
    batch_size: int = field(default_factory=lambda: _env_int("BATCH_SIZE", 10))
    poll_seconds: int = field(default_factory=lambda: _env_int("POLL_SECONDS", 10))
    nav_timeout_ms: int = field(default_factory=lambda: _env_int("NAV_TIMEOUT_MS", 45000))
    http_timeout_seconds: int = field(default_factory=lambda: _env_int("HTTP_TIMEOUT_SECONDS", 25))
    db_pool_size: int = field(default_factory=lambda: _env_int("DB_POOL_SIZE", 5))
    browser_wait_ms: int = field(default_factory=lambda: _env_int("BROWSER_WAIT_MS", 1500))
    max_chart_rows: int = field(default_factory=lambda: _env_int("MAX_CHART_ROWS", 100))
    max_html_bytes: int = field(default_factory=lambda: _env_int("MAX_HTML_BYTES", 8_000_000))
    browser_enabled: bool = field(default_factory=lambda: _env_bool("BROWSER_ENABLED", True))
    browser_restart_every: int = field(default_factory=lambda: _env_int("BROWSER_RESTART_EVERY", 50))
    # Size-chart images in the product description are read with Gemini when a key is set.
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "").strip(), repr=False)
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip())
    ocr_enabled: bool = field(default_factory=lambda: _env_bool("OCR_ENABLED", True))
    ocr_max_images: int = field(default_factory=lambda: _env_int("OCR_MAX_IMAGES", 3))
    ocr_timeout_seconds: int = field(default_factory=lambda: _env_int("OCR_TIMEOUT_SECONDS", 60))


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


_TEXT_CONTENT_TYPES = ("html", "json", "javascript", "text/plain", "xml")


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
    """status: found | not_found | generic_chart | blocked | image_chart | failed"""
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
    if page.status == "generic_chart":
        return Outcome("generic_chart", fetched.final_url, error=page.error, http_status=fetched.status)
    if page.authoritative:
        status = "image_chart" if page.status == "image_chart" else "not_found"
        return Outcome(status, fetched.final_url, error=page.error, images=page.images,
                       http_status=fetched.status)
    return None


def extract_shopify(url: str, shopify_product_id: str | None, client: httpx.Client) -> PageResult | None:
    """
    The product's own description from <product_url>.js, after checking the
    JSON id equals shopify_product_id. None when the store doesn't serve it.
    """
    js_url = shopify_json_url(url)
    if not js_url or not shopify_product_id:
        return None
    fetched = fetch_http(client, js_url)
    if fetched.error or fetched.status != 200:
        return None
    try:
        data = json.loads(fetched.html)
    except (json.JSONDecodeError, RecursionError):
        return None  # not JSON (e.g. a challenge page): the page path decides
    return extract_shopify_product(data, str(shopify_product_id), url, SETTINGS.max_chart_rows)


def extract_kiwi(html: str, product_ids: tuple[str, ...], client: httpx.Client) -> PageResult | None:
    """
    The chart the Kiwi Sizing app assigns to this product. Uses the same
    request the store's own Kiwi widget makes from the product page.
    """
    params = kiwi_request_params(html)
    if not params:
        return None
    if product_ids and params["product"] not in product_ids:
        return PageResult(status="not_found",
                          error=f"kiwi: page product {params['product']} != {product_ids[0]}")
    try:
        response = client.get(KIWI_API, params=params, timeout=SETTINGS.http_timeout_seconds)
    except httpx.HTTPError as exc:
        return PageResult(status="failed", error=f"kiwi: {type(exc).__name__}")
    if response.status_code != 200:
        return PageResult(status="failed", error=f"kiwi: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        return PageResult(status="failed", error="kiwi: invalid JSON")
    return kiwi_page_result(data, params["product"], SETTINGS.max_chart_rows)


def _scope(page: PageResult) -> str:
    return (page.chart.evidence.get("chart_scope") if page.chart else None) or "store"


def extract_product(
    url: str,
    expected_title: str | None = None,
    http_client: httpx.Client | None = None,
    browser: BrowserFetcher | None = None,
    known_product_ids: tuple[str, ...] = (),
    shopify_product_id: str | None = None,
) -> Outcome:
    """
    The size chart the store shows for this product, best source first:
      1. the product's own Shopify description (id-checked)        scope product
      2. a chart in the page's product description / product data  scope product
      3. the Kiwi Sizing app's chart for this product               scope product/collection/store
      4. any other chart on the page (theme size guide)             scope store
      5. browser render, for charts drawn by JavaScript
      6. size-chart images -> image_chart (OCR happens in apply_ocr)
    Every saved chart carries evidence.chart_scope.
    """
    adapter = adapter_for(url)
    client = http_client or get_http_client()
    images: list[dict[str, str]] = []
    errors: list[str] = []

    shopify = extract_shopify(url, shopify_product_id, client)
    if shopify is not None:
        if shopify.status == "found":
            return Outcome("found", url, chart=shopify.chart, http_status=200)
        if shopify.status == "product_mismatch":
            return Outcome("not_found", url, error=f"product_mismatch: {shopify.error}", http_status=200)
        images.extend(shopify.images)  # images in the product's own description first

    fetched = fetch_http(client, url)
    store_chart: Outcome | None = None
    if fetched.error:
        errors.append(f"http: {fetched.error}")
    elif fetched.status in (404, 410):
        if not images:
            return Outcome("not_found", fetched.final_url, http_status=fetched.status,
                           error=f"product page returned HTTP {fetched.status}")
        # The page is gone but the id-checked description had a chart image.
        return Outcome("image_chart", url, images=images, http_status=fetched.status,
                       error="size chart image found; OCR not available")
    else:
        page = extract_page(
            fetched.html, fetched.final_url, requested_url=url, expected_title=expected_title,
            http_status=fetched.status, via="http", max_rows=SETTINGS.max_chart_rows,
            known_product_ids=known_product_ids,
        )
        if page.status == "blocked":
            return Outcome("blocked", fetched.final_url, error=page.error, http_status=fetched.status)
        if page.status == "product_mismatch":
            return Outcome("not_found", fetched.final_url, error=f"product_mismatch: {page.error}",
                           http_status=fetched.status)
        if page.status == "found" and _scope(page) == "product":
            return Outcome("found", fetched.final_url, chart=page.chart, http_status=fetched.status)
        if page.status == "found":
            store_chart = Outcome("found", fetched.final_url, chart=page.chart, http_status=fetched.status)

        kiwi = extract_kiwi(fetched.html, known_product_ids, client)
        if kiwi is not None and kiwi.status == "found":
            return Outcome("found", fetched.final_url, chart=kiwi.chart, http_status=fetched.status)
        if kiwi is not None and kiwi.error:
            errors.append(kiwi.error)

        app = relentless_page_result(fetched.html, fetched.final_url,
                                     known_product_ids[0] if known_product_ids else None,
                                     SETTINGS.max_chart_rows)
        if app is not None and app.status == "found":
            return Outcome("found", fetched.final_url, chart=app.chart, http_status=fetched.status)
        if app is not None and app.status == "image_chart":
            images.extend(i for i in app.images if i not in images)
        elif app is not None and app.error:
            errors.append(app.error)

        if store_chart:
            return store_chart
        if page.authoritative and page.status == "not_found":
            return Outcome("not_found", fetched.final_url, error=page.error, http_status=fetched.status)
        images.extend(i for i in page.images if i not in images)
        if fetched.status and fetched.status >= 400:
            errors.append(f"http: status {fetched.status}")

    if browser is not None and adapter.use_browser and not images:
        try:
            rendered = browser.fetch(url, adapter)
        except Exception as exc:  # Playwright raises many error types; all mean "no render"
            errors.append(f"browser: {type(exc).__name__}: {clean_text(exc)[:300]}")
            browser.close()
        else:
            page = extract_page(
                rendered.html, rendered.final_url, requested_url=url, expected_title=expected_title,
                http_status=rendered.status, via="browser", max_rows=SETTINGS.max_chart_rows,
                known_product_ids=known_product_ids,
            )
            terminal = _outcome_from_page(page, rendered)
            if terminal:
                return terminal
            images.extend(i for i in page.images if i not in images)

    if images:
        return Outcome("image_chart", url, images=images,
                       error="size chart image found; OCR not available")
    if errors and (fetched.error or (fetched.status or 0) >= 500):
        return Outcome("failed", url, error="; ".join(errors), http_status=fetched.status)
    return Outcome("not_found", url, error="; ".join(errors) or "no size chart on the product page",
                   http_status=fetched.status)


# ============================================================
# CHECK RESULTS + RECHECK POLICY
# ============================================================

# product_size_chart_checks.result values
RESULT_PROCESSING = "processing"
OUTCOME_TO_RESULT = {
    "found": "found",
    "not_found": "not_found",
    "image_chart": "pending_ocr",
    "generic_chart": "generic_chart",
    "ocr_review": "ocr_review",
    "blocked": "blocked",
    "failed": "failed",
}

# How long after its last check a product with this result is picked again.
# 'found' is never re-checked automatically; 'processing' older than this
# means the worker died mid-product.
RECHECK_AFTER_SECONDS = {
    RESULT_PROCESSING: 30 * 60,
    "failed": 6 * 3600,
    "blocked": 24 * 3600,
    "not_found": 30 * 24 * 3600,
    "pending_ocr": 30 * 24 * 3600,
    "generic_chart": 30 * 24 * 3600,
    "ocr_review": 30 * 24 * 3600,
}
MAX_ERROR_CHARS = 300


def result_for(outcome: Outcome) -> str:
    return OUTCOME_TO_RESULT.get(outcome.status, "failed")


# ============================================================
# DATABASE (products / product_size_charts / product_size_chart_checks)
# ============================================================

_CLAIM_PRODUCTS = """
    WITH picked AS (
        SELECT p.id
        FROM products p
        LEFT JOIN product_size_chart_checks k ON k.product_id = p.id
        WHERE p.is_active
          AND NULLIF(TRIM(p.product_url), '') IS NOT NULL
          AND (
              k.product_id IS NULL
              OR k.checked_at < NOW() - CASE k.result
                  WHEN 'processing'  THEN %(processing)s * INTERVAL '1 second'
                  WHEN 'failed'      THEN %(failed)s * INTERVAL '1 second'
                  WHEN 'blocked'     THEN %(blocked)s * INTERVAL '1 second'
                  WHEN 'not_found'   THEN %(not_found)s * INTERVAL '1 second'
                  WHEN 'pending_ocr' THEN %(pending_ocr)s * INTERVAL '1 second'
                  WHEN 'generic_chart' THEN %(generic_chart)s * INTERVAL '1 second'
                  WHEN 'ocr_review'  THEN %(ocr_review)s * INTERVAL '1 second'
              END
          )
        ORDER BY k.checked_at ASC NULLS FIRST, p.id
        LIMIT %(limit)s
        FOR UPDATE OF p SKIP LOCKED
    ),
    claimed AS (
        INSERT INTO product_size_chart_checks (product_id, result, checked_at)
        SELECT id, 'processing', NOW() FROM picked
        ON CONFLICT (product_id) DO UPDATE
            SET result = EXCLUDED.result, checked_at = EXCLUDED.checked_at
        RETURNING product_id
    )
    SELECT p.id AS product_id, p.product_url, p.title, p.shopify_product_id, p.sizes
    FROM claimed c
    JOIN products p ON p.id = c.product_id
    ORDER BY p.id
"""


def claim_products(limit: int) -> list[dict[str, Any]]:
    """
    Claim up to `limit` active products that are unchecked or due for a
    re-check, marking them 'processing' in the same statement. SKIP LOCKED
    keeps concurrent workers off each other's products.
    """
    params = {"limit": limit, **RECHECK_AFTER_SECONDS}
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_CLAIM_PRODUCTS, params)
        return cur.fetchall()


_UPSERT_CHART = """
    INSERT INTO product_size_charts
        (product_id, chart, chart_type, unit, source, source_url, extraction_method)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (product_id) DO UPDATE SET
        chart = EXCLUDED.chart,
        chart_type = EXCLUDED.chart_type,
        unit = EXCLUDED.unit,
        source = EXCLUDED.source,
        source_url = EXCLUDED.source_url,
        extraction_method = EXCLUDED.extraction_method,
        updated_at = NOW()
"""


def chart_document(chart: Chart) -> dict[str, Any]:
    """
    The JSON stored in product_size_charts.chart. JSONB does not keep key
    order, so the size order is stored explicitly.
    """
    return {
        "chart_scope": chart.evidence.get("chart_scope", "store"),
        "method": chart.method,
        "sizes": chart.sizes,
        "size_order": list(chart.sizes),
        "unit": chart.unit,
        "measurement_basis": chart.measurement_basis,
        "confidence": chart.confidence,
        "evidence": chart.evidence,
        "raw_rows": chart.raw_rows,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


# Values stored in product_size_charts' short varchar columns (production
# has varchar(16)); the full method name is kept in chart JSON.
CHART_TYPE_DB = "measurement"
METHOD_DB = {
    "shopify_description": "shopify_desc",
    "embedded_json_html": "json_in_html",
}


def method_db_name(method: str) -> str:
    """Short extraction_method; browser rendering is recorded in `source`."""
    base = method[len("browser_"):] if method.startswith("browser_") else method
    return METHOD_DB.get(base, base)


def source_db_name(chart: Chart) -> str:
    if chart.source == "image":
        return "image"
    if chart.method == "kiwi_app":
        return "kiwi"
    if chart.method == "size_chart_app":
        return "app"
    return "browser" if chart.method.startswith("browser_") else "html"


_column_limits: dict[tuple[str, str], int] | None = None


def column_limits(conn) -> dict[tuple[str, str], int]:
    """varchar(n) limits of the columns the worker writes, read once per process."""
    global _column_limits
    if _column_limits is None:
        rows = conn.execute(
            """
            SELECT table_name, column_name, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name IN ('product_size_charts', 'product_size_chart_checks')
              AND character_maximum_length IS NOT NULL
            """
        ).fetchall()
        _column_limits = {(t, c): n for t, c, n in rows}
    return _column_limits


def fit(conn, table: str, column: str, value: str | None) -> str | None:
    """Never let a long value abort the save: cut it to the column's limit, loudly."""
    limit = column_limits(conn).get((table, column))
    if value is not None and limit and len(value) > limit:
        log.warning("value %r too long for %s.%s varchar(%s); truncated", value, table, column, limit)
        return value[:limit]
    return value


def save_chart(conn, product_id: int, chart: Chart, source_url: str) -> int:
    if len(chart.sizes) < 2:
        raise ValueError(f"refusing to save chart with {len(chart.sizes)} size rows")
    t = "product_size_charts"
    conn.execute(
        _UPSERT_CHART,
        (
            product_id,
            Jsonb(chart_document(chart)),
            fit(conn, t, "chart_type", CHART_TYPE_DB),
            fit(conn, t, "unit", chart.unit or "unknown"),  # unit is NOT NULL
            fit(conn, t, "source", source_db_name(chart)),
            source_url,
            fit(conn, t, "extraction_method", method_db_name(chart.method)),
        ),
    )
    return len(chart.sizes)


def record_check(conn, product_id: int, result: str) -> None:
    conn.execute(
        """
        INSERT INTO product_size_chart_checks (product_id, result, checked_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (product_id) DO UPDATE
            SET result = EXCLUDED.result, checked_at = EXCLUDED.checked_at
        """,
        (product_id, fit(conn, "product_size_chart_checks", "result", result)),
    )


def finalize_product(product: dict[str, Any], outcome: Outcome) -> str:
    """Save the chart (if any) and the check result atomically."""
    result = result_for(outcome)
    with get_pool().connection() as conn, conn.transaction():
        if outcome.status == "found":
            save_chart(conn, product["product_id"], outcome.chart, outcome.source_url)
        record_check(conn, product["product_id"], result)
    return result


def release_products(products: list[dict[str, Any]]) -> None:
    """On shutdown, hand back claimed-but-unprocessed products immediately."""
    ids = [p["product_id"] for p in products]
    if not ids:
        return
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM product_size_chart_checks WHERE product_id = ANY(%s) AND result = %s",
                (ids, RESULT_PROCESSING),
            )
    except Exception:
        log.exception("could not release %s products", len(ids))


# ============================================================
# PROCESSING
# ============================================================

def ocr_enabled() -> bool:
    return SETTINGS.ocr_enabled and bool(SETTINGS.gemini_api_key)


def apply_ocr(outcome: Outcome, product: dict[str, Any]) -> Outcome:
    """
    Read the size-chart images (each image is read once per worker and
    cached, since store-wide images repeat on every product). A chart is
    saved only if exactly one distinct chart matches the product's sizes.
    """
    if not product.get("sizes"):
        # An image chart is only accepted when its sizes match the product's
        # sizes, so without sizes (jewellery, bags) OCR can never succeed.
        return Outcome("not_found", outcome.source_url, images=outcome.images,
                       error="size chart image found but the product has no sizes to check it against")
    found, statuses, errors = [], [], []
    for image in outcome.images[: max(1, SETTINGS.ocr_max_images)]:
        scope = image.get("scope") or ("product" if image.get("in_product_description") else "store")
        result = read_chart_image(
            get_http_client(), image["url"],
            api_key=SETTINGS.gemini_api_key, model=SETTINGS.gemini_model,
            product_sizes=product.get("sizes"), timeout=SETTINGS.ocr_timeout_seconds,
            max_rows=SETTINGS.max_chart_rows, scope=scope,
        )
        if result.status == "found":
            found.append(result.chart)
        else:
            statuses.append(result.status)
            errors.append(f"{result.status}: {result.error}")

    distinct = {json.dumps(c.sizes, sort_keys=True) + str(c.unit): c for c in found}
    if len(distinct) == 1:
        chart = found[0]
        # A product-description image wins over a store-wide one with the same chart.
        for c in found:
            if c.evidence.get("chart_scope") == "product":
                chart = c
                break
        return Outcome("found", outcome.source_url, chart=chart, http_status=outcome.http_status)
    if len(distinct) > 1:
        return Outcome("ocr_review", outcome.source_url, images=outcome.images,
                       error=f"ocr: {len(distinct)} different size-chart images match this product")

    error = "; ".join(errors)[:MAX_ERROR_CHARS]
    if "error" in statuses:
        return Outcome("failed", outcome.source_url, error=f"ocr: {error}", images=outcome.images)
    if "disagree" in statuses or "ambiguous" in statuses:
        return Outcome("ocr_review", outcome.source_url, error=f"ocr: {error}", images=outcome.images)
    return Outcome("not_found", outcome.source_url, error=f"ocr: {error}", images=outcome.images)


def known_ids(product: dict[str, Any]) -> tuple[str, ...]:
    sid = product.get("shopify_product_id")
    return (str(sid),) if sid else ()


def process_product(product: dict[str, Any]) -> str:
    started = time.monotonic()
    url = (product.get("product_url") or "").strip()
    try:
        if is_google_url(url):
            outcome = Outcome("not_found", url, error="product_url is a Google URL, not a store page")
        else:
            outcome = extract_product(url, product.get("title"), get_http_client(),
                                      get_browser(), known_product_ids=known_ids(product),
                                      shopify_product_id=product.get("shopify_product_id"))
        if outcome.status == "image_chart" and ocr_enabled():
            outcome = apply_ocr(outcome, product)
    except Exception as exc:
        log.exception("extraction error product=%s", product["product_id"])
        outcome = Outcome("failed", url, error=f"{type(exc).__name__}: {exc}")

    result = finalize_product(product, outcome)
    log_event(
        "product_done",
        product=product["product_id"], result=result,
        rows=len(outcome.chart.sizes) if outcome.chart else None,
        method=outcome.chart.method if outcome.chart else None,
        confidence=outcome.chart.confidence if outcome.chart else None,
        id_in_page=outcome.chart.evidence.get("product_id_in_page") if outcome.chart else None,
        images=[i["url"] for i in outcome.images][:3] or None,
        http=outcome.http_status, error=(outcome.error or "")[:MAX_ERROR_CHARS] or None,
        url=url[:200], ms=int((time.monotonic() - started) * 1000),
    )
    return result


_stop = threading.Event()


def process_once() -> int:
    products = claim_products(SETTINGS.batch_size)
    processed = 0
    for i, product in enumerate(products):
        if _stop.is_set():
            release_products(products[i:])
            break
        try:
            process_product(product)
        except Exception:
            # Saving failed (DB down?). The 'processing' mark expires after
            # RECHECK_AFTER_SECONDS['processing'] and the product is retried.
            log.exception("could not persist product=%s", product["product_id"])
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
                  hint="set WORKER_MODE=1 to process the queue")
        while not _stop.wait(3600):
            log_event("worker_disabled", worker=SETTINGS.worker_id)
        return

    log_event("worker_started", worker=SETTINGS.worker_id, batch=SETTINGS.batch_size,
              browser=SETTINGS.browser_enabled,
              ocr=f"gemini:{SETTINGS.gemini_model}" if ocr_enabled() else "off")
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


def cli_extract_url(url: str, title: str | None, use_browser: bool, shopify_id: str | None = None) -> None:
    outcome = extract_product(url, title, get_http_client(), get_browser() if use_browser else None,
                              known_product_ids=(shopify_id,) if shopify_id else (),
                              shopify_product_id=shopify_id)
    _print_outcome(outcome)


def cli_extract_file(path: str, url: str, title: str | None) -> None:
    with open(path, encoding="utf-8", errors="replace") as f:
        html = f.read()
    page = extract_page(html, url, requested_url=url, expected_title=title,
                        http_status=200, max_rows=SETTINGS.max_chart_rows, product_scope_only=True)
    fetched = FetchResult(url, url, 200)
    outcome = _outcome_from_page(page, fetched) or Outcome(
        page.status, url, error=page.error, images=page.images)
    _print_outcome(outcome)


def cli_dry_run(product_id: int, use_browser: bool) -> None:
    """Extract one product from the `products` table without writing anything."""
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id AS product_id, product_url, title, shopify_product_id, sizes, is_active "
            "FROM products WHERE id = %s",
            (product_id,),
        )
        product = cur.fetchone()
    if not product:
        raise SystemExit(f"product {product_id} not found in products")
    print(json.dumps({"product": product}, indent=2, default=str))
    outcome = extract_product(product["product_url"], product.get("title"), get_http_client(),
                              get_browser() if use_browser else None,
                              known_product_ids=known_ids(product),
                              shopify_product_id=product.get("shopify_product_id"))
    if outcome.status == "image_chart" and ocr_enabled():
        outcome = apply_ocr(outcome, product)
    _print_outcome(outcome)
    print(json.dumps({"would_record_result": result_for(outcome),
                      "would_save_chart": outcome.status == "found"}, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Size-chart worker")
    sub = parser.add_subparsers(dest="command")

    p_url = sub.add_parser("extract-url", help="fetch and extract one URL (no DB)")
    p_url.add_argument("url")
    p_url.add_argument("--title")
    p_url.add_argument("--no-browser", action="store_true")
    p_url.add_argument("--shopify-id", help="the product's shopify_product_id (enables the id check)")

    p_file = sub.add_parser("extract-file", help="extract a saved HTML file (no DB, no network)")
    p_file.add_argument("path")
    p_file.add_argument("--url", required=True, help="the page's product URL")
    p_file.add_argument("--title")

    p_dry = sub.add_parser("dry-run", help="process one product from the DB without writing")
    p_dry.add_argument("--product-id", type=int, required=True)
    p_dry.add_argument("--no-browser", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "extract-url":
        cli_extract_url(args.url, args.title, not args.no_browser, args.shopify_id)
    elif args.command == "extract-file":
        cli_extract_file(args.path, args.url, args.title)
    elif args.command == "dry-run":
        cli_dry_run(args.product_id, not args.no_browser)
    else:
        run_worker()


if __name__ == "__main__":
    main()

import os
import re
import json
import time
import socket
import logging
from urllib.parse import urlparse, parse_qs

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from bs4 import BeautifulSoup
import httpx
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("size-chart-worker")

DATABASE_URL = os.environ["DATABASE_URL"]
WORKER_ID = os.getenv("WORKER_ID", socket.gethostname())
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "45000"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "5"))

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=int(os.getenv("DB_POOL_SIZE", "5")),
    kwargs={"autocommit": True},
)

SIZE_WORDS = {
    "size", "chest", "bust", "waist", "hip", "hips", "shoulder",
    "length", "sleeve", "sleeve length", "inseam", "outseam",
    "thigh", "rise", "foot length", "footlength", "calf",
    "neck", "collar", "body length", "dress length",
}

SIZE_GUIDE_RE = re.compile(
    r"(size\s*(chart|guide)|size\s*&\s*fit|measurement\s*(chart|guide)?|fit\s*guide)",
    re.I,
)

def site_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "amazon." in host:
        return "amazon"
    if "myntra." in host:
        return "myntra"
    if "ajio." in host:
        return "ajio"
    if "flipkart." in host:
        return "flipkart"
    if "nykaafashion." in host or "nykaa." in host:
        return "nykaa"
    if "google." in host and "search" in urlparse(url).path:
        return "google_shopping"
    return "other"

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def normalize_key(value: str) -> str:
    value = clean_text(value).lower()
    aliases = {
        "bust size": "bust",
        "bust measurement": "bust",
        "chest size": "chest",
        "chest measurement": "chest",
        "waist size": "waist",
        "hip size": "hip",
        "hips": "hip",
        "shoulder width": "shoulder",
        "sleeve": "sleeve_length",
        "sleeve length": "sleeve_length",
        "body length": "length",
        "dress length": "dress_length",
        "garment length": "length",
        "foot length": "foot_length",
        "footlength": "foot_length",
        "leg opening": "leg_opening",
    }
    if value in aliases:
        return aliases[value]
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")

def looks_like_size(value: str) -> bool:
    value = clean_text(value).lower()
    return bool(re.fullmatch(
        r"(xxxs|xxs|xs|s|m|l|xl|xxl|xxxl|xxxxl|"
        r"\d{1,3}(\s*[-/]\s*\d{1,3})?([a-z])?|"
        r"(uk|us|eu|in)\s*\d{1,3}|"
        r"\d{1,2}\s*[-–]\s*\d{1,2}\s*y)",
        value,
    ))

def parse_number(value: str):
    if value is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group(0)) if m else None

def detect_unit(text: str) -> str | None:
    t = text.lower()
    if re.search(r"\b(cm|centimeter|centimeters)\b", t):
        return "cm"
    if re.search(r"\b(in|inch|inches|\")\b", t):
        return "inch"
    if re.search(r"\b(mm|millimeter|millimeters)\b", t):
        return "mm"
    return None

def table_to_chart(table):
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        vals = [clean_text(c.get_text(" ", strip=True)) for c in cells]
        if vals:
            rows.append(vals)

    if len(rows) < 2:
        return None

    # Try to find a header row containing size/measurement terminology.
    header_index = 0
    for i, row in enumerate(rows[:3]):
        joined = " ".join(row).lower()
        if "size" in joined or sum(k in joined for k in SIZE_WORDS) >= 2:
            header_index = i
            break

    headers = rows[header_index]
    normalized_headers = [normalize_key(h) for h in headers]

    if not any(h == "size" or "size" in h for h in normalized_headers):
        # Some charts call it "UK", "US", "Age", etc. Still accept if
        # there are several measurement columns.
        if sum(h in SIZE_WORDS for h in normalized_headers) < 2:
            return None

    data = []
    for row in rows[header_index + 1:]:
        if not row:
            continue
        item = {}
        for i, value in enumerate(row):
            if i >= len(normalized_headers):
                continue
            key = normalized_headers[i]
            if not key:
                continue
            item[key] = value
        if item:
            data.append(item)

    if not data:
        return None

    # A real size chart normally has multiple rows and at least one
    # size/measurement-like column.
    joined = json.dumps({"headers": headers, "rows": data}).lower()
    score = sum(k in joined for k in SIZE_WORDS)
    if len(data) >= 2 and score >= 2:
        return {
            "headers": headers,
            "rows": data,
            "unit": detect_unit(" ".join(headers) + " " + joined),
        }
    return None

def extract_from_html(html: str):
    soup = BeautifulSoup(html, "lxml")

    # Prefer tables near a size-guide heading.
    for table in soup.find_all("table"):
        result = table_to_chart(table)
        if result:
            return result, "html_table"

    # Look for containers containing "size guide/chart" and then tables.
    for node in soup.find_all(string=SIZE_GUIDE_RE):
        parent = node.parent
        if not parent:
            continue
        container = parent
        for _ in range(5):
            if container is None:
                break
            table = container.find("table")
            if table:
                result = table_to_chart(table)
                if result:
                    return result, "size_guide_table"
            container = container.parent

    return None, None

def find_size_chart_image(soup):
    # Return likely chart image URLs. We do not OCR here; this is a hook
    # for an approved OCR/vision provider.
    candidates = []
    for img in soup.find_all("img"):
        alt = clean_text(img.get("alt", ""))
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        text = f"{alt} {src or ''}".lower()
        if src and any(x in text for x in ["size chart", "size guide", "measurement", "sizeguide", "size_chart"]):
            candidates.append(src)
    return candidates[:5]

def fetch_with_browser(url: str):
    site = site_from_url(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            locale="en-IN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_MS,
            )
            page.wait_for_timeout(1500)

            # Generic, conservative size-guide interactions.
            selectors = [
                "text=/size\\s*(chart|guide)/i",
                "text=/size\\s*&\\s*fit/i",
                "text=/measurement\\s*(chart|guide)?/i",
            ]

            for selector in selectors:
                try:
                    loc = page.locator(selector).first
                    if loc.count() > 0 and loc.is_visible(timeout=1000):
                        loc.click(timeout=2000)
                        page.wait_for_timeout(500)
                        break
                except Exception:
                    pass

            html = page.content()
            status = response.status if response else None
            return html, status

        finally:
            context.close()
            browser.close()

def extract_product(url: str):
    site = site_from_url(url)

    # First pass: lightweight HTTP.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
    }

    try:
        with httpx.Client(
            headers=headers,
            follow_redirects=True,
            timeout=20,
        ) as client:
            r = client.get(url)
            html = r.text

            chart, method = extract_from_html(html)
            if chart:
                return {
                    "status": "found",
                    "site": site,
                    "method": method,
                    "source_url": str(r.url),
                    "chart": chart,
                }

            if r.status_code in (403, 429):
                return {
                    "status": "blocked",
                    "site": site,
                    "http_status": r.status_code,
                    "source_url": str(r.url),
                }

    except Exception as e:
        log.warning("HTTP fetch failed for %s: %s", url, e)

    # Second pass: browser-rendered page.
    try:
        html, http_status = fetch_with_browser(url)
        chart, method = extract_from_html(html)

        if chart:
            return {
                "status": "found",
                "site": site,
                "method": "playwright_" + method,
                "source_url": url,
                "chart": chart,
            }

        soup = BeautifulSoup(html, "lxml")
        images = find_size_chart_image(soup)

        if images:
            return {
                "status": "image_chart",
                "site": site,
                "method": "chart_image_candidate",
                "source_url": url,
                "image_urls": images,
            }

        if http_status in (403, 429):
            return {
                "status": "blocked",
                "site": site,
                "http_status": http_status,
                "source_url": url,
            }

        return {
            "status": "not_found",
            "site": site,
            "source_url": url,
        }

    except PlaywrightTimeoutError:
        return {"status": "failed", "site": site, "error": "browser_timeout"}
    except Exception as e:
        return {"status": "failed", "site": site, "error": str(e)}

def save_chart(conn, job_id, product_id, result):
    chart = result["chart"]
    rows = chart["rows"]
    headers = chart["headers"]
    unit = chart.get("unit")

    # Determine which column is the size label.
    size_index = None
    for i, h in enumerate(headers):
        if normalize_key(h) == "size":
            size_index = i
            break

    if size_index is None:
        # Fall back to first column.
        size_index = 0

    inserted = 0

    for row in rows:
        if size_index >= len(row):
            continue

        size_label = clean_text(row[size_index])
        if not size_label:
            continue

        measurements = {}

        for i, value in enumerate(row):
            if i >= len(headers) or i == size_index:
                continue

            key = normalize_key(headers[i])
            if not key:
                continue

            # Keep the original value. This preserves ranges such as "36-38".
            measurements[key] = value

        if not measurements:
            continue

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gsa_product_measurement (
                    product_id,
                    size_label,
                    measurements,
                    unit,
                    chart_type,
                    source,
                    source_url,
                    extraction_method,
                    raw_data,
                    confidence
                )
                VALUES (
                    %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, %s
                )
                ON CONFLICT (product_id, size_label)
                DO UPDATE SET
                    measurements = EXCLUDED.measurements,
                    unit = EXCLUDED.unit,
                    chart_type = EXCLUDED.chart_type,
                    source = EXCLUDED.source,
                    source_url = EXCLUDED.source_url,
                    extraction_method = EXCLUDED.extraction_method,
                    raw_data = EXCLUDED.raw_data,
                    confidence = EXCLUDED.confidence,
                    updated_at = NOW()
                """,
                (
                    product_id,
                    size_label,
                    json.dumps(measurements),
                    unit,
                    None,
                    result.get("site"),
                    result.get("source_url"),
                    result.get("method"),
                    json.dumps(chart),
                    0.90,
                ),
            )
        inserted += 1

    if inserted == 0:
        raise RuntimeError("Chart was detected but contained no usable size rows")

    return inserted

def process_once():
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM claim_size_chart_jobs(%s, %s)",
                (WORKER_ID, BATCH_SIZE),
            )
            jobs = cur.fetchall()

    if not jobs:
        return 0

    processed = 0

    for job in jobs:
        job_id = job["job_id"]
        product_id = job["product_id"]
        url = job["product_url"]

        log.info(
            "Processing job=%s product=%s site=%s url=%s",
            job_id, product_id, site_from_url(url), url
        )

        try:
            result = extract_product(url)

            with pool.connection() as conn:
                if result["status"] == "found":
                    count = save_chart(conn, job_id, product_id, result)
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT finish_size_chart_job(
                                %s, 'found', NULL, NULL, 0
                            )
                            """,
                            (job_id,),
                        )
                    log.info(
                        "Found chart product=%s rows=%s",
                        product_id, count
                    )

                elif result["status"] == "blocked":
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT finish_size_chart_job(
                                %s, 'blocked', %s, %s, %s
                            )
                            """,
                            (
                                job_id,
                                "Website returned a blocking/rate-limit response",
                                result.get("http_status"),
                                6 * 3600,
                            ),
                        )

                elif result["status"] == "image_chart":
                    # Do not claim success until OCR/vision has produced
                    # structured measurements.
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT finish_size_chart_job(
                                %s, 'failed', %s, NULL, %s
                            )
                            """,
                            (
                                job_id,
                                "Size-chart image found; OCR/vision adapter required",
                                2 * 3600,
                            ),
                        )

                elif result["status"] == "not_found":
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT finish_size_chart_job(
                                %s, 'not_found', NULL, NULL, %s
                            )
                            """,
                            (job_id, 24 * 3600),
                        )

                else:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT finish_size_chart_job(
                                %s, 'failed', %s, NULL, %s
                            )
                            """,
                            (
                                job_id,
                                result.get("error", "Unknown extraction failure"),
                                2 * 3600,
                            ),
                        )

            processed += 1

        except Exception as e:
            log.exception(
                "Job failed job=%s product=%s",
                job_id, product_id
            )
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT finish_size_chart_job(
                            %s, 'failed', %s, NULL, %s
                        )
                        """,
                        (job_id, str(e)[:4000], 2 * 3600),
                    )

    return processed

def main():
    log.info("Starting size-chart worker %s", WORKER_ID)

    while True:
        try:
            count = process_once()
            if count == 0:
                time.sleep(POLL_SECONDS)
        except Exception:
            log.exception("Worker loop error")
            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()

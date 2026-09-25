# Production size-chart worker

This package is designed for:

- DigitalOcean App Platform
- DigitalOcean Managed PostgreSQL
- `gsa_product`
- `gsa_product_measurement`
- 100K–1M products
- a 30-minute enqueue schedule
- horizontally scalable background workers

## Architecture

`gsa_product`
→ scheduled enqueue job
→ `size_chart_jobs`
→ App Platform background workers
→ product page extraction
→ `gsa_product_measurement`

The scheduler only creates/refreshes work. The workers process jobs continuously. This is preferable to trying to scrape thousands of products inside a single cron invocation.

## 1. Database migration

Run:

`sql/001_size_chart_schema.sql`

IMPORTANT: it assumes `gsa_product.id`, `gsa_product.product_url`, and `gsa_product.is_active`.

Verify your real schema before production.

## 2. Database connection

Set `DATABASE_URL` to your DigitalOcean PostgreSQL connection string. Prefer a DigitalOcean PostgreSQL connection pool/PgBouncer endpoint for high concurrency.

## 3. Local test

From `app/`:

```
pip install -r requirements.txt -r requirements-dev.txt
playwright install chromium
```

Unit + fixture tests (no database, no network):

```
python -m pytest
```

Database tests need a disposable PostgreSQL whose database name contains
`test` (the tests DROP and recreate tables from `sql/001_size_chart_schema.sql`):

```
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/sizechart_test python -m pytest
```

Try the extractor without touching the database:

```
python worker.py extract-url "https://www.myntra.com/shirts/cahoot/cahoot-men-comfort-opaque-checked-casual-shirt/31472271/buy" --no-browser
python worker.py extract-file saved_page.html --url "<the page's product URL>"
```

Process one real product read-only (resolves + extracts, writes nothing):

```
DATABASE_URL='postgresql://...?...sslmode=require' python worker.py dry-run --product-id 123
```

Run the queue worker (only when you intend to):

```
DATABASE_URL='...' WORKER_ENABLED=true BATCH_SIZE=1 python worker.py
```

`WORKER_ENABLED` defaults to `false`; a disabled worker stays alive and idle
(App Platform restarts workers that exit) and opens no database connection.

## 4. App Platform

Use `app/app.yaml.example` as a template and merge the worker/job components into your existing App Platform spec.

Recommended initial production setup:

- 2 worker instances
- BATCH_SIZE=10 per worker
- DB_POOL_SIZE=5
- enqueue every 30 minutes
- stale-job recovery every 30 minutes

Scale worker instances after measuring retailer response times and database load.

## 5. What the worker does

Code layout:

- `url_resolution.py`: product URL -> retailer product page
- `extraction.py`: pure parsing/validation, no network or DB
- `worker.py`: queue, fetching, persistence, CLI

Per job:

1. **Resolve the retailer URL.** Direct retailer URLs are used as-is. For Google Shopping URLs the worker uses a destination embedded in the URL (`?url=`, `?q=`, `?adurl=`) or a retailer link already stored in `gsa_product.raw`. It does not fetch Google unless `GOOGLE_HTTP_RESOLUTION=true`, and a Google challenge page is recorded as `blocked`. A Google URL with no retailer link becomes `url_resolution_status='unresolved'`, and the product is marked `not_found` with `size_chart_error='retailer_url_unresolved: ...'`. A successful resolution is cached in `resolved_product_url` and reused.
2. **Fetch over HTTP first.** The response is size-capped and uses a realistic User-Agent, Accept and Accept-Language.
3. **Extract**, in priority order:
   1. The retailer adapter. Myntra reads `window.__myx.pdpData.sizes[].measurements`, matched by the exact `window.__myx =` assignment, never `__myx_deviceType__`. `pdpData.id` must equal the style id in the URL, and only sizes whose `styleId` matches are used. GARMENT measurements are preferred over BODY ones, and the two are never mixed.
   2. Generic embedded JSON: `ld+json`, `__NEXT_DATA__`, `window.__INITIAL_STATE__ = ...`, and so on.
   3. HTML `<table>` charts, including transposed ones.
   4. A headless browser, only when the page was reachable but had no chart. It clicks a size-guide control once.
   5. Size-chart image detection. The product is marked `pending_ocr` and the image URLs go in `size_chart_error`. Nothing is stored as measurements.
4. **Validate.** A chart needs at least 2 size rows. Each row needs at least 1 recognised measurement field (chest, waist, length, ...) with a numeric value. Availability, stock, price and size-conversion columns are never measurements. A page without an id match whose title clearly differs from `gsa_product.title` is rejected as `product_mismatch`.
5. **Persist in one transaction.** This covers the `gsa_product_measurement` upsert (`ON CONFLICT (product_id, size_label)`, removing sizes no longer in the chart), `finish_size_chart_job(...)`, the `gsa_product.size_chart_status` update, and the URL-resolution columns.

Status mapping:

| outcome | job (`finish_size_chart_job`) | `gsa_product.size_chart_status` | retry |
|---|---|---|---|
| chart found | found | found | - |
| no product-specific chart / product mismatch / unresolved Google URL | not_found | not_found | 7 days |
| size-chart image only | not_found | pending_ocr | 7 days |
| CAPTCHA / anti-bot / 403 / 429 | blocked | blocked | 6h, doubling, max 72h |
| network / unexpected error | failed | failed | 30m, doubling, max 24h |

After `MAX_ATTEMPTS` (default 5), failed and blocked jobs are retried weekly.
`pending_ocr` is not a valid `size_chart_jobs.status` in the schema's CHECK
constraint, so it only appears on `gsa_product`.

The worker claims with `claim_size_chart_jobs(%s::text, %s::integer)`. Without
the casts, psycopg sends a small Python int as `smallint` and PostgreSQL picks
the `(text, smallint)` overload.

## Important production limitation

Retailer page structures, access rules, login requirements, bot protection and size-chart implementations change. The generic extractor is the safe foundation, but each retailer should have a tested adapter and regression tests using current, permitted product URLs.

The current package intentionally does not bypass CAPTCHAs, bot protection, login walls, or other access controls.

Image-only charts are identified (`pending_ocr`) but are not stored as measurements until an OCR/vision adapter is added. Headless Chromium is refused by some retailers (e.g. Myntra returns `ERR_HTTP2_PROTOCOL_ERROR`); the worker records that and does not try to get around it. Myntra data comes from plain HTTP.

## Scaling

For 100K–1M products, do not run all work in one 30-minute job.

The 30-minute job only enqueues work. Workers continuously drain the queue. Add more App Platform worker instances as throughput requires.

The PostgreSQL claim function uses `FOR UPDATE SKIP LOCKED`, so multiple workers can safely process different products concurrently.

## Recommended next production step

Add a retailer adapter layer and an OCR/vision adapter, then run a 100-product pilot for each retailer before processing the entire catalog.

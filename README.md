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

`pip install -r requirements.txt`

Install browser:

`playwright install chromium`

Then:

`DATABASE_URL='...' python enqueue.py`

and:

`DATABASE_URL='...' python worker.py`

Start with one worker and a tiny enqueue limit.

## 4. App Platform

Use `app/app.yaml.example` as a template and merge the worker/job components into your existing App Platform spec.

Recommended initial production setup:

- 2 worker instances
- BATCH_SIZE=10 per worker
- DB_POOL_SIZE=5
- enqueue every 30 minutes
- stale-job recovery every 30 minutes

Scale worker instances after measuring retailer response times and database load.

## 5. What the extractor currently does

1. Detects Amazon, Myntra, AJIO, Flipkart, Nykaa or other domains.
2. Attempts a normal HTTP request.
3. Searches real HTML tables for size/measurement columns.
4. Uses Playwright when JavaScript rendering is required.
5. Attempts conservative clicks on size-guide/size-chart controls.
6. Detects likely size-chart images.
7. Stores only extracted measurements as `found`.
8. Does NOT fabricate measurements.
9. Marks blocked pages separately.
10. Retries failures with backoff.

## Important production limitation

Retailer page structures, access rules, login requirements, bot protection and size-chart implementations change. The generic extractor is the safe foundation, but each retailer should have a tested adapter and regression tests using current, permitted product URLs.

The current package intentionally does not bypass CAPTCHAs, bot protection, login walls, or other access controls.

Image-only charts are identified but are not marked as verified measurements until an OCR/vision adapter is added.

## Scaling

For 100K–1M products, do not run all work in one 30-minute job.

The 30-minute job only enqueues work. Workers continuously drain the queue. Add more App Platform worker instances as throughput requires.

The PostgreSQL claim function uses `FOR UPDATE SKIP LOCKED`, so multiple workers can safely process different products concurrently.

## Recommended next production step

Add a retailer adapter layer and an OCR/vision adapter, then run a 100-product pilot for each retailer before processing the entire catalog.

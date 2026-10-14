# Size-chart worker

Finds the measurement chart (chest, waist, length, …) that belongs to each
active product in `products` and saves it to `product_size_charts`.

Only a chart that provably belongs to that product is saved. Store-wide or
category size guides (theme popups, size-chart apps, the same table on every
page) are recorded as `generic_chart` and not saved. Available/purchasable
sizes are never treated as a chart.

## Tables

| Table | Used for |
|---|---|
| `products` | read: `id`, `product_url`, `title`, `shopify_product_id`, `is_active` |
| `product_size_charts` | one row per product (`UNIQUE (product_id)`), upserted when a chart is found |
| `product_size_chart_checks` | one row per product (`PRIMARY KEY (product_id)`): latest `result` + `checked_at` |

`gsa_product`, `gsa_product_measurement` and `size_chart_jobs` are not used.
No migration is needed; the worker uses the existing tables as they are.

`product_size_charts.chart` looks like:

```json
{
  "sizes": {"38": {"chest": "44.0", "length": "27.5", "shoulder": "20.5"}, "40": {"...": "..."}},
  "size_order": ["38", "40", "42", "44"],
  "unit": "inch",
  "measurement_basis": "garment",
  "confidence": 0.95,
  "evidence": {"product_id_in_page": true, "...": "..."},
  "raw_rows": {"...": "..."},
  "extracted_at": "2026-10-14T10:00:00+00:00"
}
```

JSONB doesn't keep key order, so `size_order` holds the chart's size order.
The `unit` column is `inch`, `cm`, `mm`, or `unknown` when the page doesn't say.

## How products are picked

Each batch claims active products that are either unchecked or due for a
re-check, and marks them `processing` in `product_size_chart_checks` in the
same statement (`FOR UPDATE SKIP LOCKED`), so several workers never pick the
same product. Unchecked products come first.

| `result` | Meaning | Checked again after |
|---|---|---|
| `found` | chart saved to `product_size_charts` | never (automatically) |
| `not_found` | no product-specific measurement chart, or the page is a different product | 30 days |
| `generic_chart` | a chart exists but isn't tied to this product (store-wide/category guide): not saved | 30 days |
| `pending_ocr` | a size-chart image in the product's own description, OCR off (no `GEMINI_API_KEY`) | 30 days |
| `ocr_review` | the two Gemini reads of the image disagreed (or it held several charts): not saved | 30 days |
| `blocked` | CAPTCHA / anti-bot / HTTP 403 / 429 | 24 hours |
| `failed` | network or unexpected error | 6 hours |
| `processing` | being worked on now; older than 30 min means a worker died | 30 minutes |

Error details are written to the worker log (`product_done ... error=...`),
because the checks table has no error column. To re-check a product, delete
its row from `product_size_chart_checks`.

## Extraction

A chart is saved only from a source tied to this one product:

1. **Shopify product JSON** (`<product_url>.js`). Its `id` must equal
   `products.shopify_product_id`. A measurement table in its `description` (the
   merchant's per-product description) is that product's chart. When this JSON is
   available the page isn't fetched. A wrong `id` gives `not_found` (product mismatch).
2. **The product page** (stores without that JSON), keeping only:
   - retailer data tied to the product id (e.g. Myntra `window.__myx.pdpData.sizes[].measurements`, id must match the URL),
   - embedded JSON found under the product's own id,
   - a `<table>` inside the product's description block (`itemprop="description"` or a description class/id).
   Any other chart on the page is `generic_chart`.
3. **Headless Chromium**, only for non-Shopify pages that loaded with no chart at all (it clicks a size-guide control once). The same rules apply.

Every chart is also validated: at least 2 sizes, each with at least one
recognised measurement field (chest, waist, hip, length, sleeve, inseam, …) and a numeric value.
Stock, price and size-conversion columns never count.

### Size-chart images (OCR with Gemini)

A size-chart image in the product's own description is read with Gemini when
`GEMINI_API_KEY` is set (`ocr.py`). It is saved only if:

- Gemini reports it is a measurement chart, and only one chart is in the image;
- the image is read **twice** with differently worded instructions and both reads give the same sizes, values and unit;
- the chart passes the same validation as tables;
- at least 2 of its sizes are among the product's own `products.sizes`.

Such charts are saved with `source = 'image'`, `extraction_method = 'image_ocr'`,
confidence 0.70, and the image URL in `chart.evidence.image_url`. Reads that
disagree become `ocr_review`; a chart for other sizes is `not_found`; a Gemini
error is `failed` (retried after 6 hours). Images on the page outside the
description are never read (`generic_chart`).
CAPTCHAs and bot protection are recorded as `blocked`, never bypassed.

## Settings

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | required | `postgresql://USER:PASSWORD@HOST:PORT/DB?sslmode=require` (no `+asyncpg`) |
| `WORKER_MODE` | `0` | `0` = stopped (stays alive, no DB connection), `1` = process products |
| `WORKER_ID` | hostname | shown in logs |
| `BATCH_SIZE` | `10` | products claimed per round |
| `POLL_SECONDS` | `10` | wait when nothing is due |
| `DB_POOL_SIZE` | `5` | max DB connections per worker |
| `HTTP_TIMEOUT_SECONDS` | `25` | per request |
| `NAV_TIMEOUT_MS` | `45000` | browser page load |
| `BROWSER_WAIT_MS` | `1500` | wait after load / after clicking the size guide |
| `BROWSER_ENABLED` | `true` | `false` = HTTP only |
| `BROWSER_RESTART_EVERY` | `50` | restart Chromium after N pages |
| `MAX_CHART_ROWS` | `100` | larger "charts" are rejected |
| `MAX_HTML_BYTES` | `8000000` | max page download |
| `GEMINI_API_KEY` | empty | enables OCR of size-chart images; keep it secret (runtime only) |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Gemini model used for OCR |
| `OCR_ENABLED` | `true` | `false` turns OCR off even with a key |
| `OCR_MAX_IMAGES` | `2` | images read per product (each read twice) |
| `OCR_TIMEOUT_SECONDS` | `60` | per Gemini request |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `text` | `LOG_FORMAT=json` for JSON lines |

If `WORKER_MODE` is unset, `WORKER_ENABLED=true` still means mode `1`.

## Deploy (Coolify)

- Build pack: **Dockerfile**, base directory `/app`, Dockerfile `/Dockerfile`
- No domain, health check off (it's a background worker with no HTTP port)
- Environment variables as above; start with `WORKER_MODE=0`
- The database firewall must allow the Coolify server's IP

## Local test

From `app/`:

```
pip install -r requirements.txt -r requirements-dev.txt
playwright install chromium
python -m pytest
```

Database tests need a disposable PostgreSQL whose database name contains
`test`; they create the three tables themselves:

```
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/sizechart_test python -m pytest
```

Without touching the database:

```
python worker.py extract-url "https://store.example.com/products/some-handle" --no-browser
python worker.py extract-file saved_page.html --url "<the page's product URL>"
```

One real product from `products`, read-only (prints what it would save):

```
DATABASE_URL='postgresql://...?sslmode=require' python worker.py dry-run --product-id 123
```

## Legacy files

`sql/001_size_chart_schema.sql`, `enqueue.py`, `recover.py`, `Dockerfile.scheduler`
and the scheduled jobs in `app.yaml.example` belong to the earlier
`gsa_product` / `size_chart_jobs` design and are not needed for `products`.

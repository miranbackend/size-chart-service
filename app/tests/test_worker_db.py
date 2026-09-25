"""
Database tests against a real, disposable PostgreSQL.

    TEST_DATABASE_URL=postgresql://postgres@localhost:55432/sizechart_test pytest

The schema is rebuilt from sql/001_size_chart_schema.sql. The database name
must contain "test" because these tests DROP tables.
"""

import gzip
import json
import os
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

import worker
from extraction import Chart

TEST_DB = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
MYNTRA_URL = ("https://www.myntra.com/shirts/cahoot/"
              "cahoot-men-comfort-opaque-checked-casual-shirt/31472271/buy")
GOOGLE_URL = "https://www.google.com/search?ibp=oshop&q=cahoot+shirt&prds=catalogid:1&tbm=shop"

LONG = "<p>" + "Product description and details. " * 150 + "</p>"
PAGES = {
    "/captcha": (200, "<html><head><title>Robot Check</title></head><body>"
                      "Enter the characters you see below</body></html>"),
    "/sizes-only": (200, f"<html><body><ul><li>S</li><li>M</li><li>L</li></ul>"
                         f"<table><tr><th>Size</th><th>Stock</th></tr><tr><td>S</td><td>Available</td></tr>"
                         f"<tr><td>M</td><td>Available</td></tr></table>{LONG}</body></html>"),
    "/image-chart": (200, f"<html><body><img src='/img/kurta-size-chart.jpg' alt='Size Chart'>"
                          f"{LONG}</body></html>"),
}


def _schema_sql() -> str:
    return """
    DROP TABLE IF EXISTS gsa_product_measurement, size_chart_jobs, gsa_product CASCADE;
    DROP FUNCTION IF EXISTS claim_size_chart_jobs(text, smallint);
    CREATE TABLE gsa_product (
        id BIGINT PRIMARY KEY,
        title TEXT,
        product_url TEXT,
        raw JSONB,
        sizes JSONB,
        is_active BOOLEAN DEFAULT TRUE,
        resolved_product_url TEXT,
        resolved_retailer VARCHAR(200),
        url_resolution_status VARCHAR(30),
        url_resolution_error TEXT,
        url_resolved_at TIMESTAMPTZ
    );
    """


@pytest.fixture(scope="module")
def db():
    if "test" not in psycopg.conninfo.conninfo_to_dict(TEST_DB).get("dbname", ""):
        pytest.fail("TEST_DATABASE_URL database name must contain 'test'")
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute(_schema_sql())
        conn.execute((ROOT / "sql" / "001_size_chart_schema.sql").read_text())
        # Production also has a smallint overload; make sure the worker never
        # resolves to it by accident.
        conn.execute("""
            CREATE FUNCTION claim_size_chart_jobs(p_worker_id text, p_limit smallint)
            RETURNS TABLE (job_id bigint, product_id bigint, product_url text)
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'smallint overload called'; END $$;
        """)
    os.environ["DATABASE_URL"] = TEST_DB
    worker._pool = None
    yield
    worker.close_pool()


@pytest.fixture
def conn(db):
    with psycopg.connect(TEST_DB, autocommit=True, row_factory=dict_row) as c:
        c.execute("TRUNCATE gsa_product_measurement, size_chart_jobs, gsa_product CASCADE")
        yield c


@pytest.fixture
def http(monkeypatch):
    """Serve fixtures instead of the internet; any Google request fails the test."""
    myntra = gzip.open(FIXTURES / "myntra_31472271.html.gz", "rt", encoding="utf-8").read()
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        if "google." in request.url.host:
            raise AssertionError("worker must not fetch Google Shopping URLs")
        if request.url.host == "down.example.com":
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.host == "www.myntra.com":
            return httpx.Response(200, text=myntra, headers={"content-type": "text/html"})
        status, body = PAGES.get(request.url.path, (404, "not found"))
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(worker, "_http_client", client)
    monkeypatch.setattr(worker.SETTINGS, "browser_enabled", False)
    monkeypatch.setattr(worker.SETTINGS, "batch_size", 20)
    yield calls
    client.close()


def add_product(conn, pid, url, raw=None, title=None):
    conn.execute("INSERT INTO gsa_product (id, title, product_url, raw) VALUES (%s, %s, %s, %s)",
                 (pid, title, url, json.dumps(raw) if raw is not None else None))
    conn.execute("INSERT INTO size_chart_jobs (product_id) VALUES (%s)", (pid,))


def job(conn, pid):
    return conn.execute("SELECT * FROM size_chart_jobs WHERE product_id = %s", (pid,)).fetchone()


def product(conn, pid):
    return conn.execute("SELECT * FROM gsa_product WHERE id = %s", (pid,)).fetchone()


def measurements(conn, pid):
    rows = conn.execute("SELECT * FROM gsa_product_measurement WHERE product_id = %s ORDER BY size_label",
                        (pid,)).fetchall()
    return {r["size_label"]: r for r in rows}


# ------------------------------------------------------------------
# 11. UPSERT
# ------------------------------------------------------------------

def _chart(sizes):
    return Chart(sizes=sizes, unit="inch", method="html_table", source="other", confidence=0.75)


def test_upsert_is_idempotent_and_replaces_stale_sizes(conn):
    conn.execute("INSERT INTO gsa_product (id, product_url) VALUES (1, 'https://e.com/p')")
    chart = _chart({"S": {"chest": "38"}, "M": {"chest": "40"}, "L": {"chest": "42"}})

    with worker.get_pool().connection() as c, c.transaction():
        assert worker.save_chart(c, 1, chart, "https://e.com/p") == 3
    with worker.get_pool().connection() as c, c.transaction():
        worker.save_chart(c, 1, chart, "https://e.com/p")
    assert set(measurements(conn, 1)) == {"S", "M", "L"}

    updated = _chart({"S": {"chest": "38.5", "length": "27"}, "M": {"chest": "40.5"}})
    with worker.get_pool().connection() as c, c.transaction():
        worker.save_chart(c, 1, updated, "https://e.com/p")
    rows = measurements(conn, 1)
    assert set(rows) == {"S", "M"}
    assert rows["S"]["measurements"] == {"chest": "38.5", "length": "27"}
    assert rows["S"]["updated_at"] >= rows["S"]["created_at"]
    count = conn.execute("SELECT count(*) AS n FROM gsa_product_measurement").fetchone()["n"]
    assert count == 2


def test_save_chart_refuses_single_row(conn):
    conn.execute("INSERT INTO gsa_product (id, product_url) VALUES (2, 'https://e.com/p')")
    with pytest.raises(ValueError):
        with worker.get_pool().connection() as c, c.transaction():
            worker.save_chart(c, 2, _chart({"M": {"chest": "40"}}), "https://e.com/p")
    assert measurements(conn, 2) == {}


# ------------------------------------------------------------------
# 12. Queue processing + status handling
# ------------------------------------------------------------------

def test_google_product_resolved_via_raw_then_myntra_chart_saved(conn, http):
    add_product(conn, 115, GOOGLE_URL, raw={"product_link": GOOGLE_URL, "link": MYNTRA_URL},
                title="CAHOOT Men Comfort Opaque Checked Casual Shirt")

    assert worker.process_once() == 1

    rows = measurements(conn, 115)
    assert {k: v["measurements"] for k, v in rows.items()} == {
        "38": {"chest": "44.0", "length": "27.5", "shoulder": "20.5"},
        "40": {"chest": "46.0", "length": "28.0", "shoulder": "21.5"},
        "42": {"chest": "48.0", "length": "28.5", "shoulder": "22.5"},
        "44": {"chest": "50.0", "length": "29.0", "shoulder": "23.5"},
    }
    r = rows["40"]
    assert (r["unit"], r["chart_type"], r["source"], r["extraction_method"]) == \
        ("inch", "measurement_chart", "myntra", "embedded_json")
    assert r["source_url"] == MYNTRA_URL
    assert float(r["confidence"]) == 0.95
    assert r["raw_data"]["row"]["skuId"] == 101505229
    assert r["raw_data"]["evidence"]["product_id_match"] is True

    j, p = job(conn, 115), product(conn, 115)
    assert j["status"] == "found" and j["locked_by"] is None
    assert p["size_chart_status"] == "found" and p["size_chart_error"] is None
    assert p["url_resolution_status"] == "resolved"
    assert p["resolved_product_url"] == MYNTRA_URL
    assert p["resolved_retailer"] == "myntra"

    # Re-processing converges on the same 4 rows.
    conn.execute("UPDATE size_chart_jobs SET status = 'pending' WHERE product_id = 115")
    assert worker.process_once() == 1
    assert len(measurements(conn, 115)) == 4
    assert http.count(MYNTRA_URL) == 2  # the cached resolution was reused, Google never hit


def test_status_handling_for_each_outcome(conn, http):
    add_product(conn, 201, "https://shop.example.com/captcha")
    add_product(conn, 202, "https://shop.example.com/sizes-only")
    add_product(conn, 203, "https://shop.example.com/image-chart")
    add_product(conn, 204, GOOGLE_URL, raw={"product_link": GOOGLE_URL})
    add_product(conn, 205, "https://down.example.com/p/1")
    add_product(conn, 206, MYNTRA_URL.replace("31472271", "11111111"))

    assert worker.process_once() == 6

    blocked = job(conn, 201)
    assert blocked["status"] == "blocked"
    assert blocked["last_http_status"] == 200
    assert conn.execute("SELECT available_at > NOW() + interval '5 hours' AS later FROM size_chart_jobs "
                        "WHERE product_id = 201").fetchone()["later"]
    assert product(conn, 201)["size_chart_status"] == "blocked"

    assert job(conn, 202)["status"] == "not_found"
    assert product(conn, 202)["size_chart_status"] == "not_found"

    assert job(conn, 203)["status"] == "not_found"
    p203 = product(conn, 203)
    assert p203["size_chart_status"] == "pending_ocr"
    assert "https://shop.example.com/img/kurta-size-chart.jpg" in p203["size_chart_error"]

    p204 = product(conn, 204)
    assert job(conn, 204)["status"] == "not_found"
    assert p204["size_chart_status"] == "not_found"
    assert p204["url_resolution_status"] == "unresolved"
    assert p204["resolved_product_url"] is None
    assert p204["size_chart_error"].startswith("retailer_url_unresolved")

    failed = job(conn, 205)
    assert failed["status"] == "failed"
    assert "ConnectError" in failed["last_error"]
    assert product(conn, 205)["size_chart_status"] == "failed"

    assert job(conn, 206)["status"] == "not_found"
    assert product(conn, 206)["size_chart_error"].startswith("product_mismatch")

    for pid in (201, 202, 203, 204, 205, 206):
        assert measurements(conn, pid) == {}, pid
    assert not any("google." in u for u in http)


def test_unexpected_extraction_error_marks_failed(conn, http, monkeypatch):
    add_product(conn, 301, "https://shop.example.com/sizes-only")

    def explode(*a, **k):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(worker, "extract_product", explode)
    assert worker.process_once() == 1
    assert job(conn, 301)["status"] == "failed"
    assert "parser exploded" in product(conn, 301)["size_chart_error"]


def test_nothing_to_claim_returns_zero(conn, http):
    assert worker.process_once() == 0

BEGIN;

-- This migration assumes gsa_product has:
--   id BIGINT
--   product_url TEXT
--   is_active BOOLEAN
-- If your production column names differ, change the three references
-- in the enqueue function below before running this migration.

CREATE TABLE IF NOT EXISTS size_chart_jobs (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL
        REFERENCES gsa_product(id) ON DELETE CASCADE,

    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','found','not_found','blocked','failed')),

    priority SMALLINT NOT NULL DEFAULT 100,
    attempts INTEGER NOT NULL DEFAULT 0,

    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_at TIMESTAMPTZ,
    locked_by TEXT,

    last_error TEXT,
    last_http_status INTEGER,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (product_id)
);

CREATE INDEX IF NOT EXISTS idx_size_chart_jobs_claim
ON size_chart_jobs (priority, available_at, id)
WHERE status IN ('pending','failed');

CREATE INDEX IF NOT EXISTS idx_size_chart_jobs_processing
ON size_chart_jobs (locked_at)
WHERE status = 'processing';

-- Keep the flexible measurement model.
-- One row = one product/size; measurements contains category-specific fields.
CREATE TABLE IF NOT EXISTS gsa_product_measurement (
    id BIGSERIAL PRIMARY KEY,

    product_id BIGINT NOT NULL
        REFERENCES gsa_product(id) ON DELETE CASCADE,

    size_label VARCHAR(100) NOT NULL,

    measurements JSONB NOT NULL DEFAULT '{}'::jsonb,

    unit VARCHAR(20),

    chart_type VARCHAR(100),

    source VARCHAR(100),

    source_url TEXT,

    extraction_method VARCHAR(50),

    raw_data JSONB,

    confidence NUMERIC(5,4),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (product_id, size_label)
);

CREATE INDEX IF NOT EXISTS idx_gsa_product_measurement_product
ON gsa_product_measurement(product_id);

CREATE INDEX IF NOT EXISTS idx_gsa_product_measurement_json
ON gsa_product_measurement USING GIN(measurements);

-- Tracking columns on the product table.
ALTER TABLE gsa_product
    ADD COLUMN IF NOT EXISTS size_chart_status VARCHAR(30) DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS size_chart_last_checked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS size_chart_error TEXT;

-- Put products without measurements into the queue.
CREATE OR REPLACE FUNCTION enqueue_size_chart_jobs(p_limit INTEGER DEFAULT 5000)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_count INTEGER;
BEGIN
    INSERT INTO size_chart_jobs(product_id)
    SELECT p.id
    FROM gsa_product p
    WHERE COALESCE(p.is_active, TRUE) = TRUE
      AND NULLIF(TRIM(p.product_url), '') IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM gsa_product_measurement m
          WHERE m.product_id = p.id
      )
    ORDER BY p.id
    LIMIT p_limit
    ON CONFLICT (product_id) DO UPDATE
       SET status = CASE
           WHEN size_chart_jobs.status IN ('not_found','blocked')
                AND size_chart_jobs.attempts < 5
           THEN 'pending'
           ELSE size_chart_jobs.status
       END,
       available_at = CASE
           WHEN size_chart_jobs.status IN ('not_found','blocked')
                AND size_chart_jobs.attempts < 5
           THEN NOW()
           ELSE size_chart_jobs.available_at
       END,
       updated_at = NOW();

    GET DIAGNOSTICS v_count = ROW_COUNT;

    UPDATE gsa_product p
    SET size_chart_status = 'pending'
    WHERE p.id IN (
        SELECT sj.product_id
        FROM size_chart_jobs sj
        WHERE sj.status IN ('pending','failed')
    );

    RETURN v_count;
END;
$$;

-- Safely claim jobs from multiple workers/instances.
CREATE OR REPLACE FUNCTION claim_size_chart_jobs(
    p_worker_id TEXT,
    p_limit INTEGER DEFAULT 25
)
RETURNS TABLE (
    job_id BIGINT,
    product_id BIGINT,
    product_url TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH picked AS (
        SELECT j.id
        FROM size_chart_jobs j
        WHERE j.status IN ('pending','failed')
          AND j.available_at <= NOW()
        ORDER BY j.priority ASC, j.available_at ASC, j.id ASC
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    ),
    claimed AS (
        UPDATE size_chart_jobs j
        SET status = 'processing',
            attempts = j.attempts + 1,
            locked_at = NOW(),
            locked_by = p_worker_id,
            updated_at = NOW()
        FROM picked
        WHERE j.id = picked.id
        RETURNING j.id, j.product_id
    )
    SELECT c.id, c.product_id, p.product_url
    FROM claimed c
    JOIN gsa_product p ON p.id = c.product_id;
END;
$$;

CREATE OR REPLACE FUNCTION finish_size_chart_job(
    p_job_id BIGINT,
    p_status TEXT,
    p_error TEXT DEFAULT NULL,
    p_http_status INTEGER DEFAULT NULL,
    p_retry_seconds INTEGER DEFAULT 3600
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    v_product_id BIGINT;
BEGIN
    SELECT product_id INTO v_product_id
    FROM size_chart_jobs
    WHERE id = p_job_id;

    UPDATE size_chart_jobs
    SET status = p_status,
        last_error = p_error,
        last_http_status = p_http_status,
        locked_at = NULL,
        locked_by = NULL,
        available_at = CASE
            WHEN p_status IN ('failed','blocked')
            THEN NOW() + make_interval(secs => p_retry_seconds)
            ELSE NOW()
        END,
        updated_at = NOW()
    WHERE id = p_job_id;

    UPDATE gsa_product
    SET size_chart_status = p_status,
        size_chart_last_checked_at = NOW(),
        size_chart_error = p_error
    WHERE id = v_product_id;
END;
$$;

-- Recover jobs left locked by a crashed worker.
CREATE OR REPLACE FUNCTION recover_stale_size_chart_jobs(
    p_timeout_minutes INTEGER DEFAULT 30
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_count INTEGER;
BEGIN
    UPDATE size_chart_jobs
    SET status = 'failed',
        available_at = NOW(),
        locked_at = NULL,
        locked_by = NULL,
        last_error = COALESCE(last_error, 'Recovered stale processing job'),
        updated_at = NOW()
    WHERE status = 'processing'
      AND locked_at < NOW() - make_interval(mins => p_timeout_minutes);

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;

COMMIT;

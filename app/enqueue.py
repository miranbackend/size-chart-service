import os
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]
LIMIT = int(os.getenv("ENQUEUE_LIMIT", "5000"))

with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT enqueue_size_chart_jobs(%s)", (LIMIT,))
        count = cur.fetchone()[0]
        print(f"Enqueued/rechecked {count} size-chart jobs")

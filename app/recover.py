import os
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]
TIMEOUT = int(os.getenv("STALE_TIMEOUT_MINUTES", "30"))

with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT recover_stale_size_chart_jobs(%s)", (TIMEOUT,))
        count = cur.fetchone()[0]
        print(f"Recovered {count} stale jobs")

"""
Idempotently applies the PostgreSQL schema to the database specified by SUPABASE_DB_URL.
"""
import os
import sys
import psycopg
from dotenv import load_dotenv

load_dotenv()


def init_schema(db_url: str = None) -> None:
    url = db_url or os.environ.get("SUPABASE_DB_URL")
    if not url:
        print("ERROR: SUPABASE_DB_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    schema_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sql", "schema.sql")
    if not os.path.exists(schema_file):
        print(f"ERROR: Schema file not found at {schema_file}", file=sys.stderr)
        sys.exit(1)

    with open(schema_file, "r", encoding="utf-8") as f:
        sql = f.read()

    print(f"Applying schema to database: {url.split('@')[-1] if '@' in url else '***'}...")
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print("Schema applied successfully.")


if __name__ == "__main__":
    init_schema()


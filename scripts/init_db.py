"""Initialize an explicitly empty PostgreSQL database; never restore personal data."""
from pathlib import Path
import argparse
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-empty", action="store_true", required=True)
    parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from database import connect
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (742310029,))
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
            if cur.fetchone()[0]:
                raise RuntimeError("Refusing to initialize a non-empty database. Use the separate migration runner for upgrades.")
            cur.execute((ROOT / "schema/0000_baseline.sql").read_text(encoding="utf-8"))
            cur.execute("CREATE TABLE public.schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            for path in sorted((ROOT / "migrations").glob("*.sql")):
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute("INSERT INTO public.schema_migrations(version) VALUES (%s) ON CONFLICT(version) DO NOTHING", (path.stem,))
        conn.commit()
        print("Initialized empty schema and all migrations; no player/account data loaded.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    main()

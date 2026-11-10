"""Apply checked-in SQL migrations in filename order."""

from pathlib import Path

from database import connect


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def main() -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.commit()
            cur.execute("SELECT version FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

        for path in sorted(MIGRATIONS.glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations(version) VALUES (%s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (version,),
                )
            conn.commit()
            print(f"MIGRATION_APPLIED|{version}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

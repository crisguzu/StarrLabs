import os
import requests
import psycopg2
from database import connect as database_connect
from psycopg2.extras import execute_batch
from dotenv import load_dotenv

# 1. Load .env
load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

if not all([DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME]):
    raise RuntimeError("Some DB_* variables are missing in .env")


def get_db_connection():
    return database_connect()


def ensure_game_modes_table(conn):
    """
    We base uniqueness on `id` (primary key) and keep `sc_id` unique.
    IMPORTANT: `name` must NOT be UNIQUE, because BrawlAPI can return
    duplicate names with different ids (e.g., BRAWL-HOCKEY).
    """
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS game_modes (
      id          INTEGER PRIMARY KEY,     -- BrawlAPI id (unique)
      sc_id       INTEGER UNIQUE,          -- Supercell id (used in CDN URL)
      name        TEXT NOT NULL,           -- not unique (API duplicates possible)
      hash        TEXT,                    -- "Super-City-Rampage"
      sc_hash     TEXT,                    -- "superCityRampage"
      regular_url TEXT                     -- cdn.brawlify.com/game-modes/regular/<scId>.png
    );
    """

    # In older versions you had: name TEXT UNIQUE NOT NULL
    # That creates a constraint named: game_modes_name_key
    # We drop it if it exists so inserts won't fail.
    drop_name_unique_sql = """
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'game_modes_name_key'
      ) THEN
        ALTER TABLE game_modes DROP CONSTRAINT game_modes_name_key;
      END IF;
    END $$;
    """

    # Optional: index on name for faster lookups/filtering (not unique)
    create_name_index_sql = """
    CREATE INDEX IF NOT EXISTS idx_game_modes_name ON game_modes (name);
    """

    with conn.cursor() as cur:
        cur.execute(create_table_sql)
        cur.execute(drop_name_unique_sql)
        cur.execute(create_name_index_sql)
    conn.commit()


CDN_GAME_MODES_BASE = "https://cdn.brawlify.com/game-modes/regular"


def fetch_game_modes_from_api():
    """
    Uses BrawlAPI (no auth needed):
    https://api.brawlapi.com/v1/gamemodes
    """
    url = "https://api.brawlapi.com/v1/gamemodes"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    modes = data.get("list") or data.get("items") or data
    if isinstance(modes, dict):
        modes = list(modes.values())
    return modes


def upsert_game_modes(conn, modes):
    """
    Base on `id` (PRIMARY KEY).
    This will work safely only if `name` is not unique (see ensure_game_modes_table).
    """
    sql = """
    INSERT INTO game_modes (
      id, sc_id, name, hash, sc_hash, regular_url
    )
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE
    SET
      sc_id       = EXCLUDED.sc_id,
      name        = EXCLUDED.name,
      hash        = EXCLUDED.hash,
      sc_hash     = EXCLUDED.sc_hash,
      regular_url = EXCLUDED.regular_url;
    """

    rows = []
    for gm in modes:
        gm_id = gm.get("id")
        sc_id = gm.get("scId")
        name = (gm.get("name") or "").strip()

        if gm_id is None or sc_id is None or not name:
            continue

        hash_ = gm.get("hash")
        sc_hash = gm.get("scHash")
        regular_url = f"{CDN_GAME_MODES_BASE}/{int(sc_id)}.png"

        rows.append((gm_id, sc_id, name, hash_, sc_hash, regular_url))

    if not rows:
        print("No game modes to upsert.")
        return

    with conn.cursor() as cur:
        execute_batch(cur, sql, rows, page_size=100)
    conn.commit()
    print(f"Upserted {len(rows)} game modes.")


def main():
    print("Connecting to database...")
    conn = get_db_connection()

    try:
        print("Ensuring game_modes table exists (id-based uniqueness)...")
        ensure_game_modes_table(conn)

        print("Fetching game modes from BrawlAPI...")
        modes = fetch_game_modes_from_api()
        print(f"Fetched {len(modes)} game modes from API.")

        print("Upserting into game_modes...")
        upsert_game_modes(conn, modes)

        print("Done ✅")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

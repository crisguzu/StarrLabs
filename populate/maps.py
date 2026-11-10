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


def ensure_maps_table(conn):
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS maps (
      id                    INTEGER PRIMARY KEY,   -- BrawlAPI id (15000000)
      name                  TEXT NOT NULL,
      hash                  TEXT,
      link                  TEXT,
      image_url             TEXT,

      environment_id        INTEGER,
      environment_sc_id     INTEGER,
      environment_name      TEXT,
      environment_hash      TEXT,
      environment_image_url TEXT,

      game_mode_id          INTEGER,
      game_mode_sc_id       INTEGER,
      game_mode_name        TEXT,
      game_mode_hash        TEXT,
      game_mode_image_url   TEXT,

      last_active           TIMESTAMPTZ,
      data_updated          BIGINT
    );
    """
    with conn.cursor() as cur:
        cur.execute(create_table_sql)
    conn.commit()


def fetch_maps_from_api():
    """
    Uses BrawlAPI (no auth needed):

    https://api.brawlapi.com/v1/maps
    """
    url = "https://api.brawlapi.com/v1/maps"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    maps = data.get("list") or data.get("items") or data
    if isinstance(maps, dict):
        maps = list(maps.values())

    if maps:
        print("Sample map from API:", maps[0])

    return maps


def upsert_maps(conn, maps):
    sql = """
    INSERT INTO maps (
      id, name, hash, link, image_url,
      environment_id, environment_sc_id, environment_name, environment_hash, environment_image_url,
      game_mode_id, game_mode_sc_id, game_mode_name, game_mode_hash, game_mode_image_url,
      last_active, data_updated
    )
    VALUES (%s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s)
    ON CONFLICT (id) DO UPDATE
    SET
      name                  = EXCLUDED.name,
      hash                  = EXCLUDED.hash,
      link                  = EXCLUDED.link,
      image_url             = EXCLUDED.image_url,
      environment_id        = EXCLUDED.environment_id,
      environment_sc_id     = EXCLUDED.environment_sc_id,
      environment_name      = EXCLUDED.environment_name,
      environment_hash      = EXCLUDED.environment_hash,
      environment_image_url = EXCLUDED.environment_image_url,
      game_mode_id          = EXCLUDED.game_mode_id,
      game_mode_sc_id       = EXCLUDED.game_mode_sc_id,
      game_mode_name        = EXCLUDED.game_mode_name,
      game_mode_hash        = EXCLUDED.game_mode_hash,
      game_mode_image_url   = EXCLUDED.game_mode_image_url,
      last_active           = EXCLUDED.last_active,
      data_updated          = EXCLUDED.data_updated;
    """

    rows = []
    for m in maps:
        m_id = m.get("id")
        name = m.get("name")
        if m_id is None or not name:
            continue

        hash_ = m.get("hash")
        link = m.get("link")
        image_url = m.get("imageUrl")  # already like https://cdn.brawlify.com/maps/regular/15000000.png

        env = m.get("environment") or {}
        env_id = env.get("id")
        env_sc_id = env.get("scId")
        env_name = env.get("name")
        env_hash = env.get("hash")
        env_image_url = env.get("imageUrl")

        gm = m.get("gameMode") or {}
        gm_id = gm.get("id")
        gm_sc_id = gm.get("scId")
        gm_name = gm.get("name")
        gm_hash = gm.get("hash")
        gm_image_url = gm.get("imageUrl")

        last_active = m.get("lastActive")    # None or ISO string
        data_updated = m.get("dataUpdated")  # epoch (int)

        rows.append((
            m_id,
            name,
            hash_,
            link,
            image_url,
            env_id,
            env_sc_id,
            env_name,
            env_hash,
            env_image_url,
            gm_id,
            gm_sc_id,
            gm_name,
            gm_hash,
            gm_image_url,
            last_active,
            data_updated,
        ))

    if not rows:
        print("No maps to upsert (rows list is empty).")
        return

    with conn.cursor() as cur:
        execute_batch(cur, sql, rows, page_size=200)
    conn.commit()
    print(f"Upserted {len(rows)} maps.")


def main():
    print("Connecting to database...")
    conn = get_db_connection()

    try:
        print("Ensuring maps table exists...")
        ensure_maps_table(conn)

        print("Fetching maps from BrawlAPI...")
        maps = fetch_maps_from_api()
        print(f"Fetched {len(maps)} maps from API.")

        print("Upserting into maps...")
        upsert_maps(conn, maps)

        print("Done ✅")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

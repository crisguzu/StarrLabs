import os
import requests
import psycopg2
from database import connect as database_connect
from psycopg2.extras import execute_batch
from dotenv import load_dotenv

# 1. Load .env
load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")


if not API_TOKEN:
    raise RuntimeError("API_TOKEN is not set in .env")

if not all([DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME]):
    raise RuntimeError("Some DB_* variables are missing in .env")


def get_db_connection():
    return database_connect()


def ensure_brawlers_table(conn):
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS brawlers (
    id              INTEGER PRIMARY KEY,      -- e.g. 16000015 from BS API
    name            TEXT UNIQUE NOT NULL,     -- "Shelly"
    class           TEXT,                     -- "Damage Dealer"
    rarity          TEXT,                     -- "Mythic", etc.

    -- image variants
    borderless_url  TEXT,                     -- /brawlers/borderless/<id>.png
    border_url      TEXT,                     -- /brawlers/borders/<id>.png
    emoji_url       TEXT,                     -- /brawlers/emoji/<id>.png
    model_url       TEXT,                     -- /brawlers/models/<id>.png  (assumed)
    portrait_url    TEXT                      -- /brawlers/portraits/<id>.png (assumed)
    );
    """
    with conn.cursor() as cur:
        cur.execute(create_table_sql)
    conn.commit()

CDN_BASE = "https://cdn.brawlify.com/brawlers"

def build_image_urls(brawler_id: int) -> dict:
    bid = int(brawler_id)
    return {
        "borderless": f"{CDN_BASE}/borderless/{bid}.png",
        "borders":    f"{CDN_BASE}/borders/{bid}.png",
        "emoji":      f"{CDN_BASE}/emoji/{bid}.png",
        "model":      f"{CDN_BASE}/model/{bid}.png",      # adjust if needed
        "portrait":   f"{CDN_BASE}/portraits/{bid}.png",   # adjust if needed
    }

def fetch_brawlers_from_api():
    url = "https://api.brawlstars.com/v1/brawlers"
    headers = {"Authorization": f"Bearer {API_TOKEN}"}

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    return data.get("items", [])



def upsert_brawlers(conn, brawlers):
    sql = """
    INSERT INTO brawlers (
    id, name, class, rarity,
    borderless_url, border_url, emoji_url, model_url, portrait_url
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE
    SET
    name           = EXCLUDED.name,
    class          = EXCLUDED.class,
    rarity         = EXCLUDED.rarity,
    borderless_url = EXCLUDED.borderless_url,
    border_url     = EXCLUDED.border_url,
    emoji_url      = EXCLUDED.emoji_url,
    model_url      = EXCLUDED.model_url,
    portrait_url   = EXCLUDED.portrait_url;
    """

    rows = []
    for b in brawlers:  # from the API
        b_id = b.get("id")
        name = b.get("name")
        if not b_id or not name:
            continue

        pretty_name = name.title()

        cls = b.get("class")
        if isinstance(cls, dict):
            class_name = cls.get("name")
        else:
            class_name = cls

        rar = b.get("rarity")
        if isinstance(rar, dict):
            rarity_name = rar.get("name")
        else:
            rarity_name = rar

        urls = build_image_urls(b_id)

        rows.append((
            b_id,
            pretty_name,
            class_name,
            rarity_name,
            urls["borderless"],
            urls["borders"],
            urls["emoji"],
            urls["model"],
            urls["portrait"],
        ))

    if not rows:
        print("No brawlers to upsert.")
        return

    with conn.cursor() as cur:
        execute_batch(cur, sql, rows, page_size=100)
    conn.commit()
    print(f"Upserted {len(rows)} brawlers.")


def main():
    print("Connecting to database...")
    conn = get_db_connection()

    try:
        print("Ensuring brawlers table exists...")
        ensure_brawlers_table(conn)

        print("Fetching brawlers from Brawl Stars API...")
        brawlers = fetch_brawlers_from_api()
        print(f"Fetched {len(brawlers)} brawlers from API.")

        print("Upserting into database...")
        upsert_brawlers(conn, brawlers)

        print("Done ✅")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

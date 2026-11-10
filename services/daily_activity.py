# services/daily_activity.py
import os
import datetime as dt
from typing import Optional, Dict, Any, List

import psycopg2
from database import connect as database_connect
from psycopg2.extras import execute_batch

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

def get_conn():
    if not all([DB_HOST, DB_USER, DB_PASSWORD, DB_NAME]):
        raise RuntimeError("Some DB_* variables are missing in environment")
    return database_connect()


def normalize_tag(tag: str) -> str:
    tag = (tag or "").strip().upper()
    if not tag:
        return tag
    if not tag.startswith("#"):
        tag = "#" + tag
    return tag


def get_tracked_tags() -> List[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT player_tag FROM tracked_players ORDER BY player_tag;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [normalize_tag(r[0]) for r in rows]


def build_daily_rows(tags: List[str], activity_date: Optional[dt.date] = None) -> List[Dict[str, Any]]:
    day = activity_date or dt.datetime.utcnow().date()
    if not tags:
        return []
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        WITH latest AS (
          SELECT DISTINCT ON (player_tag)
            id, player_tag, trophies, highest_trophies, exp_level,
            victories_3v3, solo_victories, duo_victories
          FROM player_profile_snapshots
          WHERE player_tag = ANY(%s)
          ORDER BY player_tag, captured_at DESC
        ), brawler_totals AS (
          SELECT profile_snapshot_id, count(*) AS brawler_count,
                 sum(trophies) AS total_brawler_trophies
          FROM player_brawler_snapshots
          GROUP BY profile_snapshot_id
        )
        SELECT %s, l.player_tag, l.trophies, l.highest_trophies, l.exp_level,
               COALESCE(b.brawler_count, 0), COALESCE(b.total_brawler_trophies, 0),
               l.victories_3v3, l.solo_victories, l.duo_victories
        FROM latest l
        LEFT JOIN brawler_totals b ON b.profile_snapshot_id = l.id
        """,
        (tags, day),
    )
    keys = [
        "activity_date", "player_tag", "trophies", "highest_trophies", "exp_level",
        "brawler_count", "total_brawler_trophies", "victories_3v3",
        "victories_solo", "victories_duo",
    ]
    rows = [dict(zip(keys, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def save_daily_rows(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0

    conn = get_conn()
    cur = conn.cursor()

    sql = """
    INSERT INTO player_daily_activity (
      activity_date,
      player_tag,
      trophies,
      highest_trophies,
      exp_level,
      brawler_count,
      total_brawler_trophies,
      victories_3v3,
      victories_solo,
      victories_duo,
      last_update
    ) VALUES (
      %(activity_date)s,
      %(player_tag)s,
      %(trophies)s,
      %(highest_trophies)s,
      %(exp_level)s,
      %(brawler_count)s,
      %(total_brawler_trophies)s,
      %(victories_3v3)s,
      %(victories_solo)s,
      %(victories_duo)s,
      NOW()
    )
    ON CONFLICT (activity_date, player_tag) DO NOTHING;
    """

    execute_batch(cur, sql, rows, page_size=100)
    conn.commit()
    cur.close()
    conn.close()
    return len(rows)


# =========================================================
# ✅ MAIN callable function
# =========================================================

def snapshot_daily_activity(tags: List[str], activity_date: Optional[dt.date] = None) -> Dict[str, int]:
    cleaned = [normalize_tag(t) for t in (tags or [])]
    tags = sorted(set(t for t in cleaned if t))
    rows = build_daily_rows(tags, activity_date=activity_date)
    saved = save_daily_rows(rows)
    return {"players": len(tags), "rows_built": len(rows), "rows_saved": saved}


def main():
    tags = get_tracked_tags()
    out = snapshot_daily_activity(tags)
    print(f"[daily_activity] done: {out} @ {dt.datetime.now()}")


if __name__ == "__main__":
    main()

# services/player_brawler_daily_activity.py
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

def build_brawler_daily_rows(tags: List[str], activity_date: Optional[dt.date] = None) -> List[Dict[str, Any]]:
    day = activity_date or dt.datetime.utcnow().date()
    if not tags:
        return []
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        WITH latest AS (
          SELECT DISTINCT ON (player_tag) id, player_tag
          FROM player_profile_snapshots
          WHERE player_tag = ANY(%s)
          ORDER BY player_tag, captured_at DESC
        )
        SELECT %s, b.player_tag, b.brawler_name, b.trophies
        FROM latest l
        JOIN player_brawler_snapshots b ON b.profile_snapshot_id = l.id
        WHERE b.brawler_name IS NOT NULL
        """,
        (tags, day),
    )
    keys = ["activity_date", "player_tag", "brawler_name", "trophies"]
    rows = [dict(zip(keys, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def save_brawler_daily_rows(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0

    conn = get_conn()
    cur = conn.cursor()

    sql = """
    INSERT INTO player_brawler_daily_activity (
      activity_date,
      player_tag,
      brawler_name,
      trophies,
      games,
      wins,
      losses,
      draws
    ) VALUES (
      %(activity_date)s,
      %(player_tag)s,
      %(brawler_name)s,
      %(trophies)s,
      0, 0, 0, 0
    )
    ON CONFLICT (player_tag, brawler_name, activity_date)
    DO UPDATE SET
      trophies = EXCLUDED.trophies;
    """

    execute_batch(cur, sql, rows, page_size=200)
    conn.commit()
    cur.close()
    conn.close()
    return len(rows)


def update_brawler_daily_from_battles(tags: List[str], day: Optional[dt.date] = None) -> int:
    if not tags:
        return 0

    day = day or dt.datetime.utcnow().date()
    from_prefix = day.strftime("%Y%m%d")
    to_prefix = (day + dt.timedelta(days=1)).strftime("%Y%m%d")

    conn = get_conn()
    cur = conn.cursor()

    sql = """
    WITH daily AS (
      SELECT
        player_tag,
        brawler_name,
        LEFT(battleTime, 8) AS d,
        COUNT(*) AS games,
        COUNT(*) FILTER (WHERE result = 'victory') AS wins,
        COUNT(*) FILTER (WHERE result = 'defeat')  AS losses,
        COUNT(*) FILTER (WHERE result = 'draw')    AS draws
      FROM battles
      WHERE LEFT(battleTime, 8) >= %s
        AND LEFT(battleTime, 8) < %s
        AND player_tag = ANY(%s)
        AND brawler_name IS NOT NULL
      GROUP BY player_tag, brawler_name, LEFT(battleTime, 8)
    )
    INSERT INTO player_brawler_daily_activity (
      player_tag,
      brawler_name,
      activity_date,
      trophies,
      games,
      wins,
      losses,
      draws
    )
    SELECT
      player_tag,
      brawler_name,
      to_date(d, 'YYYYMMDD') AS activity_date,
      NULL AS trophies,
      games,
      wins,
      losses,
      draws
    FROM daily
    ON CONFLICT (player_tag, brawler_name, activity_date)
    DO UPDATE SET
      games  = EXCLUDED.games,
      wins   = EXCLUDED.wins,
      losses = EXCLUDED.losses,
      draws  = EXCLUDED.draws;
    """

    cur.execute(sql, (from_prefix, to_prefix, tags))
    updated = cur.rowcount

    conn.commit()
    cur.close()
    conn.close()
    return updated


# =========================================================
# ✅ MAIN callable function
# =========================================================

def snapshot_brawler_daily(tags: List[str], activity_date: Optional[dt.date] = None) -> Dict[str, int]:
    tags = sorted(set(normalize_tag(t) for t in (tags or []) if normalize_tag(t)))

    rows = build_brawler_daily_rows(tags, activity_date=activity_date)
    saved_snapshot = save_brawler_daily_rows(rows)
    updated_from_battles = update_brawler_daily_from_battles(tags, day=activity_date)

    return {
        "players": len(tags),
        "rows_built": len(rows),
        "rows_saved": saved_snapshot,
        "rows_updated_from_battles": updated_from_battles,
    }


def main():
    tags = get_tracked_tags()
    out = snapshot_brawler_daily(tags)
    print(f"[player_brawler_daily_activity] done: {out} @ {dt.datetime.now()}")


if __name__ == "__main__":
    main()

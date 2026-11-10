# backfill_player_brawler_daily.py
import os
import datetime as dt
from typing import Dict, Any, Tuple

import psycopg2
from database import connect as database_connect
from psycopg2.extras import execute_batch, RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")


def get_conn():
    return database_connect()


def backfill_for_range(days_back: int = 90):
    """
    Rebuild player_brawler_daily_activity.games / wins / losses / draws
    for the last `days_back` days based on the battles table.

    We treat the day as the *UTC calendar date* derived from battleTime.
    """
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Lower bound: today_utc - days_back
    today_utc = dt.datetime.utcnow().date()
    from_date = today_utc - dt.timedelta(days=days_back)
    from_prefix = from_date.strftime("%Y%m%d")

    # 1) Aggregate from battles
    # battleTime format: 'YYYYMMDDT...Z' so LEFT(battleTime, 8) is the UTC date.
    cur.execute(
        """
        SELECT
          b.player_tag,
          b.brawler_name,
          LEFT(b.battleTime, 8) AS d,   -- 'YYYYMMDD'
          COUNT(*) AS games,
          COUNT(*) FILTER (WHERE b.result = 'victory') AS wins,
          COUNT(*) FILTER (WHERE b.result = 'defeat')  AS losses,
          COUNT(*) FILTER (WHERE b.result = 'draw')    AS draws
        FROM battles b
        WHERE b.battleTime >= %s
          AND b.player_tag IS NOT NULL
          AND b.brawler_name IS NOT NULL
        GROUP BY b.player_tag, b.brawler_name, LEFT(b.battleTime, 8)
        ORDER BY b.player_tag, b.brawler_name, d;
        """,
        (from_prefix,),
    )

    rows = cur.fetchall()

    # 2) Transform to rows with a proper DATE
    upsert_rows = []
    for r in rows:
        day_str = r["d"]           # e.g. "20251213"
        day = dt.datetime.strptime(day_str, "%Y%m%d").date()  # UTC date

        upsert_rows.append(
            {
                "player_tag": r["player_tag"],
                "brawler_name": r["brawler_name"],
                "activity_date": day,
                "games": r["games"] or 0,
                "wins": r["wins"] or 0,
                "losses": r["losses"] or 0,
                "draws": r["draws"] or 0,
            }
        )

    # 3) Upsert into player_brawler_daily_activity
    #    (this only touches games/wins/losses/draws; trophies stays as-is
    #     from your snapshot script, if you’re populating that separately).
    sql = """
    INSERT INTO player_brawler_daily_activity (
      player_tag,
      brawler_name,
      activity_date,
      trophies,
      games,
      wins,
      losses,
      draws,
      last_update
    ) VALUES (
      %(player_tag)s,
      %(brawler_name)s,
      %(activity_date)s,
      NULL,
      %(games)s,
      %(wins)s,
      %(losses)s,
      %(draws)s,
      NOW()
    )
    ON CONFLICT (player_tag, brawler_name, activity_date)
    DO UPDATE SET
      games      = EXCLUDED.games,
      wins       = EXCLUDED.wins,
      losses     = EXCLUDED.losses,
      draws      = EXCLUDED.draws,
      last_update = NOW();
    """

    if upsert_rows:
        execute_batch(cur, sql, upsert_rows, page_size=1000)
        conn.commit()
        print(f"Backfilled {len(upsert_rows)} player_brawler_daily_activity rows")
    else:
        print("No rows to backfill.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    # Example: rebuild last 90 days
    backfill_for_range(days_back=90)

#!/usr/bin/env python3
"""Retry transient post-match scout profile failures from the persistent queue."""

import json
import os

from database import connect
from services.profile_updater import ensure_scout_profiles


MAX_PER_RUN = int(os.getenv("SCOUT_RETRY_MAX_PER_RUN", "20"))


def collect() -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT player_tag, observed_from_tag, battle_time
            FROM scout_profile_retry_queue
            WHERE status IN ('pending', 'rate_limited', 'error')
              AND next_retry_at <= now()
            ORDER BY next_retry_at, first_queued_at
            LIMIT %s
            """,
            (MAX_PER_RUN,),
        )
        due = cur.fetchall()

    totals = {"due": len(due), "succeeded": 0, "not_found": 0, "rate_limited": 0, "error": 0}
    for tag, observed_from_tag, battle_time in due:
        result = ensure_scout_profiles(
            [tag], max_age_hours=6, battle_time=battle_time, observed_from_tag=observed_from_tag
        )
        status = result.get("statuses", {}).get(tag, "error")
        bucket = "succeeded" if status in {"cached", "fetched"} else status
        if bucket not in totals:
            bucket = "error"
        totals[bucket] += 1
    return totals


if __name__ == "__main__":
    print("SCOUT_RETRY|" + json.dumps(collect(), sort_keys=True))

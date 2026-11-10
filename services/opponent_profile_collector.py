#!/usr/bin/env python3
"""Bounded direct-opponent profile snapshots for future-battle features.

Only tags explicitly enrolled from newly observed complete tracked-player 3v3
lobbies are eligible. This worker never reads battle participants and therefore
cannot recursively discover more players.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from typing import Any, Dict, List

from services.profile_updater import (
    LAST_PROFILE_META,
    fetch_player_profile,
    get_conn,
    normalize_tag,
    sync_player_brawlers,
    upsert_profile,
    write_profile_snapshot,
)


MAX_PER_RUN = int(os.getenv("OPPONENT_PROFILE_MAX_PER_RUN", "100"))
DAILY_BUDGET = int(os.getenv("OPPONENT_PROFILE_DAILY_BUDGET", "1000"))
ACTIVE_TTL_DAYS = int(os.getenv("OPPONENT_PROFILE_ACTIVE_TTL_DAYS", "30"))
REQUEST_DELAY = float(os.getenv("OPPONENT_PROFILE_REQUEST_DELAY", "0.2"))


def remaining_daily_budget(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM collector_polls
            WHERE endpoint = 'opponent_profile'
              AND started_at >= now() - interval '24 hours'
            """
        )
        used = int(cur.fetchone()[0] or 0)
    conn.rollback()
    return max(0, DAILY_BUDGET - used)


def load_due(conn, limit: int) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT player_tag, discovery_count, last_discovered_at,
                   last_successful_profile_poll_at, attempts
            FROM opponent_profile_pool
            WHERE status IN ('active', 'error', 'not_found')
              AND next_profile_poll_at <= now()
              AND last_discovered_at >= now() - make_interval(days => %s)
            ORDER BY
              (last_successful_profile_poll_at IS NULL) DESC,
              discovery_count DESC,
              last_discovered_at DESC,
              next_profile_poll_at,
              player_tag
            LIMIT %s
            """,
            (ACTIVE_TTL_DAYS, limit),
        )
        columns = [d.name for d in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    conn.rollback()
    return rows


def record_poll(conn, tag: str, success: bool, meta: Dict[str, Any], error: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO collector_polls (
              player_tag, endpoint, completed_at, http_status, api_latency_ms,
              cache_max_age_seconds, retry_after_seconds, rate_limited, success,
              error_category, error_message, metadata
            ) VALUES (
              %s, 'opponent_profile', now(), %s, %s, %s, %s, %s, %s,
              %s, %s, '{"bounded":true,"recursive":false,"future_battles_only":true}'::jsonb
            ) RETURNING id
            """,
            (
                tag, meta.get("http_status"), meta.get("api_latency_ms"),
                meta.get("cache_max_age_seconds"), meta.get("retry_after_seconds"),
                meta.get("rate_limited", False), success,
                None if success else "fetch_failed", error,
            ),
        )
        return int(cur.fetchone()[0])


def mark_success(conn, tag: str, poll_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE opponent_profile_pool
            SET status = 'active', last_profile_poll_at = now(),
                last_successful_profile_poll_at = now(),
                next_profile_poll_at = now() + CASE
                  WHEN last_discovered_at >= now() - interval '7 days' THEN interval '24 hours'
                  ELSE interval '7 days' END,
                attempts = 0, source_poll_id = %s, last_error = NULL,
                metadata = metadata || %s::jsonb, updated_at = now()
            WHERE player_tag = %s
            """,
            (poll_id, json.dumps({"last_result": "success"}), tag),
        )


def mark_failure(conn, tag: str, poll_id: int, meta: Dict[str, Any], error: str) -> None:
    not_found = meta.get("http_status") == 404
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE opponent_profile_pool
            SET status = %s, last_profile_poll_at = now(), attempts = attempts + 1,
                next_profile_poll_at = now() + CASE
                  WHEN %s THEN interval '7 days'
                  ELSE LEAST(interval '24 hours', make_interval(hours => power(2, LEAST(attempts, 4))::int)) END,
                source_poll_id = %s, last_error = %s,
                metadata = metadata || %s::jsonb, updated_at = now()
            WHERE player_tag = %s
            """,
            (
                "not_found" if not_found else "error", not_found, poll_id, error,
                json.dumps({"last_result": "not_found" if not_found else "error"}), tag,
            ),
        )


def collect() -> Dict[str, Any]:
    conn = get_conn()
    stats: Dict[str, Any] = {
        "daily_budget": DAILY_BUDGET,
        "remaining_before": 0,
        "due": 0,
        "requested": 0,
        "snapshotted": 0,
        "not_found": 0,
        "failed": 0,
        "rate_limited": 0,
        "skipped_locked": False,
    }
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext('starrlabs-opponent-profile-collector'))")
            if not cur.fetchone()[0]:
                conn.rollback()
                stats["skipped_locked"] = True
                stats["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                return stats
        conn.commit()
        remaining = remaining_daily_budget(conn)
        stats["remaining_before"] = remaining
        due = load_due(conn, min(MAX_PER_RUN, remaining))
        stats["due"] = len(due)
        for row in due:
            tag = normalize_tag(row["player_tag"])
            data = fetch_player_profile(tag)
            meta = dict(LAST_PROFILE_META.get(tag, {}))
            stats["requested"] += 1
            if meta.get("rate_limited"):
                stats["rate_limited"] += 1
            if not data:
                error = "profile not found" if meta.get("http_status") == 404 else "profile unavailable"
                poll_id = record_poll(conn, tag, False, meta, error)
                mark_failure(conn, tag, poll_id, meta, error)
                conn.commit()
                stats["not_found" if meta.get("http_status") == 404 else "failed"] += 1
                time.sleep(REQUEST_DELAY)
                continue

            poll_id = record_poll(conn, tag, True, meta)
            write_profile_snapshot(conn, data, source_poll_id=poll_id)
            upsert_profile(conn, data)
            sync_player_brawlers(conn, data)
            mark_success(conn, tag, poll_id)
            conn.commit()
            stats["snapshotted"] += 1
            time.sleep(REQUEST_DELAY)
    finally:
        conn.close()
    stats["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    result = collect()
    print("OPPONENT_PROFILE_COLLECTION|" + json.dumps(result, sort_keys=True))
    raise SystemExit(1 if result["failed"] else 0)

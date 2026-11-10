# services/profile_updater.py
import os
import time
import json
import hashlib
import urllib.parse
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, Any, List, Tuple

import requests
import psycopg2
from database import connect as database_connect
from psycopg2.extras import execute_batch

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

BASE_URL = "https://api.brawlstars.com/v1"

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
PROFILE_REQUEST_DELAY = float(os.getenv("PROFILE_REQUEST_DELAY", "0.2"))
LAST_PROFILE_META: Dict[str, Dict[str, Any]] = {}


def _cache_max_age(headers: Dict[str, Any]) -> Optional[int]:
    value = str(headers.get("Cache-Control", ""))
    for part in value.split(","):
        part = part.strip().lower()
        if part.startswith("max-age="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                return None
    return None

def normalize_tag(tag: str) -> str:
    tag = (tag or "").strip().upper()
    if tag and not tag.startswith("#"):
        tag = "#" + tag
    return tag

def _require_token() -> str:
    token = os.getenv("API_TOKEN")
    if not token:
        raise RuntimeError("API_TOKEN is not set in environment")
    return token

def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_require_token()}", "Accept": "application/json"}

def get_conn():
    if not all([DB_HOST, DB_USER, DB_PASSWORD, DB_NAME]):
        raise RuntimeError("Some DB_* variables are missing in environment")
    return database_connect()


def fetch_player_profile(tag: str) -> Optional[Dict[str, Any]]:
    tag = normalize_tag(tag)
    if not tag:
        return None
    encoded = urllib.parse.quote(tag)
    url = f"{BASE_URL}/players/{encoded}"
    started = time.monotonic()
    rate_limited = False
    retry_after = None
    last_http_status = None
    for attempt in range(6):
        try:
            r = requests.get(url, headers=_headers(), timeout=15)
            last_http_status = r.status_code
            if r.status_code == 404:
                LAST_PROFILE_META[tag] = {
                    "http_status": 404,
                    "api_latency_ms": int((time.monotonic() - started) * 1000),
                    "rate_limited": rate_limited,
                }
                print(f"[profile] Player {tag} not found (404)")
                return None
            if r.status_code == 429:
                rate_limited = True
                raw_retry = r.headers.get("Retry-After")
                retry_after = float(raw_retry) if raw_retry else None
                time.sleep(min(retry_after or (0.5 * (2 ** attempt)), 20))
                continue
            if 500 <= r.status_code < 600:
                time.sleep(min(0.5 * (2 ** attempt), 10))
                continue
            r.raise_for_status()
            LAST_PROFILE_META[tag] = {
                "http_status": r.status_code,
                "api_latency_ms": int((time.monotonic() - started) * 1000),
                "cache_max_age_seconds": _cache_max_age(r.headers),
                "retry_after_seconds": retry_after,
                "rate_limited": rate_limited,
            }
            return r.json()
        except Exception as e:
            if attempt == 5:
                LAST_PROFILE_META[tag] = {
                    "http_status": None,
                    "api_latency_ms": int((time.monotonic() - started) * 1000),
                    "retry_after_seconds": retry_after,
                    "rate_limited": rate_limited,
                }
                print(f"[profile] Error fetching {tag}: {e}")
                return None
            time.sleep(min(0.25 * (2 ** attempt), 5))
    LAST_PROFILE_META[tag] = {
        "http_status": last_http_status,
        "api_latency_ms": int((time.monotonic() - started) * 1000),
        "retry_after_seconds": retry_after,
        "rate_limited": rate_limited,
    }
    return None


def compute_favorite_brawler(player_json: Dict[str, Any]) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    brawlers = player_json.get("brawlers", []) or []
    if not brawlers:
        return None, None, None

    sorted_b = sorted(brawlers, key=lambda b: b.get("trophies", 0), reverse=True)
    top = sorted_b[0]
    name = top.get("name")
    trophies = top.get("trophies", 0)
    return name, trophies, 1


def get_tracked_tags(conn) -> List[str]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.player_tag
        FROM tracked_players t
        LEFT JOIN tracked_player_state s ON s.player_tag = t.player_tag
        WHERE s.next_profile_poll_at IS NULL OR s.next_profile_poll_at <= now()
        ORDER BY t.player_tag
        """
    )
    rows = cur.fetchall()
    cur.close()
    return [normalize_tag(r[0]) for r in rows]


def _serialize_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value)


def _payload_from_player_json(player_json: Dict[str, Any]) -> Dict[str, Any]:
    fav_name, fav_trophies, _fav_rank = compute_favorite_brawler(player_json)
    return {
        "name": player_json.get("name"),
        "icon_id": (player_json.get("icon") or {}).get("id"),
        "trophies": player_json.get("trophies"),
        "highest_trophies": player_json.get("highestTrophies"),
        "exp_level": player_json.get("expLevel"),
        "3v3_victories": player_json.get("3vs3Victories"),
        "solo_victories": player_json.get("soloVictories"),
        "duo_victories": player_json.get("duoVictories"),
        "favorite_brawler_name": fav_name,
        "favorite_brawler_trophies": fav_trophies,
        "name_color": player_json.get("nameColor"),
        "exp_points": player_json.get("expPoints"),
        "power_play_points": player_json.get("powerPlayPoints"),
        "highest_power_play_points": player_json.get("highestPowerPlayPoints"),
        "is_qualified_from_championship_challenge": player_json.get("isQualifiedFromChampionshipChallenge"),
        "best_robo_rumble_time": player_json.get("bestRoboRumbleTime"),
        "best_time_as_big_brawler": player_json.get("bestTimeAsBigBrawler"),
        "ranked_elo": player_json.get("rankedElo"),
        "ranked_rank": player_json.get("rankedRank"),
        "ranked_rank_name": player_json.get("rankedRankName"),
        "ranked_season_id": player_json.get("rankedSeasonId"),
        "highest_season_ranked_elo": player_json.get("highestSeasonRankedElo"),
        "highest_season_ranked_rank": player_json.get("highestSeasonRankedRank"),
        "highest_season_ranked_rank_name": player_json.get("highestSeasonRankedRankName"),
        "highest_all_time_ranked_elo": player_json.get("highestAllTimeRankedElo"),
        "highest_all_time_ranked_rank": player_json.get("highestAllTimeRankedRank"),
        "highest_all_time_ranked_rank_name": player_json.get("highestAllTimeRankedRankName"),
        "total_prestige_level": player_json.get("totalPrestigeLevel"),
    }


def _json_hash(value: Dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def write_profile_snapshot(
    conn,
    player_json: Dict[str, Any],
    source_poll_id: Optional[int] = None,
) -> int:
    """Persist one immutable, point-in-time player + brawler snapshot."""
    tag = normalize_tag(player_json.get("tag"))
    if not tag:
        raise ValueError("profile snapshot requires a player tag")
    payload = _payload_from_player_json(player_json)
    club = player_json.get("club") or {}
    captured_at = dt.datetime.now(dt.timezone.utc)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO player_profile_snapshots (
          player_tag, captured_at, source, source_poll_id, payload_hash, payload,
          name, icon_id, trophies, highest_trophies, exp_level, exp_points,
          victories_3v3, solo_victories, duo_victories, club_tag, club_name,
          ranked_elo, ranked_rank, ranked_rank_name, ranked_season_id,
          highest_season_ranked_elo, highest_season_ranked_rank, highest_season_ranked_rank_name,
          highest_all_time_ranked_elo, highest_all_time_ranked_rank, highest_all_time_ranked_rank_name,
          total_prestige_level
        ) VALUES (
          %(tag)s, %(captured_at)s, 'supercell_player_api', %(poll_id)s, %(payload_hash)s, %(payload)s::jsonb,
          %(name)s, %(icon_id)s, %(trophies)s, %(highest_trophies)s, %(exp_level)s, %(exp_points)s,
          %(v3)s, %(solo)s, %(duo)s, %(club_tag)s, %(club_name)s,
          %(ranked_elo)s, %(ranked_rank)s, %(ranked_rank_name)s, %(ranked_season_id)s,
          %(highest_season_ranked_elo)s, %(highest_season_ranked_rank)s, %(highest_season_ranked_rank_name)s,
          %(highest_all_time_ranked_elo)s, %(highest_all_time_ranked_rank)s, %(highest_all_time_ranked_rank_name)s,
          %(total_prestige_level)s
        ) RETURNING id
        """,
        {
            **payload,
            "tag": tag,
            "captured_at": captured_at,
            "poll_id": source_poll_id,
            "payload_hash": _json_hash(player_json),
            "payload": json.dumps(player_json),
            "icon_id": (player_json.get("icon") or {}).get("id"),
            "v3": player_json.get("3vs3Victories"),
            "solo": player_json.get("soloVictories"),
            "duo": player_json.get("duoVictories"),
            "club_tag": club.get("tag"),
            "club_name": club.get("name"),
        },
    )
    snapshot_id = cur.fetchone()[0]
    rows = []
    for b in player_json.get("brawlers", []) or []:
        if b.get("id") is None:
            continue
        rows.append(
            {
                "snapshot_id": snapshot_id,
                "tag": tag,
                "captured_at": captured_at,
                "brawler_id": b.get("id"),
                "brawler_name": b.get("name"),
                "power": b.get("power"),
                "rank": b.get("rank"),
                "trophies": b.get("trophies"),
                "highest_trophies": b.get("highestTrophies"),
                "prestige_level": b.get("prestigeLevel"),
                "current_win_streak": b.get("currentWinStreak"),
                "max_win_streak": b.get("maxWinStreak"),
                "skin": _serialize_json(b.get("skin")),
                "star_powers": _serialize_json(b.get("starPowers") or []),
                "gadgets": _serialize_json(b.get("gadgets") or []),
                "gears": _serialize_json(b.get("gears") or []),
                "hypercharges": _serialize_json(b.get("hyperCharges") or []),
                "buffies": _serialize_json(b.get("buffies") or {}),
            }
        )
    if rows:
        execute_batch(
            cur,
            """
            INSERT INTO player_brawler_snapshots (
              profile_snapshot_id, player_tag, captured_at, brawler_id, brawler_name,
              power, rank, trophies, highest_trophies, prestige_level,
              current_win_streak, max_win_streak, skin_json, star_powers_json,
              gadgets_json, gears_json, hypercharges_json, buffies_json
            ) VALUES (
              %(snapshot_id)s, %(tag)s, %(captured_at)s, %(brawler_id)s, %(brawler_name)s,
              %(power)s, %(rank)s, %(trophies)s, %(highest_trophies)s, %(prestige_level)s,
              %(current_win_streak)s, %(max_win_streak)s, %(skin)s::jsonb, %(star_powers)s::jsonb,
              %(gadgets)s::jsonb, %(gears)s::jsonb, %(hypercharges)s::jsonb, %(buffies)s::jsonb
            )
            """,
            rows,
            page_size=100,
        )
    cur.close()
    return snapshot_id


def upsert_profile(conn, player_json: Dict[str, Any]) -> None:
    tag = player_json.get("tag")
    if not tag:
        return

    payload = _payload_from_player_json(player_json)
    club = player_json.get("club") or {}
    club_tag = club.get("tag")
    club_name = club.get("name")
    fav_name, fav_trophies, fav_rank = compute_favorite_brawler(player_json)

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO player_profiles (
          tag, name, icon_id, trophies, highest_trophies, exp_level,
          "3v3_victories", solo_victories, duo_victories,
          favorite_brawler_name, favorite_brawler_trophies, favorite_brawler_rank_on_account,
          club_tag, club_name,
          name_color, exp_points, power_play_points, highest_power_play_points,
          is_qualified_from_championship_challenge, best_robo_rumble_time, best_time_as_big_brawler,
          ranked_elo, ranked_rank, ranked_rank_name, ranked_season_id,
          highest_season_ranked_elo, highest_season_ranked_rank, highest_season_ranked_rank_name,
          highest_all_time_ranked_elo, highest_all_time_ranked_rank, highest_all_time_ranked_rank_name,
          total_prestige_level,
          first_seen, last_update
        )
        VALUES (
          %(tag)s, %(name)s, %(icon_id)s, %(trophies)s, %(highest_trophies)s, %(exp_level)s,
          %(v3)s, %(solo)s, %(duo)s,
          %(fav_name)s, %(fav_trophies)s, %(fav_rank)s,
          %(club_tag)s, %(club_name)s,
          %(name_color)s, %(exp_points)s, %(power_play_points)s, %(highest_power_play_points)s,
          %(is_qualified_from_championship_challenge)s, %(best_robo_rumble_time)s, %(best_time_as_big_brawler)s,
          %(ranked_elo)s, %(ranked_rank)s, %(ranked_rank_name)s, %(ranked_season_id)s,
          %(highest_season_ranked_elo)s, %(highest_season_ranked_rank)s, %(highest_season_ranked_rank_name)s,
          %(highest_all_time_ranked_elo)s, %(highest_all_time_ranked_rank)s, %(highest_all_time_ranked_rank_name)s,
          %(total_prestige_level)s,
          now(), now()
        )
        ON CONFLICT (tag) DO UPDATE
        SET
          name = EXCLUDED.name,
          icon_id = EXCLUDED.icon_id,
          trophies = EXCLUDED.trophies,
          highest_trophies = EXCLUDED.highest_trophies,
          exp_level = EXCLUDED.exp_level,
          "3v3_victories" = EXCLUDED."3v3_victories",
          solo_victories = EXCLUDED.solo_victories,
          duo_victories = EXCLUDED.duo_victories,
          favorite_brawler_name = EXCLUDED.favorite_brawler_name,
          favorite_brawler_trophies = EXCLUDED.favorite_brawler_trophies,
          favorite_brawler_rank_on_account = EXCLUDED.favorite_brawler_rank_on_account,
          club_tag = EXCLUDED.club_tag,
          club_name = EXCLUDED.club_name,
          name_color = EXCLUDED.name_color,
          exp_points = EXCLUDED.exp_points,
          power_play_points = EXCLUDED.power_play_points,
          highest_power_play_points = EXCLUDED.highest_power_play_points,
          is_qualified_from_championship_challenge = EXCLUDED.is_qualified_from_championship_challenge,
          best_robo_rumble_time = EXCLUDED.best_robo_rumble_time,
          best_time_as_big_brawler = EXCLUDED.best_time_as_big_brawler,
          ranked_elo = EXCLUDED.ranked_elo,
          ranked_rank = EXCLUDED.ranked_rank,
          ranked_rank_name = EXCLUDED.ranked_rank_name,
          ranked_season_id = EXCLUDED.ranked_season_id,
          highest_season_ranked_elo = EXCLUDED.highest_season_ranked_elo,
          highest_season_ranked_rank = EXCLUDED.highest_season_ranked_rank,
          highest_season_ranked_rank_name = EXCLUDED.highest_season_ranked_rank_name,
          highest_all_time_ranked_elo = EXCLUDED.highest_all_time_ranked_elo,
          highest_all_time_ranked_rank = EXCLUDED.highest_all_time_ranked_rank,
          highest_all_time_ranked_rank_name = EXCLUDED.highest_all_time_ranked_rank_name,
          total_prestige_level = EXCLUDED.total_prestige_level,
          last_update = now();
        """,
        {
            "tag": tag,
            "name": payload["name"],
            "icon_id": payload["icon_id"],
            "trophies": payload["trophies"],
            "highest_trophies": payload["highest_trophies"],
            "exp_level": payload["exp_level"],
            "v3": payload["3v3_victories"],
            "solo": payload["solo_victories"],
            "duo": payload["duo_victories"],
            "fav_name": fav_name,
            "fav_trophies": fav_trophies,
            "fav_rank": fav_rank,
            "club_tag": club_tag,
            "club_name": club_name,
            "name_color": payload["name_color"],
            "exp_points": payload["exp_points"],
            "power_play_points": payload["power_play_points"],
            "highest_power_play_points": payload["highest_power_play_points"],
            "is_qualified_from_championship_challenge": payload["is_qualified_from_championship_challenge"],
            "best_robo_rumble_time": payload["best_robo_rumble_time"],
            "best_time_as_big_brawler": payload["best_time_as_big_brawler"],
            "ranked_elo": payload["ranked_elo"],
            "ranked_rank": payload["ranked_rank"],
            "ranked_rank_name": payload["ranked_rank_name"],
            "ranked_season_id": payload["ranked_season_id"],
            "highest_season_ranked_elo": payload["highest_season_ranked_elo"],
            "highest_season_ranked_rank": payload["highest_season_ranked_rank"],
            "highest_season_ranked_rank_name": payload["highest_season_ranked_rank_name"],
            "highest_all_time_ranked_elo": payload["highest_all_time_ranked_elo"],
            "highest_all_time_ranked_rank": payload["highest_all_time_ranked_rank"],
            "highest_all_time_ranked_rank_name": payload["highest_all_time_ranked_rank_name"],
            "total_prestige_level": payload["total_prestige_level"],
        },
    )
    cur.close()


def sync_player_brawlers(conn, player_json: Dict[str, Any]) -> int:
    tag = normalize_tag(player_json.get("tag"))
    if not tag:
        return 0

    brawlers = player_json.get("brawlers", []) or []
    rows = []
    seen_ids = []
    for b in brawlers:
        brawler_id = b.get("id")
        if brawler_id is None:
            continue
        seen_ids.append(int(brawler_id))
        rows.append(
            {
                "player_tag": tag,
                "brawler_id": int(brawler_id),
                "brawler_name": b.get("name"),
                "power": b.get("power"),
                "rank": b.get("rank"),
                "trophies": b.get("trophies"),
                "highest_trophies": b.get("highestTrophies"),
                "star_powers_json": _serialize_json(b.get("starPowers") or []),
                "gadgets_json": _serialize_json(b.get("gadgets") or []),
                "gears_json": _serialize_json(b.get("gears") or []),
                "prestige_level": b.get("prestigeLevel"),
                "current_win_streak": b.get("currentWinStreak"),
                "max_win_streak": b.get("maxWinStreak"),
                "skin_json": _serialize_json(b.get("skin")),
                "hypercharges_json": _serialize_json(b.get("hyperCharges") or []),
                "buffies_json": _serialize_json(b.get("buffies") or {}),
            }
        )

    cur = conn.cursor()
    if rows:
        execute_batch(
            cur,
            """
            INSERT INTO player_brawlers (
              player_tag, brawler_id, brawler_name, power, rank, trophies, highest_trophies,
              star_powers_json, gadgets_json, gears_json, last_update
              , prestige_level, current_win_streak, max_win_streak,
              skin_json, hypercharges_json, buffies_json
            )
            VALUES (
              %(player_tag)s, %(brawler_id)s, %(brawler_name)s, %(power)s, %(rank)s, %(trophies)s, %(highest_trophies)s,
              %(star_powers_json)s::jsonb, %(gadgets_json)s::jsonb, %(gears_json)s::jsonb, now()
              , %(prestige_level)s, %(current_win_streak)s, %(max_win_streak)s,
              %(skin_json)s::jsonb, %(hypercharges_json)s::jsonb, %(buffies_json)s::jsonb
            )
            ON CONFLICT (player_tag, brawler_id) DO UPDATE
            SET
              brawler_name = EXCLUDED.brawler_name,
              power = EXCLUDED.power,
              rank = EXCLUDED.rank,
              trophies = EXCLUDED.trophies,
              highest_trophies = EXCLUDED.highest_trophies,
              star_powers_json = EXCLUDED.star_powers_json,
              gadgets_json = EXCLUDED.gadgets_json,
              gears_json = EXCLUDED.gears_json,
              prestige_level = EXCLUDED.prestige_level,
              current_win_streak = EXCLUDED.current_win_streak,
              max_win_streak = EXCLUDED.max_win_streak,
              skin_json = EXCLUDED.skin_json,
              hypercharges_json = EXCLUDED.hypercharges_json,
              buffies_json = EXCLUDED.buffies_json,
              last_update = now();
            """,
            rows,
            page_size=100,
        )

    if seen_ids:
        cur.execute(
            "DELETE FROM player_brawlers WHERE player_tag = %s AND NOT (brawler_id = ANY(%s));",
            (tag, seen_ids),
        )
    else:
        cur.execute("DELETE FROM player_brawlers WHERE player_tag = %s;", (tag,))

    cur.close()
    return len(rows)


def _get_existing_profile_row(conn, tag: str) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
          name,
          icon_id,
          trophies,
          highest_trophies,
          exp_level,
          "3v3_victories",
          solo_victories,
          duo_victories,
          favorite_brawler_name,
          favorite_brawler_trophies,
          name_color,
          exp_points,
          power_play_points,
          highest_power_play_points,
          is_qualified_from_championship_challenge,
          best_robo_rumble_time,
          best_time_as_big_brawler,
          ranked_elo, ranked_rank, ranked_rank_name, ranked_season_id,
          highest_season_ranked_elo, highest_season_ranked_rank, highest_season_ranked_rank_name,
          highest_all_time_ranked_elo, highest_all_time_ranked_rank, highest_all_time_ranked_rank_name,
          total_prestige_level
        FROM player_profiles
        WHERE tag = %s
        """,
        (tag,),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return None

    keys = [
        "name","icon_id","trophies","highest_trophies","exp_level",
        "3v3_victories","solo_victories","duo_victories",
        "favorite_brawler_name","favorite_brawler_trophies",
        "name_color","exp_points","power_play_points","highest_power_play_points",
        "is_qualified_from_championship_challenge","best_robo_rumble_time","best_time_as_big_brawler",
        "ranked_elo","ranked_rank","ranked_rank_name","ranked_season_id",
        "highest_season_ranked_elo","highest_season_ranked_rank","highest_season_ranked_rank_name",
        "highest_all_time_ranked_elo","highest_all_time_ranked_rank","highest_all_time_ranked_rank_name",
        "total_prestige_level",
    ]
    return dict(zip(keys, row))


def _same_profile(existing: Dict[str, Any], new_payload: Dict[str, Any]) -> bool:
    if not existing:
        return False
    for k, v in new_payload.items():
        if existing.get(k) != v:
            return False
    return True


# =========================================================
# ✅ MAIN callable function
# =========================================================

def update_profiles(tags: List[str]) -> Dict[str, Any]:
    """
    Fetch + upsert player_profiles and player_brawlers for given tags.
    Returns per-tag status: added | updated | no_updates
    """
    if not tags:
        return {"requested": 0, "updated": 0, "statuses": {}}

    tags = [normalize_tag(t) for t in tags if normalize_tag(t)]
    tags = sorted(set(tags))

    conn = get_conn()
    statuses: Dict[str, str] = {}
    updated = 0
    brawler_rows_written = 0

    try:
        for tag in tags:
            before = _get_existing_profile_row(conn, tag)

            started = time.monotonic()
            data = fetch_player_profile(tag)
            if not data:
                statuses[tag] = "error"
                meta = LAST_PROFILE_META.get(tag, {})
                err_cur = conn.cursor()
                err_cur.execute(
                    """
                    INSERT INTO collector_polls (
                      player_tag, endpoint, completed_at, http_status, api_latency_ms,
                      retry_after_seconds, rate_limited, success, error_category, error_message
                    ) VALUES (%s, 'player_profile', now(), %s, %s, %s, %s,
                              false, 'fetch_failed', 'profile unavailable')
                    """,
                    (tag, meta.get("http_status"), meta.get("api_latency_ms"),
                     meta.get("retry_after_seconds"), meta.get("rate_limited", False)),
                )
                err_cur.execute(
                    """
                    INSERT INTO tracked_player_state (player_tag, last_profile_poll_at, next_profile_poll_at)
                    VALUES (%s, now(), now() + interval '24 hours')
                    ON CONFLICT (player_tag) DO UPDATE SET
                      last_profile_poll_at = now(),
                      next_profile_poll_at = now() + interval '24 hours',
                      updated_at = now()
                    """,
                    (tag,),
                )
                conn.commit()
                err_cur.close()
                time.sleep(PROFILE_REQUEST_DELAY)
                continue

            elapsed_ms = int((time.monotonic() - started) * 1000)
            meta = LAST_PROFILE_META.get(tag, {})
            poll_cur = conn.cursor()
            poll_cur.execute(
                """
                INSERT INTO collector_polls (
                  player_tag, endpoint, completed_at, http_status, api_latency_ms,
                  cache_max_age_seconds, retry_after_seconds, rate_limited, success
                ) VALUES (%s, 'player_profile', now(), %s, %s, %s, %s, %s, true)
                RETURNING id
                """,
                (tag, meta.get("http_status", 200), meta.get("api_latency_ms", elapsed_ms),
                 meta.get("cache_max_age_seconds"), meta.get("retry_after_seconds"),
                 meta.get("rate_limited", False)),
            )
            poll_id = poll_cur.fetchone()[0]
            poll_cur.close()
            write_profile_snapshot(conn, data, source_poll_id=poll_id)
            poll_cur = conn.cursor()
            poll_cur.execute(
                """
                INSERT INTO tracked_player_state (
                  player_tag, last_profile_poll_at, last_successful_profile_poll_at,
                  next_profile_poll_at, updated_at
                ) VALUES (
                  %s, now(), now(), now() + interval '1 hour', now()
                )
                ON CONFLICT (player_tag) DO UPDATE SET
                  last_profile_poll_at = now(),
                  last_successful_profile_poll_at = now(),
                  next_profile_poll_at = now() + CASE
                    WHEN tracked_player_state.last_successful_battle_poll_at >= now() - interval '1 hour'
                    THEN interval '1 hour' ELSE interval '6 hours' END,
                  updated_at = now()
                """,
                (tag,),
            )
            poll_cur.close()

            payload = _payload_from_player_json(data)

            if before is None:
                upsert_profile(conn, data)
                brawler_rows_written += sync_player_brawlers(conn, data)
                conn.commit()
                statuses[tag] = "added"
                updated += 1
            else:
                if _same_profile(before, payload):
                    brawler_rows_written += sync_player_brawlers(conn, data)
                    conn.commit()
                    statuses[tag] = "no_updates"
                else:
                    upsert_profile(conn, data)
                    brawler_rows_written += sync_player_brawlers(conn, data)
                    conn.commit()
                    statuses[tag] = "updated"
                    updated += 1

            time.sleep(PROFILE_REQUEST_DELAY)

    finally:
        conn.close()

    return {"requested": len(tags), "updated": updated, "brawler_rows_written": brawler_rows_written, "statuses": statuses}


def _queue_scout_retry(
    conn,
    tag: str,
    status: str,
    meta: Dict[str, Any],
    battle_time: Optional[str] = None,
    observed_from_tag: Optional[str] = None,
) -> None:
    retry_after = meta.get("retry_after_seconds")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scout_profile_retry_queue (
              player_tag, observed_from_tag, battle_time, status, attempts,
              next_retry_at, last_http_status, last_retry_after_seconds,
              last_error, last_attempt_at, metadata
            ) VALUES (
              %s, %s, %s, %s, 1,
              now() + make_interval(secs => GREATEST(COALESCE(%s, 60), 60)),
              %s, %s, %s, now(), %s::jsonb
            )
            ON CONFLICT (player_tag) DO UPDATE SET
              observed_from_tag = COALESCE(EXCLUDED.observed_from_tag, scout_profile_retry_queue.observed_from_tag),
              battle_time = COALESCE(EXCLUDED.battle_time, scout_profile_retry_queue.battle_time),
              status = EXCLUDED.status,
              attempts = scout_profile_retry_queue.attempts + 1,
              next_retry_at = now() + make_interval(secs => GREATEST(
                COALESCE(EXCLUDED.last_retry_after_seconds, 0),
                CASE LEAST(scout_profile_retry_queue.attempts + 1, 6)
                  WHEN 1 THEN 60 WHEN 2 THEN 300 WHEN 3 THEN 900
                  WHEN 4 THEN 3600 WHEN 5 THEN 21600 ELSE 86400 END
                + mod(abs(hashtext(EXCLUDED.player_tag)), 31)
              )),
              last_http_status = EXCLUDED.last_http_status,
              last_retry_after_seconds = EXCLUDED.last_retry_after_seconds,
              last_error = EXCLUDED.last_error,
              last_attempt_at = now(), resolved_at = NULL, updated_at = now(),
              metadata = scout_profile_retry_queue.metadata || EXCLUDED.metadata
            """,
            (
                tag, observed_from_tag, battle_time, status, retry_after,
                meta.get("http_status"), retry_after,
                "rate limited" if status == "rate_limited" else "profile fetch failed",
                json.dumps({"last_result": status}),
            ),
        )
    conn.commit()


def _resolve_scout_retry(conn, tag: str, status: str, meta: Dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scout_profile_retry_queue
            SET status = %s, next_retry_at = NULL, last_http_status = %s,
                last_retry_after_seconds = %s, last_error = NULL,
                last_attempt_at = now(), resolved_at = now(), updated_at = now(),
                metadata = metadata || %s::jsonb
            WHERE player_tag = %s
            """,
            (status, meta.get("http_status"), meta.get("retry_after_seconds"),
             json.dumps({"last_result": status}), tag),
        )
    conn.commit()


def ensure_scout_profiles(
    tags: List[str],
    max_age_hours: int = 6,
    battle_time: Optional[str] = None,
    observed_from_tag: Optional[str] = None,
) -> Dict[str, Any]:
    """Cache current public profiles for post-match scouting without tracking them.

    Scout snapshots are deliberately separate from the tracked-player scheduler. They
    may enrich a post-fight report, but point-in-time prediction joins still require a
    snapshot captured at or before the battle.
    """
    normalized = sorted({normalize_tag(tag) for tag in tags if normalize_tag(tag)})
    if not normalized:
        return {"requested": 0, "fetched": 0, "cached": 0, "statuses": {}}

    conn = get_conn()
    statuses: Dict[str, str] = {}
    fetched = 0
    cached = 0
    not_found = 0
    failed = 0
    rate_limited = 0
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT player_tag, max(captured_at)
            FROM player_profile_snapshots
            WHERE player_tag = ANY(%s)
            GROUP BY player_tag
            """,
            (normalized,),
        )
        latest = {row[0]: row[1] for row in cur.fetchall()}
        cur.close()
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)

        pending = []
        for tag in normalized:
            captured_at = latest.get(tag)
            if captured_at and captured_at >= cutoff:
                statuses[tag] = "cached"
                cached += 1
                _resolve_scout_retry(conn, tag, "succeeded", {"http_status": 200})
                continue
            pending.append(tag)

        # A Solo Showdown report may have nine opponents. Fetch public profiles in a
        # small bounded pool so the first scout load takes a few seconds, not tens.
        fetched_payloads: Dict[str, Optional[Dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(pending) or 1)) as pool:
            future_tags = {pool.submit(fetch_player_profile, tag): tag for tag in pending}
            for future in as_completed(future_tags):
                tag = future_tags[future]
                try:
                    fetched_payloads[tag] = future.result()
                except Exception:
                    fetched_payloads[tag] = None

        for tag in pending:
            data = fetched_payloads.get(tag)
            if not data:
                meta = LAST_PROFILE_META.get(tag, {})
                if meta.get("http_status") == 404:
                    statuses[tag] = "not_found"
                    not_found += 1
                    _resolve_scout_retry(conn, tag, "not_found", meta)
                elif meta.get("rate_limited"):
                    statuses[tag] = "rate_limited"
                    rate_limited += 1
                    _queue_scout_retry(conn, tag, "rate_limited", meta, battle_time, observed_from_tag)
                else:
                    statuses[tag] = "error"
                    failed += 1
                    _queue_scout_retry(conn, tag, "error", meta, battle_time, observed_from_tag)
                continue

            meta = LAST_PROFILE_META.get(tag, {})
            poll_cur = conn.cursor()
            poll_cur.execute(
                """
                INSERT INTO collector_polls (
                  player_tag, endpoint, completed_at, http_status, api_latency_ms,
                  cache_max_age_seconds, retry_after_seconds, rate_limited, success
                ) VALUES (%s, 'post_match_scout', now(), %s, %s, %s, %s, %s, true)
                RETURNING id
                """,
                (tag, meta.get("http_status", 200), meta.get("api_latency_ms"),
                 meta.get("cache_max_age_seconds"), meta.get("retry_after_seconds"),
                 meta.get("rate_limited", False)),
            )
            poll_id = poll_cur.fetchone()[0]
            poll_cur.close()
            write_profile_snapshot(conn, data, source_poll_id=poll_id)
            upsert_profile(conn, data)
            sync_player_brawlers(conn, data)
            conn.commit()
            statuses[tag] = "fetched"
            fetched += 1
            _resolve_scout_retry(conn, tag, "succeeded", meta)
    finally:
        conn.close()

    return {
        "requested": len(normalized),
        "fetched": fetched,
        "cached": cached,
        "not_found": not_found,
        "failed": failed,
        "rate_limited": rate_limited,
        "statuses": statuses,
    }



def main():
    conn = get_conn()
    tags = get_tracked_tags(conn)
    conn.close()

    if not tags:
        raw_tag = os.getenv("RAW_TAG", "#DEMO000")
        tags = [raw_tag]
        print(f"[profile] No profiles yet, seeding with {raw_tag}")
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO player_profiles (tag, name)
            VALUES (%s, %s)
            ON CONFLICT (tag) DO NOTHING;
            """,
            (raw_tag, raw_tag),
        )
        conn.commit()
        cur.close()
        conn.close()

    out = update_profiles(tags)
    print(f"[profile] done: {out} @ {dt.datetime.now()}")


if __name__ == "__main__":
    main()

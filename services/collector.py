# services/collector.py
import os
import time
import datetime as dt
import urllib.parse
import random
import threading
import json
import hashlib
from typing import Optional, Dict, Any, List, Set, Tuple

import requests
import psycopg2
from database import connect as database_connect
from psycopg2.extras import execute_batch, execute_values

from pathlib import Path
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

BASE_URL = "https://api.brawlstars.com/v1"

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

# Optional (kept for extra smoothing). With QPS limiter, these can be near-zero.
BATTLELOG_REQUEST_DELAY = float(os.getenv("BATTLELOG_REQUEST_DELAY", "0.05"))

# Worker + QPS knobs
API_QPS = float(os.getenv("API_QPS", "8"))                 # total requests/sec across all threads
BATTLELOG_WORKERS = int(os.getenv("BATTLELOG_WORKERS", "6"))
RECIPROCAL_COLLECTION_ENABLED = os.getenv("RECIPROCAL_COLLECTION_ENABLED", "1") == "1"
RECIPROCAL_MAX_REQUESTS_PER_RUN = int(os.getenv("RECIPROCAL_MAX_REQUESTS_PER_RUN", "100"))
RECIPROCAL_MAX_ATTEMPTS = int(os.getenv("RECIPROCAL_MAX_ATTEMPTS", "3"))
RECIPROCAL_TTL_MINUTES = int(os.getenv("RECIPROCAL_TTL_MINUTES", "45"))
OPPONENT_PROFILE_COLLECTION_ENABLED = os.getenv("OPPONENT_PROFILE_COLLECTION_ENABLED", "1") == "1"

SESSION = requests.Session()
API_META: Dict[str, Dict[str, Any]] = {}
API_META_LOCK = threading.Lock()


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


# ---------- Rate limiting ----------
class RateLimiter:
    """
    Simple global rate limiter. Guarantees average <= rate_per_sec across threads.
    """
    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / max(rate_per_sec, 0.1)
        self.lock = threading.Lock()
        self.next_time = time.monotonic()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_time:
                time.sleep(self.next_time - now)
            self.next_time = max(self.next_time + self.interval, time.monotonic())


limiter = RateLimiter(rate_per_sec=API_QPS)


# ---------- DB helpers ----------
def normalize_tag(tag: str) -> str:
    tag = (tag or "").strip().upper()
    if tag and not tag.startswith("#"):
        tag = "#" + tag
    return tag


def canonical_payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def item_roster_tags(item: Dict[str, Any]) -> List[str]:
    battle = item.get("battle", {}) or {}
    players: List[Dict[str, Any]] = []
    if "players" in battle:
        players = list(battle.get("players") or [])
    elif "teams" in battle:
        players = [p for team in (battle.get("teams") or []) for p in (team or [])]
    return sorted({normalize_tag(p.get("tag")) for p in players if p.get("tag")})


def roster_hash(tags: List[str]) -> str:
    return hashlib.sha256("|".join(sorted(normalize_tag(t) for t in tags if t)).encode("utf-8")).hexdigest()


def item_mode(item: Dict[str, Any]) -> Optional[str]:
    event = item.get("event", {}) or {}
    battle = item.get("battle", {}) or {}
    return event.get("mode") or battle.get("mode")


def match_reciprocal_item(
    battlelog: Dict[str, Any], target_battletime: str, target_event_id: int,
    target_mode: str, target_roster_hash: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Fail closed unless exactly one reciprocal item matches the full lobby identity."""
    candidates = []
    for item in battlelog.get("items", []) or []:
        event = item.get("event", {}) or {}
        if item.get("battleTime") != target_battletime:
            continue
        if event.get("id") != target_event_id:
            continue
        if item_mode(item) != target_mode:
            continue
        if roster_hash(item_roster_tags(item)) != target_roster_hash:
            continue
        candidates.append(item)
    if not candidates:
        return "missing", None
    if len(candidates) != 1:
        return "ambiguous", None
    return "matched", candidates[0]


def get_conn():
    if not all([DB_HOST, DB_USER, DB_PASSWORD, DB_NAME]):
        raise RuntimeError("Some DB_* variables are missing in environment")
    return database_connect()


def get_tracked_tags() -> List[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.player_tag
        FROM tracked_players t
        LEFT JOIN tracked_player_state s ON s.player_tag = t.player_tag
        WHERE s.next_battle_poll_at IS NULL OR s.next_battle_poll_at <= now()
        ORDER BY t.player_tag
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [normalize_tag(r[0]) for r in rows]


# ---------- API helpers ----------
def _require_token():
    token = os.getenv("API_TOKEN")
    if not token:
        raise RuntimeError("API_TOKEN is not set in environment")
    return token


def _headers():
    token = _require_token()
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def get_json(url: str, timeout=15, max_retries=6) -> dict:
    """
    All API calls go through here:
    - Uses shared requests.Session()
    - Global QPS limiter
    - Retries 429 + 5xx with exponential backoff
    """
    last_exc = None
    rate_limited = False
    retry_after = None
    request_started = time.monotonic()
    for attempt in range(max_retries):
        try:
            limiter.wait()
            r = SESSION.get(url, headers=_headers(), timeout=timeout)

            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                rate_limited = True
                retry_after = float(ra) if ra else None
                sleep_s = float(ra) if ra else (0.5 * (2 ** attempt) + random.random() * 0.25)
                time.sleep(min(sleep_s, 20))
                continue

            if 500 <= r.status_code < 600:
                time.sleep(min(0.5 * (2 ** attempt), 10))
                continue

            if 400 <= r.status_code < 500:
                # Permanent client-side responses (notably an unavailable
                # player/battlelog at 404) will not improve with six retries.
                # Preserve the real status so callers can classify it without
                # turning expected data unavailability into platform failure.
                with API_META_LOCK:
                    API_META[url] = {
                        "http_status": r.status_code,
                        "api_latency_ms": int((time.monotonic() - request_started) * 1000),
                        "cache_max_age_seconds": _cache_max_age(r.headers),
                        "retry_after_seconds": retry_after,
                        "rate_limited": rate_limited,
                    }
                r.raise_for_status()

            r.raise_for_status()
            with API_META_LOCK:
                API_META[url] = {
                    "http_status": r.status_code,
                    "api_latency_ms": int((time.monotonic() - request_started) * 1000),
                    "cache_max_age_seconds": _cache_max_age(r.headers),
                    "retry_after_seconds": retry_after,
                    "rate_limited": rate_limited,
                }
            return r.json()

        except requests.HTTPError as e:
            last_exc = e
            status = e.response.status_code if e.response is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                raise
            time.sleep(min(0.25 * (2 ** attempt), 5))
        except Exception as e:
            last_exc = e
            # Small backoff on network errors
            time.sleep(min(0.25 * (2 ** attempt), 5))

    # If we got here, retries exhausted
    if last_exc:
        with API_META_LOCK:
            API_META[url] = {
                "http_status": None,
                "api_latency_ms": int((time.monotonic() - request_started) * 1000),
                "cache_max_age_seconds": None,
                "retry_after_seconds": retry_after,
                "rate_limited": rate_limited,
            }
        raise last_exc
    raise RuntimeError("get_json failed without exception (unexpected)")


def get_battlelog(tag: str) -> dict:
    tag = normalize_tag(tag)
    encoded = urllib.parse.quote(tag)
    url = f"{BASE_URL}/players/{encoded}/battlelog"
    return get_json(url, timeout=15, max_retries=6)


def battlelog_http_status(tag: str) -> Optional[int]:
    """Return the last captured API status for one battle-log fetch."""
    url = f"{BASE_URL}/players/{urllib.parse.quote(normalize_tag(tag))}/battlelog"
    with API_META_LOCK:
        return API_META.get(url, {}).get("http_status")


def reciprocal_unavailable_status(http_status: Optional[int]) -> Tuple[str, str]:
    """Classify expected terminal absence separately from pipeline failure."""
    if http_status in (404, 410):
        return "expired", f"opponent battlelog unavailable (HTTP {http_status})"
    return "failed", (
        f"battlelog unavailable (HTTP {http_status})"
        if http_status is not None else "battlelog unavailable"
    )


# ---------- Parallel fetchers ----------
def fetch_battlelogs_parallel(tags: List[str], max_workers: int) -> Dict[str, dict]:
    results: Dict[str, dict] = {}
    if not tags:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(get_battlelog, t): t for t in tags}
        for fut in as_completed(futs):
            tag = futs[fut]
            try:
                results[tag] = fut.result()
            except Exception as e:
                print(f"[WARN] battlelog failed {tag}: {e}")
            finally:
                # Optional extra smoothing
                if BATTLELOG_REQUEST_DELAY > 0:
                    time.sleep(BATTLELOG_REQUEST_DELAY)

    return results


# ---------- Battlelog flatten ----------
def invert_result(res: Optional[str]) -> Optional[str]:
    if not res:
        return res
    r = res.lower()
    if r == "victory":
        return "defeat"
    if r == "defeat":
        return "victory"
    return res


def showdown_result_from_rank(mode: Optional[str], rank: Optional[int]) -> Optional[str]:
    if mode not in ("soloShowdown", "duoShowdown"):
        return None
    if rank is None:
        return None
    if mode == "soloShowdown":
        return "victory" if rank <= 4 else "defeat"
    if mode == "duoShowdown":
        return "victory" if rank <= 2 else "defeat"
    return None


def resolve_tracked_result(battle_result: Optional[str], mode: Optional[str], tracked_rank: Optional[int]) -> Optional[str]:
    if battle_result:
        return battle_result
    return showdown_result_from_rank(mode, tracked_rank)


def flatten_battlelog(battlelog: dict, main_tag: str) -> Tuple[List[Dict[str, Any]], Set[str]]:
    main_tag = normalize_tag(main_tag)
    items = battlelog.get("items", [])
    rows: List[Dict[str, Any]] = []
    player_tags: Set[str] = set()

    for item in items:
        battle = item.get("battle", {}) or {}
        event = item.get("event", {}) or {}

        battle_time = item.get("battleTime")
        mode = event.get("mode")
        event_mode_id = event.get("modeId")
        battle_mode = battle.get("mode")
        map_name = event.get("map")
        map_id = event.get("id")  # stable
        lobby_roster_hash = roster_hash(item_roster_tags(item))

        battle_result = battle.get("result")
        btype = battle.get("type")
        duration = battle.get("duration")
        battle_rank = battle.get("rank")
        battle_trophy_change = battle.get("trophyChange")

        star_player = (battle.get("starPlayer") or {})
        star_tag = star_player.get("tag")
        raw_battle_hash = hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        if "teams" in battle:
            teams = battle["teams"]

            main_team_idx = None
            for idx, team in enumerate(teams):
                if any(p.get("tag") == main_tag for p in team):
                    main_team_idx = idx
                    break

            for idx, team in enumerate(teams):
                if main_team_idx is None:
                    team_result = battle_result
                elif idx == main_team_idx:
                    team_result = battle_result
                else:
                    team_result = invert_result(battle_result)

                for p in team:
                    p_tag = p.get("tag")
                    p_name = p.get("name")
                    if p_tag:
                        player_tags.add(p_tag)

                    b = p.get("brawler") or {}
                    b_id = b.get("id")
                    b_name = b.get("name")
                    b_power = b.get("power")
                    b_trophies = b.get("trophies")

                    p_rank = p.get("rank")
                    p_trophy_change = p.get("trophyChange")

                    if p_tag == main_tag:
                        rank = battle_rank if battle_rank is not None else p_rank
                        trophy_change = battle_trophy_change if battle_trophy_change is not None else p_trophy_change
                    else:
                        rank = p_rank
                        trophy_change = p_trophy_change

                    pre_battle_trophies = (
                        b_trophies - trophy_change
                        if p_tag == main_tag and b_trophies is not None and trophy_change is not None
                        else None
                    )

                    row_result = team_result
                    if p_tag == main_tag:
                        row_result = resolve_tracked_result(team_result, mode, rank)

                    is_star = (p_tag == star_tag)

                    rows.append({
                        "battleTime": battle_time,
                        "mode": mode,
                        "event_mode_id": event_mode_id,
                        "battle_mode": battle_mode,
                        "map": map_name,
                        "map_id": map_id,
                        "result": row_result,
                        "type": btype,
                        "duration": duration,
                        "player_tag": p_tag,
                        "player_name": p_name,
                        "brawler_id": b_id,
                        "brawler_name": b_name,
                        "brawler_power": b_power,
                        "brawler_trophies": b_trophies,
                        "is_starPlayer": is_star,
                        "rank": rank,
                        "trophyChange": trophy_change,
                        "pre_battle_brawler_trophies": pre_battle_trophies,
                        "observed_from_tag": main_tag,
                        "team_index": idx,
                        "raw_battle_hash": raw_battle_hash,
                        "roster_hash": lobby_roster_hash,
                    })

        elif "players" in battle:
            for p in battle["players"]:
                p_tag = p.get("tag")
                p_name = p.get("name")
                if p_tag:
                    player_tags.add(p_tag)

                b = p.get("brawler") or {}
                b_id = b.get("id")
                b_name = b.get("name")
                b_power = b.get("power")
                b_trophies = b.get("trophies")

                p_rank = p.get("rank")
                p_trophy_change = p.get("trophyChange")

                if p_tag == main_tag:
                    rank = battle_rank if battle_rank is not None else p_rank
                    trophy_change = battle_trophy_change if battle_trophy_change is not None else p_trophy_change
                else:
                    rank = p_rank
                    trophy_change = p_trophy_change

                pre_battle_trophies = (
                    b_trophies - trophy_change
                    if p_tag == main_tag and b_trophies is not None and trophy_change is not None
                    else None
                )

                row_result = battle_result
                if p_tag == main_tag:
                    row_result = resolve_tracked_result(battle_result, mode, rank)

                is_star = (p_tag == star_tag)

                rows.append({
                    "battleTime": battle_time,
                    "mode": mode,
                    "event_mode_id": event_mode_id,
                    "battle_mode": battle_mode,
                    "map": map_name,
                    "map_id": map_id,
                    "result": row_result,
                    "type": btype,
                    "duration": duration,
                    "player_tag": p_tag,
                    "player_name": p_name,
                    "brawler_id": b_id,
                    "brawler_name": b_name,
                    "brawler_power": b_power,
                    "brawler_trophies": b_trophies,
                    "is_starPlayer": is_star,
                    "rank": rank,
                    "trophyChange": trophy_change,
                    "pre_battle_brawler_trophies": pre_battle_trophies,
                    "observed_from_tag": main_tag,
                    "team_index": None,
                    "raw_battle_hash": raw_battle_hash,
                    "roster_hash": lobby_roster_hash,
                })

    return rows, player_tags


# ---------- Save ----------
def save_to_cloud(rows: List[Dict[str, Any]], player_metrics: Dict[str, Dict[str, Any]]) -> int:
    conn = get_conn()
    cur = conn.cursor()

    sql = """
    INSERT INTO battles (
      battleTime, battletime_ts, mode, event_mode_id, battle_mode, map, map_id, result, type, duration,
      player_tag, player_name, player_total_trophies,
      brawler_id, brawler_name, brawler_power, brawler_trophies,
      brawler_total_trophies, brawler_rank_on_account,
      is_starPlayer, rank, trophyChange, pre_battle_brawler_trophies, is_highest_player_brawler,
      player_level, first_collected_at, last_observed_at, source_poll_id,
      observed_from_tag, team_index, raw_battle_hash, possible_gap,
      legacy_profile_enrichment
    ) VALUES (
      %(battleTime)s, (%(battleTime)s)::timestamptz, %(mode)s, %(event_mode_id)s, %(battle_mode)s, %(map)s, %(map_id)s, %(result)s, %(type)s, %(duration)s,
      %(player_tag)s, %(player_name)s, %(player_total_trophies)s,
      %(brawler_id)s, %(brawler_name)s, %(brawler_power)s, %(brawler_trophies)s,
      %(brawler_total_trophies)s, %(brawler_rank_on_account)s,
      %(is_starPlayer)s, %(rank)s, %(trophyChange)s, %(pre_battle_brawler_trophies)s, %(is_highest_player_brawler)s,
      %(player_level)s, now(), now(), %(source_poll_id)s,
      %(observed_from_tag)s, %(team_index)s, %(raw_battle_hash)s, %(possible_gap)s,
      false
    )
    ON CONFLICT (battleTime, player_tag) DO UPDATE
    SET
      result                    = EXCLUDED.result,
      trophyChange              = EXCLUDED.trophyChange,
      pre_battle_brawler_trophies = COALESCE(EXCLUDED.pre_battle_brawler_trophies, battles.pre_battle_brawler_trophies),
      battletime_ts             = COALESCE(battles.battletime_ts, EXCLUDED.battletime_ts),
      mode                      = EXCLUDED.mode,
      event_mode_id             = COALESCE(EXCLUDED.event_mode_id, battles.event_mode_id),
      battle_mode               = COALESCE(EXCLUDED.battle_mode, battles.battle_mode),
      map                       = EXCLUDED.map,
      map_id                    = EXCLUDED.map_id,
      type                      = EXCLUDED.type,
      duration                  = EXCLUDED.duration,
      player_name               = COALESCE(battles.player_name, EXCLUDED.player_name),
      brawler_id                = COALESCE(EXCLUDED.brawler_id, battles.brawler_id),
      brawler_name              = EXCLUDED.brawler_name,
      brawler_power             = EXCLUDED.brawler_power,
      brawler_trophies          = EXCLUDED.brawler_trophies,
      is_starPlayer             = EXCLUDED.is_starPlayer,
      rank                      = EXCLUDED.rank,
      last_observed_at          = now(),
      source_poll_id            = EXCLUDED.source_poll_id,
      observed_from_tag         = COALESCE(battles.observed_from_tag, EXCLUDED.observed_from_tag),
      team_index                = COALESCE(battles.team_index, EXCLUDED.team_index),
      raw_battle_hash           = COALESCE(battles.raw_battle_hash, EXCLUDED.raw_battle_hash),
      possible_gap              = battles.possible_gap OR EXCLUDED.possible_gap;
    """

    enriched_rows: List[Dict[str, Any]] = []
    for r in rows:
        tag = r.get("player_tag")
        bname = r.get("brawler_name")
        pm = player_metrics.get(tag, {}) if tag else {}
        bmap = pm.get("brawlers", {}) or {}
        bm = bmap.get(bname, {}) if bname else {}

        r["player_total_trophies"] = pm.get("player_total_trophies")
        r["player_level"] = pm.get("player_level")
        r["brawler_id"] = r.get("brawler_id") or bm.get("brawler_id")
        r["brawler_total_trophies"] = bm.get("brawler_total_trophies")
        r["brawler_rank_on_account"] = bm.get("brawler_rank_on_account")
        r["is_highest_player_brawler"] = bm.get("is_highest_player_brawler", False)
        r.setdefault("source_poll_id", None)
        r.setdefault("event_mode_id", None)
        r.setdefault("battle_mode", None)
        r.setdefault("pre_battle_brawler_trophies", None)
        r.setdefault("observed_from_tag", None)
        r.setdefault("team_index", None)
        r.setdefault("raw_battle_hash", None)
        r.setdefault("possible_gap", False)

        enriched_rows.append(r)

    execute_batch(cur, sql, enriched_rows, page_size=100)

    execute_batch(
        cur,
        """
        INSERT INTO battle_sources (
          battletime, participant_tag, observed_from_tag, source_poll_id,
          first_observed_at, last_observed_at
        ) VALUES (
          %(battleTime)s, %(player_tag)s, %(observed_from_tag)s, %(source_poll_id)s,
          now(), now()
        )
        ON CONFLICT (battletime, participant_tag, observed_from_tag)
        DO UPDATE SET
          source_poll_id = EXCLUDED.source_poll_id,
          last_observed_at = now()
        """,
        [r for r in enriched_rows if r.get("observed_from_tag")],
        page_size=200,
    )
    conn.commit()

    cur.close()
    conn.close()

    return len(enriched_rows)


def persist_battlelog_payload(
    poll_id: int, observed_from_tag: str, collection_kind: str, payload: Dict[str, Any]
) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO battlelog_payloads (
                  source_poll_id, observed_from_tag, collection_kind,
                  payload_hash, payload, returned_items
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (observed_from_tag, collection_kind, payload_hash)
                DO UPDATE SET last_observed_at = now(), returned_items = EXCLUDED.returned_items
                RETURNING id
                """,
                (
                    poll_id, normalize_tag(observed_from_tag), collection_kind,
                    canonical_payload_hash(payload), json.dumps(payload),
                    len(payload.get("items", []) or []),
                ),
            )
            payload_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO battlelog_payload_observations (source_poll_id, payload_id)
                VALUES (%s, %s)
                ON CONFLICT (source_poll_id) DO NOTHING
                """,
                (poll_id, payload_id),
            )
        conn.commit()
    finally:
        conn.close()


def enqueue_reciprocal_requests(
    rows: List[Dict[str, Any]], new_target_times: Dict[str, Set[str]]
) -> int:
    """Queue only direct opponents from newly observed complete Solo lobbies."""
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        observer = normalize_tag(row.get("observed_from_tag"))
        battle_time = row.get("battleTime")
        if not observer or not battle_time or battle_time not in new_target_times.get(observer, set()):
            continue
        if row.get("mode") != "soloShowdown":
            continue
        grouped.setdefault((observer, battle_time), []).append(row)

    values: List[Tuple[Any, ...]] = []
    for (observer, battle_time), lobby in grouped.items():
        tags = sorted({normalize_tag(r.get("player_tag")) for r in lobby if r.get("player_tag")})
        if len(tags) != 10 or observer not in tags:
            continue
        event_ids = {r.get("map_id") for r in lobby if r.get("map_id") is not None}
        roster_hashes = {r.get("roster_hash") for r in lobby if r.get("roster_hash")}
        if len(event_ids) != 1 or len(roster_hashes) != 1:
            continue
        event_id = next(iter(event_ids))
        target_roster_hash = next(iter(roster_hashes))
        for opponent in tags:
            if opponent == observer:
                continue
            values.append((
                battle_time, battle_time, event_id, "soloShowdown",
                target_roster_hash, observer, opponent,
            ))

    if not values:
        return 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO reciprocal_battlelog_requests (
                  target_battle_time, target_battletime, target_event_id, target_mode,
                  target_roster_hash, discovered_from_tag, opponent_tag
                ) VALUES %s
                ON CONFLICT (target_battletime, target_event_id, target_mode, target_roster_hash, opponent_tag)
                DO NOTHING
                RETURNING id
                """,
                values,
                template="(%s::timestamptz, %s, %s, %s, %s, %s, %s)",
                page_size=200,
            )
            inserted = len(cur.fetchall())
        conn.commit()
        return max(inserted, 0)
    finally:
        conn.close()


def direct_opponent_profile_discoveries(
    rows: List[Dict[str, Any]], new_target_times: Dict[str, Set[str]]
) -> List[Tuple[Any, ...]]:
    """Return direct opponents from newly observed complete standard 3v3 lobbies.

    This consumes only tracked-player battle rows. Reciprocal rows are persisted
    later and never pass through this function, which is the non-recursion
    boundary for the profile pool.
    """
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        observer = normalize_tag(row.get("observed_from_tag"))
        battle_time = row.get("battleTime")
        if not observer or not battle_time or battle_time not in new_target_times.get(observer, set()):
            continue
        if row.get("mode") in ("soloShowdown", "duoShowdown"):
            continue
        grouped.setdefault((observer, battle_time), []).append(row)

    discoveries: List[Tuple[Any, ...]] = []
    for (observer, battle_time), lobby in grouped.items():
        tags = sorted({normalize_tag(r.get("player_tag")) for r in lobby if r.get("player_tag")})
        team_counts: Dict[int, int] = {}
        for row in lobby:
            team_index = row.get("team_index")
            if team_index is not None:
                team_counts[int(team_index)] = team_counts.get(int(team_index), 0) + 1
        # First challenger target is standard Ladder 3v3. Fail closed on partial,
        # duplicate, showdown, 5v5, or otherwise unfamiliar lobby structures.
        if len(tags) != 6 or observer not in tags or sorted(team_counts.values()) != [3, 3]:
            continue
        modes = {r.get("mode") for r in lobby if r.get("mode")}
        roster_hashes = {r.get("roster_hash") for r in lobby if r.get("roster_hash")}
        if len(modes) != 1 or len(roster_hashes) != 1:
            continue
        mode = next(iter(modes))
        lobby_hash = next(iter(roster_hashes))
        for opponent in tags:
            if opponent != observer:
                discoveries.append((opponent, observer, battle_time, mode, lobby_hash))
    return discoveries


def enqueue_opponent_profile_pool(
    rows: List[Dict[str, Any]], new_target_times: Dict[str, Set[str]]
) -> Dict[str, int]:
    """Persist bounded direct-opponent discoveries for future profile snapshots."""
    values = direct_opponent_profile_discoveries(rows, new_target_times)
    stats = {"eligible": len(values), "discoveries": 0, "new_players": 0}
    if not values:
        return stats
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Upsert the pool first so discovery rows always satisfy the FK.
            pool_values: Dict[str, Dict[str, Any]] = {}
            for tag, observer, battle_time, _mode, _hash in values:
                current = pool_values.get(tag)
                if current is None or battle_time > current["battle_time"]:
                    pool_values[tag] = {
                        "battle_time": battle_time,
                        "observer": observer,
                        "count": (current or {}).get("count", 0) + 1,
                    }
                else:
                    current["count"] += 1
            execute_values(
                cur,
                """
                INSERT INTO opponent_profile_pool (
                  player_tag, first_discovered_at, last_discovered_at, last_battle_time,
                  last_discovered_from_tag, discovery_count, next_profile_poll_at, metadata
                ) VALUES %s
                ON CONFLICT (player_tag) DO UPDATE SET
                  status = CASE WHEN opponent_profile_pool.status = 'paused'
                                THEN 'paused' ELSE 'active' END,
                  last_discovered_at = now(),
                  last_battle_time = GREATEST(opponent_profile_pool.last_battle_time, EXCLUDED.last_battle_time),
                  last_discovered_from_tag = EXCLUDED.last_discovered_from_tag,
                  discovery_count = opponent_profile_pool.discovery_count + EXCLUDED.discovery_count,
                  next_profile_poll_at = CASE
                    WHEN opponent_profile_pool.status = 'paused' THEN opponent_profile_pool.next_profile_poll_at
                    WHEN opponent_profile_pool.last_successful_profile_poll_at IS NULL THEN now()
                    ELSE LEAST(opponent_profile_pool.next_profile_poll_at, now() + interval '24 hours') END,
                  updated_at = now()
                RETURNING (xmax = 0) AS inserted
                """,
                [(tag, item["battle_time"], item["battle_time"], item["observer"], item["count"],
                  dt.datetime.now(dt.timezone.utc), json.dumps({"bounded": True, "recursive": False}))
                 for tag, item in pool_values.items()],
                template="(%s, %s::timestamptz, now(), %s::timestamptz, %s, %s, %s, %s::jsonb)",
                page_size=200,
            )
            stats["new_players"] = sum(1 for row in cur.fetchall() if row[0])
            execute_values(
                cur,
                """
                INSERT INTO opponent_profile_discoveries (
                  player_tag, discovered_from_tag, battle_time, mode, roster_hash
                ) VALUES %s
                ON CONFLICT (player_tag, discovered_from_tag, battle_time) DO NOTHING
                RETURNING id
                """,
                values,
                template="(%s, %s, %s::timestamptz, %s, %s)",
                page_size=200,
            )
            stats["discoveries"] = len(cur.fetchall())
        conn.commit()
        return stats
    finally:
        conn.close()


def load_reciprocal_requests(limit: int) -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, target_battletime, target_event_id, target_mode,
                       target_roster_hash, opponent_tag, attempts, first_requested_at
                FROM reciprocal_battlelog_requests
                WHERE status IN ('pending', 'missing', 'failed')
                  AND attempts < %s
                  AND first_requested_at >= now() - make_interval(mins => %s)
                ORDER BY first_requested_at, id
                LIMIT %s
                """,
                (RECIPROCAL_MAX_ATTEMPTS, RECIPROCAL_TTL_MINUTES, limit),
            )
            columns = [d.name for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def expire_reciprocal_requests() -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE reciprocal_battlelog_requests
                SET status = 'expired', error_message = COALESCE(error_message, 'retry window exhausted')
                WHERE status IN ('pending', 'missing', 'failed')
                  AND (attempts >= %s OR first_requested_at < now() - make_interval(mins => %s))
                """,
                (RECIPROCAL_MAX_ATTEMPTS, RECIPROCAL_TTL_MINUTES),
            )
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def record_reciprocal_poll(tag: str, payload: Dict[str, Any]) -> int:
    url = f"{BASE_URL}/players/{urllib.parse.quote(normalize_tag(tag))}/battlelog"
    with API_META_LOCK:
        api_meta = dict(API_META.get(url, {}))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_polls (
                  player_tag, endpoint, completed_at, http_status, returned_items,
                  api_latency_ms, cache_max_age_seconds, retry_after_seconds,
                  rate_limited, success, metadata
                ) VALUES (%s, 'battlelog_reciprocal', now(), 200, %s, %s, %s, %s, %s, true,
                          '{"bounded":true,"recursive":false}'::jsonb)
                RETURNING id
                """,
                (
                    normalize_tag(tag), len(payload.get("items", []) or []),
                    api_meta.get("api_latency_ms"), api_meta.get("cache_max_age_seconds"),
                    api_meta.get("retry_after_seconds"), api_meta.get("rate_limited", False),
                ),
            )
            poll_id = cur.fetchone()[0]
        conn.commit()
        return poll_id
    finally:
        conn.close()


def update_reciprocal_request(
    request_id: int, status: str, poll_id: Optional[int] = None,
    matched_hash: Optional[str] = None, error: Optional[str] = None,
) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE reciprocal_battlelog_requests
                SET status = %s, attempts = attempts + 1, last_attempted_at = now(),
                    matched_at = CASE WHEN %s = 'matched' THEN now() ELSE matched_at END,
                    source_poll_id = COALESCE(%s, source_poll_id),
                    matched_raw_battle_hash = COALESCE(%s, matched_raw_battle_hash),
                    error_message = %s
                WHERE id = %s
                """,
                (status, status, poll_id, matched_hash, error, request_id),
            )
        conn.commit()
    finally:
        conn.close()


def collect_reciprocal_logs() -> Dict[str, int]:
    stats = {"requested": 0, "matched": 0, "missing": 0, "ambiguous": 0, "failed": 0, "saved": 0}
    requests_to_process = load_reciprocal_requests(RECIPROCAL_MAX_REQUESTS_PER_RUN)
    if not requests_to_process:
        expire_reciprocal_requests()
        return stats

    by_tag: Dict[str, List[Dict[str, Any]]] = {}
    for request in requests_to_process:
        by_tag.setdefault(normalize_tag(request["opponent_tag"]), []).append(request)
    payloads = fetch_battlelogs_parallel(list(by_tag), max_workers=BATTLELOG_WORKERS)

    for tag, requests_for_tag in by_tag.items():
        payload = payloads.get(tag)
        if payload is None:
            status, error = reciprocal_unavailable_status(battlelog_http_status(tag))
            for request in requests_for_tag:
                update_reciprocal_request(request["id"], status, error=error)
                stats[status] = stats.get(status, 0) + 1
            continue
        poll_id = record_reciprocal_poll(tag, payload)
        persist_battlelog_payload(poll_id, tag, "reciprocal", payload)
        stats["requested"] += 1

        # Persist only this directly requested player's own rows. This is the
        # hard boundary that prevents recursive participant expansion.
        own_rows, _ = flatten_battlelog(payload, tag)
        own_rows = [r for r in own_rows if normalize_tag(r.get("player_tag")) == tag]
        for row in own_rows:
            row["source_poll_id"] = poll_id
            row["possible_gap"] = False
        if own_rows:
            stats["saved"] += save_to_cloud(own_rows, {})

        for request in requests_for_tag:
            status, matched_item = match_reciprocal_item(
                payload, request["target_battletime"], request["target_event_id"],
                request["target_mode"], request["target_roster_hash"],
            )
            matched_hash = canonical_payload_hash(matched_item) if matched_item else None
            update_reciprocal_request(request["id"], status, poll_id, matched_hash)
            stats[status] += 1

    expire_reciprocal_requests()
    return stats


# =========================================================
# MAIN callable function (FastAPI can import this)
# =========================================================
def collect_battles_for_tags(tags: List[str]) -> Dict[str, Any]:
    if not tags:
        return {"tracked": 0, "participants": 0, "saved": 0, "unique_players": 0}

    tags = [normalize_tag(t) for t in (tags or []) if t]

    all_rows: List[Dict[str, Any]] = []
    all_player_tags: Set[str] = set()
    new_target_times: Dict[str, Set[str]] = {}

    # 1) Battlelogs in parallel
    battlelogs_by_tag = fetch_battlelogs_parallel(tags, max_workers=BATTLELOG_WORKERS)
    failed_tags = sorted(set(tags) - set(battlelogs_by_tag))
    if failed_tags:
        conn = get_conn()
        cur = conn.cursor()
        for tag in failed_tags:
            cur.execute(
                """
                INSERT INTO collector_polls (
                  player_tag, endpoint, completed_at, success, error_category, error_message
                ) VALUES (%s, 'battlelog', now(), false, 'fetch_failed', 'battlelog unavailable')
                """,
                (tag,),
            )
            cur.execute(
                """
                INSERT INTO tracked_player_state (
                  player_tag, last_battle_poll_at, polling_interval_seconds,
                  next_battle_poll_at, updated_at
                ) VALUES (%s, now(), 86400, now() + interval '24 hours', now())
                ON CONFLICT (player_tag) DO UPDATE SET
                  last_battle_poll_at = now(),
                  polling_interval_seconds = 86400,
                  next_battle_poll_at = now() + interval '24 hours',
                  updated_at = now()
                """,
                (tag,),
            )
        conn.commit()
        cur.close()
        conn.close()

    # 2) Flatten each battlelog and record per-tracked-player telemetry.
    for tag, battlelog in battlelogs_by_tag.items():
        rows, player_tags = flatten_battlelog(battlelog, tag)
        meta_url = f"{BASE_URL}/players/{urllib.parse.quote(normalize_tag(tag))}/battlelog"
        with API_META_LOCK:
            api_meta = dict(API_META.get(meta_url, {}))
        own_times = sorted({r.get("battleTime") for r in rows if r.get("player_tag") == tag and r.get("battleTime")})
        conn = get_conn()
        cur = conn.cursor()
        existing = set()
        if own_times:
            cur.execute(
                "SELECT battletime FROM battles WHERE player_tag = %s AND battletime = ANY(%s)",
                (tag, own_times),
            )
            existing = {r[0] for r in cur.fetchall()}
        new_items = len(set(own_times) - existing)
        new_target_times[tag] = set(own_times) - existing
        returned_items = len(battlelog.get("items", []) or [])
        possible_gap = new_items >= 20 or (returned_items >= 25 and new_items >= 15)
        newest = max(own_times) if own_times else None
        oldest = min(own_times) if own_times else None
        cur.execute(
            """
            INSERT INTO collector_polls (
              player_tag, endpoint, completed_at, http_status, returned_items,
              new_items, duplicate_items, oldest_battle_time, newest_battle_time,
              api_latency_ms, cache_max_age_seconds, retry_after_seconds, rate_limited,
              success, possible_gap
            ) VALUES (
              %s, 'battlelog', now(), 200, %s, %s, %s,
              CASE WHEN %s IS NULL THEN NULL ELSE %s::timestamptz END,
              CASE WHEN %s IS NULL THEN NULL ELSE %s::timestamptz END,
              %s, %s, %s, %s,
              true, %s
            ) RETURNING id
            """,
            (tag, returned_items, new_items, max(returned_items - new_items, 0),
             oldest, oldest, newest, newest,
             api_meta.get("api_latency_ms"), api_meta.get("cache_max_age_seconds"),
             api_meta.get("retry_after_seconds"), api_meta.get("rate_limited", False),
             possible_gap),
        )
        poll_id = cur.fetchone()[0]
        interval = 300 if new_items >= 15 else 900
        cur.execute(
            """
            INSERT INTO tracked_player_state (
              player_tag, last_battle_poll_at, last_successful_battle_poll_at,
              newest_battle_time, consecutive_high_velocity, polling_interval_seconds,
              next_battle_poll_at, possible_gap, updated_at
            ) VALUES (
              %s, now(), now(), CASE WHEN %s IS NULL THEN NULL ELSE %s::timestamptz END,
              CASE WHEN %s >= 15 THEN 1 ELSE 0 END, %s,
              now() + make_interval(secs => %s), %s, now()
            )
            ON CONFLICT (player_tag) DO UPDATE SET
              last_battle_poll_at = now(),
              last_successful_battle_poll_at = now(),
              newest_battle_time = GREATEST(tracked_player_state.newest_battle_time, EXCLUDED.newest_battle_time),
              consecutive_high_velocity = CASE WHEN %s >= 15
                THEN tracked_player_state.consecutive_high_velocity + 1 ELSE 0 END,
              polling_interval_seconds = EXCLUDED.polling_interval_seconds,
              next_battle_poll_at = EXCLUDED.next_battle_poll_at,
              possible_gap = tracked_player_state.possible_gap OR EXCLUDED.possible_gap,
              updated_at = now()
            """,
            (tag, newest, newest, new_items, interval, interval, possible_gap, new_items),
        )
        conn.commit()
        cur.close()
        conn.close()
        persist_battlelog_payload(poll_id, tag, "tracked", battlelog)
        for row in rows:
            row["source_poll_id"] = poll_id
            row["possible_gap"] = possible_gap
        all_rows.extend(rows)
        all_player_tags.update(player_tags)

    # Deduplicate (battleTime, player_tag)
    unique: Dict[tuple, Dict[str, Any]] = {}
    for r in all_rows:
        key = (r.get("battleTime"), r.get("player_tag"))
        unique[key] = r
    all_rows = list(unique.values())

    # 3) Save battle payload facts. Profile enrichment is timestamped separately
    # by profile_updater; incidental opponents are never recursively fetched.
    saved = save_to_cloud(all_rows, {})

    reciprocal = {"enabled": RECIPROCAL_COLLECTION_ENABLED}
    if RECIPROCAL_COLLECTION_ENABLED:
        reciprocal["enqueued"] = enqueue_reciprocal_requests(all_rows, new_target_times)
        reciprocal.update(collect_reciprocal_logs())

    opponent_profiles = {"enabled": OPPONENT_PROFILE_COLLECTION_ENABLED}
    if OPPONENT_PROFILE_COLLECTION_ENABLED:
        opponent_profiles.update(enqueue_opponent_profile_pool(all_rows, new_target_times))

    return {
        "tracked": len(tags),
        "participants": len(all_rows),
        "saved": saved,
        "unique_players": len(all_player_tags),
        "reciprocal": reciprocal,
        "opponent_profiles": opponent_profiles,
    }


def main():
    tags = get_tracked_tags()
    print(f"Tracking battlelogs for {len(tags)} players")
    out = collect_battles_for_tags(tags)
    print(f"[collector] done: {out} @ {dt.datetime.now()}")


if __name__ == "__main__":
    main()

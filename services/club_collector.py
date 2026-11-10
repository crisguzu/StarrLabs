import os
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2
from database import connect as database_connect
from psycopg2.extras import RealDictCursor, execute_batch

BASE_URL = "https://api.brawlstars.com/v1"

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

CLUB_REQUEST_DELAY = float(os.getenv("CLUB_REQUEST_DELAY", "0.2"))


def normalize_tag(tag: str) -> str:
    tag = (tag or "").strip().upper().replace("O", "0")
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


def fetch_player_club(owner_tag: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (club_tag, club_name) for a player."""
    owner_tag = normalize_tag(owner_tag)
    if not owner_tag:
        return None, None
    encoded = urllib.parse.quote(owner_tag)
    url = f"{BASE_URL}/players/{encoded}"
    r = requests.get(url, headers=_headers(), timeout=15)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    j = r.json() or {}
    club = j.get("club") or {}
    return club.get("tag"), club.get("name")


def fetch_club(club_tag: str) -> Dict[str, Any]:
    club_tag = normalize_tag(club_tag)
    encoded = urllib.parse.quote(club_tag)
    url = f"{BASE_URL}/clubs/{encoded}"
    r = requests.get(url, headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def _upsert_club_and_members(club_json: Dict[str, Any]) -> Dict[str, int]:
    """Persist club + members. Also updates player_profiles with what club payload contains."""
    club_tag = normalize_tag(club_json.get("tag"))
    if not club_tag:
        return {"saved_members": 0}

    club_name = club_json.get("name")
    club_desc = club_json.get("description")
    club_type = club_json.get("type")
    required_trophies = club_json.get("requiredTrophies")
    trophies = club_json.get("trophies")
    badge_id = (club_json.get("badgeId") or club_json.get("badge"))

    members = club_json.get("members") or []
    now = datetime.now(timezone.utc)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Clubs table (optional, but recommended)
        cur.execute(
            """
            INSERT INTO clubs (tag, name, description, type, badge_id, required_trophies, trophies, last_update)
            VALUES (%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (tag) DO UPDATE SET
              name = EXCLUDED.name,
              description = EXCLUDED.description,
              type = EXCLUDED.type,
              badge_id = EXCLUDED.badge_id,
              required_trophies = EXCLUDED.required_trophies,
              trophies = EXCLUDED.trophies,
              last_update = now();
            """,
            (club_tag, club_name, club_desc, club_type, badge_id, required_trophies, trophies),
        )

        # Mark all existing members inactive first
        cur.execute(
            """
            UPDATE club_memberships
            SET is_active = FALSE
            WHERE club_tag = %s;
            """,
            (club_tag,),
        )

        # Upsert members + touch last_seen
        member_rows = []
        profile_rows = []
        for m in members:
            ptag = normalize_tag(m.get("tag"))
            if not ptag:
                continue

            member_rows.append(
                {
                    "club_tag": club_tag,
                    "player_tag": ptag,
                    "name": m.get("name"),
                    "name_color": m.get("nameColor"),
                    "role": m.get("role"),
                    "trophies": m.get("trophies"),
                    "icon_id": (m.get("icon") or {}).get("id"),
                    "now": now,
                }
            )

            # Lightweight upsert to player_profiles so the list can render immediately.
            profile_rows.append(
                {
                    "tag": ptag,
                    "name": m.get("name") or ptag,
                    "icon_id": (m.get("icon") or {}).get("id"),
                    "trophies": m.get("trophies"),
                    "club_tag": club_tag,
                    "club_name": club_name,
                }
            )

        if profile_rows:
            execute_batch(
                cur,
                """
                INSERT INTO player_profiles (tag, name, icon_id, trophies, club_tag, club_name)
                VALUES (%(tag)s, %(name)s, %(icon_id)s, %(trophies)s, %(club_tag)s, %(club_name)s)
                ON CONFLICT (tag) DO UPDATE SET
                  name = EXCLUDED.name,
                  icon_id = COALESCE(EXCLUDED.icon_id, player_profiles.icon_id),
                  trophies = COALESCE(EXCLUDED.trophies, player_profiles.trophies),
                  club_tag = EXCLUDED.club_tag,
                  club_name = EXCLUDED.club_name,
                  last_update = now();
                """,
                profile_rows,
                page_size=100,
            )

        if member_rows:
            execute_batch(
                cur,
                """
                INSERT INTO club_memberships (
                  club_tag, player_tag, name, name_color, role, trophies, icon_id,
                  is_active, first_seen, last_seen
                ) VALUES (
                  %(club_tag)s, %(player_tag)s, %(name)s, %(name_color)s, %(role)s, %(trophies)s, %(icon_id)s,
                  TRUE,
                  COALESCE((SELECT first_seen FROM club_memberships WHERE club_tag=%(club_tag)s AND player_tag=%(player_tag)s), now()),
                  now()
                )
                ON CONFLICT (club_tag, player_tag) DO UPDATE SET
                  name = EXCLUDED.name,
                  name_color = EXCLUDED.name_color,
                  role = EXCLUDED.role,
                  trophies = EXCLUDED.trophies,
                  icon_id = COALESCE(EXCLUDED.icon_id, club_memberships.icon_id),
                  is_active = TRUE,
                  last_seen = now();
                """,
                member_rows,
                page_size=100,
            )

        conn.commit()
        return {"saved_members": len(member_rows)}
    finally:
        cur.close()
        conn.close()


def sync_club_for_owner(owner_tag: str) -> Dict[str, Any]:
    """Fetch the owner's club and persist it. Returns {ok, club_tag, club_name, member_tags}."""
    owner_tag = normalize_tag(owner_tag)
    club_tag, club_name = fetch_player_club(owner_tag)
    if not club_tag:
        return {"ok": False, "message": "This player is not in a club."}

    club_json = fetch_club(club_tag)
    time.sleep(CLUB_REQUEST_DELAY)
    _upsert_club_and_members(club_json)

    members = club_json.get("members") or []
    member_tags = [normalize_tag(m.get("tag")) for m in members if m.get("tag")]
    return {
        "ok": True,
        "club_tag": club_tag,
        "club_name": club_name,
        "member_count": len(member_tags),
        "member_tags": member_tags,
    }

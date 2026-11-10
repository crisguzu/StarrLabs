
import os
import re
import uuid
import json
import threading
import datetime as dt
from datetime import datetime, timezone,date, timedelta
from pathlib import Path

import math
import secrets
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import psycopg2
import requests
from database import connect as database_connect
from psycopg2.extras import RealDictCursor, execute_batch

from fastapi import FastAPI, Request, Form, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder

from starlette.middleware.sessions import SessionMiddleware

from services.profile_updater import update_profiles, ensure_scout_profiles
from services.collector import collect_battles_for_tags
from services.daily_activity import snapshot_daily_activity
from services.player_brawler_daily_activity import snapshot_brawler_daily
from services.club_collector import sync_club_for_owner
from services.player_verification import api_battlelog, api_player as verification_api_player
from services.player_verification import api_rotation, build_tasks, matching_progress
from services.victory_score_calculation import bayesian_effect_score
from services.bot_detection import lobby_detection, participant_evidence
from services.event_dashboard import build_event_dashboard, event_stages_for_date
from services.email_auth import (
    COOKIE_NAME as EMAIL_AUTH_COOKIE,
    MAX_CODE_ATTEMPTS,
    SESSION_TTL_DAYS,
    challenge_expiry,
    code_digest,
    hash_request_value,
    new_code,
    new_session_token,
    normalize_email,
    render_login_email,
    safe_next_path,
    send_login_email,
    session_expiry,
    token_digest,
    verify_turnstile,
)


refresh_jobs = {}  # Single supported server worker, finite retained progress.
collection_slot = threading.BoundedSemaphore(1)
refresh_jobs_lock = threading.Lock()
scid_poc_challenges = {}
scid_poc_attempts = {}
scid_poc_lock = threading.Lock()
missy_maintenance_lock = threading.Lock()

load_dotenv(Path(__file__).resolve().parent / ".env")

# ---------- Config ----------
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if len(SESSION_SECRET) < 32 or SESSION_SECRET in {"dev-only-change-me", "GENERATE_A_LONG_RANDOM_LOCAL_SECRET"}:
    raise RuntimeError("Set SESSION_SECRET to a unique random value of at least 32 characters before starting StarrLabs")

# Rollup sentinels (must match your victory_score_calculation.py)
ALL_DIM = os.getenv("VICTORY_ALL_DIM", "__ALL__")
UNKNOWN_DIM = os.getenv("VICTORY_UNKNOWN_DIM", "__UNKNOWN__")
VICTORY_WINDOW_DAYS = int(os.getenv("VICTORY_WINDOW_DAYS", "30"))
PRED_WIN_THRESHOLD = float(os.getenv("VICTORY_PRED_THRESHOLD", "0.60"))
SCID_POC_ENABLED = os.getenv("SCID_POC_ENABLED", "0") == "1"
SCID_POC_ADMIN_EMAILS = {
    value.strip().lower()
    for value in os.getenv("SCID_POC_ADMIN_EMAILS", "").split(",")
    if value.strip()
}
MISSY_ADMIN_EMAILS = {
    value.strip().lower()
    for value in os.getenv("MISSY_ADMIN_EMAILS", os.getenv("SCID_POC_ADMIN_EMAILS", "")).split(",")
    if value.strip()
}
SCID_POC_TTL_SECONDS = 600
SCID_POC_SUPERCELL_BASE = "https://ingame.id.supercell.com/api"
PLAYER_VERIFICATION_TTL_MINUTES = int(os.getenv("PLAYER_VERIFICATION_TTL_MINUTES", "15"))


def _add_victory_score_windows(cur, players):
    """Attach honest rolling V2 scores for the compact 7d/30d home cards."""
    tags = [p.get("tag") for p in players if p.get("tag")]
    if not tags:
        return
    cur.execute(
        """
        SELECT player_tag, win_label, pred_win_prob, battletime
        FROM battle_features_3v3
        WHERE player_tag = ANY(%s)
          AND battletime >= now() - interval '30 days'
          AND pred_win_prob IS NOT NULL
          AND win_label IN (0, 1)
        ORDER BY player_tag, battletime
        """,
        (tags,),
    )
    now = datetime.now(timezone.utc)
    games = {tag: {7: [], 30: []} for tag in tags}
    for row in cur.fetchall() or []:
        battle_time = row.get("battletime")
        if battle_time and battle_time.tzinfo is None:
            battle_time = battle_time.replace(tzinfo=timezone.utc)
        age_days = (now - battle_time).total_seconds() / 86400 if battle_time else 31
        game = (int(row["win_label"]), float(row["pred_win_prob"]))
        if age_days <= 30:
            games[row["player_tag"]][30].append(game)
        if age_days <= 7:
            games[row["player_tag"]][7].append(game)
    for player in players:
        tag_games = games.get(player.get("tag"), {7: [], 30: []})
        for days in (7, 30):
            result = bayesian_effect_score(tag_games[days])
            score = result.get("victory_score")
            player[f"victory_score_{days}d"] = round(score) if score is not None else None
            player[f"victory_score_{days}d_battles"] = len(tag_games[days])

from fastapi import Depends

def _standalone_route_access(request: Request):
    from access_policy import require_standalone_access
    return require_standalone_access(request, _identity_email, get_conn)

app = FastAPI(title="StarrLabs — Battle Research Division", dependencies=[Depends(_standalone_route_access)])

from database import DatabasePoolBusyError

@app.exception_handler(DatabasePoolBusyError)
async def database_budget_busy(request: Request, exc):
    return JSONResponse({"ok": False, "message": "Database busy; retry shortly."}, status_code=503, headers={"Retry-After": "1", "Cache-Control": "no-store"})
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=os.getenv("SESSION_HTTPS_ONLY", "0") == "1",
    max_age=int(os.getenv("SESSION_MAX_AGE_SECONDS", "1209600")),
)

templates = Jinja2Templates(directory="templates")
from standalone_assets import StandaloneStaticFiles
app.mount("/static", StandaloneStaticFiles(directory="static"), name="static")

from starlette.middleware.trustedhost import TrustedHostMiddleware
from standalone_security import allowed_hosts, allowed_origins, legacy_operator_path, trusted_mutation
allowed_origins()  # Validate configuration before serving requests.
from services.email_auth import _secret as _validate_auth_secret
_validate_auth_secret()
if any(host not in {"127.0.0.1", "localhost"} for host in allowed_hosts()) and os.getenv("SESSION_HTTPS_ONLY", "0") != "1":
    raise RuntimeError("Remote hosts require HTTPS and SESSION_HTTPS_ONLY=1")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())

@app.middleware("http")
async def standalone_security_boundary(request: Request, call_next):
    if legacy_operator_path(request.url.path):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not trusted_mutation(request.headers):
        return JSONResponse({"detail": "A configured same-origin request is required"}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith(("/api/", "/login", "/verify-player/")):
        response.headers["Cache-Control"] = "no-store"
    if os.getenv("STARRLABS_REMOTE_MEDIA_ENABLED", "0") != "1":
        policy = response.headers.get("Content-Security-Policy", "")
        if policy:
            response.headers["Content-Security-Policy"] = re.sub(r"img-src[^;]*", "img-src 'self' data:", policy)
    return response


@app.middleware("http")
async def canonical_starrlabs_host(request: Request, call_next):
    # Explicit Host/Origin configuration replaces deployment-specific redirects.
    return await call_next(request)


# ---------- Helpers ----------
def get_conn():
    return database_connect()

def brawlify_profile_icon_url(icon_id) -> str:
    if not icon_id:
        return None
    # ✅ This is the SAME path you used in player.html
    return f"https://cdn.brawlify.com/profile-icons/regular/{icon_id}.png"

def normalize_tag(tag: str) -> str:
    tag = (tag or "").strip().upper().replace("O", "0")  # optional O→0 fix
    if not tag:
        return tag
    if not tag.startswith("#"):
        tag = "#" + tag
    return tag


def is_valid_player_tag(tag: str) -> bool:
    """Validate the documented Brawl Stars tag alphabet before an API lookup."""
    return bool(re.fullmatch(r"#[0289PYLQGRJCUV]+", normalize_tag(tag) or ""))




def _app_session_email(request: Request):
    raw_token = request.cookies.get(EMAIL_AUTH_COOKIE)
    if not raw_token:
        return None
    digest = token_digest(raw_token)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT identity_email
            FROM starrlabs_identity_sessions
            WHERE token_digest = %s AND revoked_at IS NULL AND expires_at > now()
            LIMIT 1
            """,
            (digest,),
        )
        row = cur.fetchone()
    return row["identity_email"] if row else None


def _identity_email(request: Request, required=False):
    """StarrLabs user identity from its own opaque server-side session."""
    email = _app_session_email(request)
    if not email:
        email = None  # Development identity overrides are not supported in this distribution.
    if required and not email:
        raise HTTPException(status_code=401, detail="Log in with your verified email first.")
    return email or None


def _request_ip(request: Request) -> str:
    return ((request.client.host if request.client else "") or "unknown").strip()


def _require_same_site(request: Request):
    if not trusted_mutation(request.headers):
        raise HTTPException(status_code=403, detail="A configured same-origin request is required")


def _security_headers() -> dict[str, str]:
    captcha = " https://challenges.cloudflare.com" if os.getenv("TURNSTILE_SITE_KEY") else ""
    images = "'self' data:"
    if os.getenv("STARRLABS_REMOTE_MEDIA_ENABLED", "0") == "1":
        images += " https://cdn.brawlify.com https://media.ffycdn.net"
    policy = ("default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; "
              "script-src 'self' 'unsafe-inline'" + captcha + "; style-src 'self' 'unsafe-inline'; img-src " + images + "; font-src 'self' data:; connect-src 'self'" + captcha + "; frame-src " + (captcha.strip() if captcha else "'none'") + ";")
    headers = {"Content-Security-Policy": policy, "X-Content-Type-Options":"nosniff", "X-Frame-Options":"DENY", "Referrer-Policy":"no-referrer", "Permissions-Policy":"camera=(), microphone=(), geolocation=(), payment=(), usb=(), accelerometer=(), gyroscope=(), magnetometer=()"}
    if os.getenv("SESSION_HTTPS_ONLY", "0") == "1":
        headers["Content-Security-Policy"] += " upgrade-insecure-requests;"
        headers["Strict-Transport-Security"] = "max-age=31536000"
    return headers


@app.middleware("http")
async def protect_browser_requests(request: Request, call_next):
    """Reject cross-site mutations and attach a consistent browser policy."""
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        try:
            _require_same_site(request)
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    for name, value in _security_headers().items():
        response.headers[name] = value
    return response


def _verified_player(cur, player_tag):
    cur.execute(
        """
        SELECT player_tag, identity_email, verified_at, last_verified_at
        FROM player_verifications
        WHERE player_tag = %s AND status = 'verified'
        """,
        (player_tag,),
    )
    return cur.fetchone()


def _audit_account_action(cur, player_tag, actor_email, action, previous_email=None, details=None):
    cur.execute(
        """
        INSERT INTO player_account_audit(
          player_tag, actor_identity_email, action, previous_identity_email, details
        ) VALUES (%s, %s, %s, %s, %s::jsonb)
        """,
        (player_tag, actor_email, action, previous_email, json.dumps(details or {})),
    )










def _coverage_polyline(values, width=240, height=54):
    """Return bounded SVG points for a compact metric history trace."""
    values = [float(value) for value in values if value is not None]
    if not values:
        return ""
    if len(values) == 1:
        return f"0,{height / 2:.1f} {width},{height / 2:.1f}"
    low, high = min(values), max(values)
    spread = high - low
    if spread == 0:
        spread = max(abs(high) * 0.1, 1.0)
        low -= spread / 2
        high += spread / 2
    return " ".join(
        f"{index * width / (len(values) - 1):.1f},{height - ((value - low) / (high - low)) * height:.1f}"
        for index, value in enumerate(values)
    )










def victory_tier_and_label(score):
    if score is None:
        return None, None
    try:
        s = float(score)
    except Exception:
        return None, None

    if s >= 81:
        return "S", "Exceptional overperformance"
    if s >= 66:
        return "A", "Strongly above expectation"
    if s >= 56:
        return "B", "Above expectation"
    if s >= 45:
        return "C", "As expected"
    if s >= 35:
        return "D", "Below expectation"
    return "E", "Well below expectation"


def get_weekly_deltas(cur, player_tag: str) -> dict:
    cur.execute(
        """
        WITH latest AS (
          SELECT *
          FROM player_daily_activity
          WHERE player_tag = %s
          ORDER BY activity_date DESC
          LIMIT 1
        ),
        previous AS (
          SELECT *
          FROM player_daily_activity
          WHERE player_tag = %s
            AND activity_date <= (
              SELECT activity_date FROM latest
            ) - INTERVAL '7 days'
          ORDER BY activity_date DESC
          LIMIT 1
        )
        SELECT
          l.trophies              AS trophies_now,
          p.trophies              AS trophies_prev,
          l.highest_trophies      AS highest_now,
          p.highest_trophies      AS highest_prev,
          l.exp_level             AS exp_now,
          p.exp_level             AS exp_prev,
          l.victories_3v3         AS v3_now,
          p.victories_3v3         AS v3_prev,
          l.victories_solo        AS solo_now,
          p.victories_solo        AS solo_prev,
          l.victories_duo         AS duo_now,
          p.victories_duo         AS duo_prev
        FROM latest l
        LEFT JOIN previous p ON TRUE;
        """,
        (player_tag, player_tag),
    )
    r = cur.fetchone()
    if not r:
        return {}

    def delta(now, prev):
        if now is None or prev is None:
            return None
        return int(now) - int(prev)

    return {
        "trophies": delta(r["trophies_now"], r["trophies_prev"]),
        "highest_trophies": delta(r["highest_now"], r["highest_prev"]),
        "exp_level": delta(r["exp_now"], r["exp_prev"]),
        "3v3_victories": delta(r["v3_now"], r["v3_prev"]),
        "solo_victories": delta(r["solo_now"], r["solo_prev"]),
        "duo_victories": delta(r["duo_now"], r["duo_prev"]),
    }


def _session_owner_tag(request: Request):
    t = request.session.get("owner_tag")
    return normalize_tag(t) if t else None


def _session_selected_list_id(request: Request):
    return request.session.get("selected_list_id")


def _ensure_default_list(cur, owner_tag: str):
    """
    Ensure owner has at least one list.
    Returns (selected_list_id).
    """
    cur.execute(
        """
        SELECT id
        FROM player_lists
        WHERE owner_tag = %s AND is_deleted = FALSE
        ORDER BY is_favorite DESC, updated_at DESC, id DESC
        LIMIT 1;
        """,
        (owner_tag,),
    )
    row = cur.fetchone()
    if row:
        return row["id"]

    # create a default list
    cur.execute(
        """
        INSERT INTO player_lists (owner_tag, name, is_favorite, is_deleted)
        VALUES (%s, %s, FALSE, FALSE)
        RETURNING id;
        """,
        (owner_tag, "My Tracked Players"),
    )
    return cur.fetchone()["id"]

def _ensure_system_club_list(cur, owner_tag: str) -> int:
    cur.execute(
        """
        SELECT id
        FROM player_lists
        WHERE owner_tag = %s AND system_key = 'my_club'
        LIMIT 1;
        """,
        (owner_tag,),
    )
    r = cur.fetchone()
    if r:
        return int(r["id"])

    token = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO player_lists (
          owner_tag, name, is_favorite, is_deleted,
          is_system, system_key, is_deletable,
          is_public, share_token
        )
        VALUES (%s, %s, FALSE, FALSE, TRUE, 'my_club', FALSE, TRUE, %s)
        RETURNING id;
        """,
        (owner_tag, "My club", token),
    )
    return int(cur.fetchone()["id"])


def _ensure_my_accounts_list(cur, identity_email: str, anchor_tag: str) -> int:
    cur.execute(
        """
        SELECT id
        FROM player_lists
        WHERE identity_email = %s AND system_key = 'my_accounts' AND is_deleted = FALSE
        LIMIT 1
        """,
        (identity_email,),
    )
    row = cur.fetchone()
    if row:
        list_id = int(row["id"])
    else:
        cur.execute(
            """
            INSERT INTO player_lists (
              owner_tag, identity_email, name, is_favorite, is_deleted,
              is_system, system_key, is_deletable, is_public
            )
            VALUES (%s, %s, 'My Accounts', FALSE, FALSE, TRUE, 'my_accounts', FALSE, FALSE)
            RETURNING id
            """,
            (anchor_tag, identity_email),
        )
        list_id = int(cur.fetchone()["id"])
    cur.execute(
        """
        INSERT INTO player_list_members (list_id, player_tag, is_removed)
        SELECT %s, player_tag, FALSE
        FROM player_verifications
        WHERE identity_email = %s AND status = 'verified'
        ON CONFLICT (list_id, player_tag)
        DO UPDATE SET is_removed = FALSE, removed_at = NULL
        """,
        (list_id, identity_email),
    )
    return list_id


def _list_accessible_to(cur, list_id: int, owner_tag: str, identity_email: str = None) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM player_lists
        WHERE id = %s
          AND is_deleted = FALSE
          AND (
            owner_tag = %s
            OR (system_key = 'my_accounts' AND identity_email = %s)
          )
        LIMIT 1
        """,
        (list_id, owner_tag, identity_email),
    )
    return cur.fetchone() is not None



def _get_best_selected_list(cur, owner_tag: str):
    """
    Pick favorite if exists, else most recently updated.
    """
    cur.execute(
        """
        SELECT id
        FROM player_lists
        WHERE owner_tag = %s AND is_deleted = FALSE
        ORDER BY is_favorite DESC, updated_at DESC, id DESC
        LIMIT 1;
        """,
        (owner_tag,),
    )
    r = cur.fetchone()
    return r["id"] if r else None


def _list_belongs_to_owner(cur, list_id: int, owner_tag: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM player_lists
        WHERE id = %s AND owner_tag = %s AND is_deleted = FALSE
        LIMIT 1;
        """,
        (list_id, owner_tag),
    )
    return cur.fetchone() is not None


def _get_list_meta_by_token(cur, token: str):
    cur.execute(
        """
        SELECT id, owner_tag, name, is_system, is_deletable, is_deleted, is_public
        FROM player_lists
        WHERE share_token = %s
        LIMIT 1;
        """,
        (token,),
    )
    return cur.fetchone()


def _public_list_by_token(cur, token: str):
    cur.execute(
        """
        SELECT id, name, owner_tag
        FROM player_lists
        WHERE share_token = %s
          AND is_public = TRUE
          AND is_deleted = FALSE
        LIMIT 1;
        """,
        (token,),
    )
    return cur.fetchone()


def _auth_audit(cur, action, email=None, challenge_id=None, session_id=None, ip_hash=None, details=None):
    cur.execute(
        """
        INSERT INTO starrlabs_auth_audit(
          identity_email, action, challenge_id, session_id, ip_hash, details
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (email, action, challenge_id, session_id, ip_hash, json.dumps(details or {})),
    )


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    destination = safe_next_path(next)
    if _identity_email(request):
        return RedirectResponse(destination, status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next_path": destination,
            "active_page": "login",
            "turnstile_site_key": os.getenv("TURNSTILE_SITE_KEY", ""),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/auth/email/start")
async def email_login_start(request: Request):
    _require_same_site(request)
    payload = await request.json()
    try:
        email = normalize_email(str(payload.get("email") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    destination = safe_next_path(payload.get("next"))
    if not verify_turnstile(str(payload.get("turnstile_token") or ""), _request_ip(request)):
        raise HTTPException(status_code=400, detail="Human verification expired. Please try again.")
    now = datetime.now(timezone.utc)
    challenge_id = uuid.uuid4()
    code = new_code()
    ip_hash = hash_request_value(_request_ip(request))
    agent_hash = hash_request_value(request.headers.get("user-agent"))

    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (742310030,))
        daily_limit = int(os.getenv("EMAIL_AUTH_DAILY_SEND_LIMIT", "100"))
        if not 1 <= daily_limit <= 10000:
            raise HTTPException(status_code=503, detail="Email budget configuration is invalid")
        cur.execute("SELECT count(*) AS count FROM starrlabs_email_login_challenges WHERE created_at >= date_trunc('day', now())")
        if int(cur.fetchone()["count"]) >= daily_limit:
            raise HTTPException(status_code=429, detail="The daily email budget is exhausted")
        cur.execute(
            "SELECT count(*) AS count FROM starrlabs_email_login_challenges "
            "WHERE identity_email=%s AND created_at >= now() - interval '1 hour'",
            (email,),
        )
        if int(cur.fetchone()["count"]) >= 5:
            raise HTTPException(status_code=429, detail="Too many codes requested. Try again later.")
        cur.execute(
            "SELECT count(*) AS count FROM starrlabs_email_login_challenges "
            "WHERE requested_ip_hash=%s AND created_at >= now() - interval '1 hour'",
            (ip_hash,),
        )
        if int(cur.fetchone()["count"]) >= 20:
            raise HTTPException(status_code=429, detail="Too many codes requested from this connection.")
        cur.execute(
            "SELECT created_at FROM starrlabs_email_login_challenges "
            "WHERE identity_email=%s ORDER BY created_at DESC LIMIT 1",
            (email,),
        )
        latest = cur.fetchone()
        if latest and (now - latest["created_at"]).total_seconds() < 60:
            raise HTTPException(status_code=429, detail="Wait one minute before requesting another code.")
        cur.execute(
            "UPDATE starrlabs_email_login_challenges SET status='cancelled' "
            "WHERE identity_email=%s AND status='pending'",
            (email,),
        )
        cur.execute(
            """
            INSERT INTO starrlabs_email_login_challenges(
              id, identity_email, code_digest, next_path, requested_ip_hash,
              user_agent_hash, expires_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (str(challenge_id), email, code_digest(challenge_id, code), destination,
             ip_hash, agent_hash, challenge_expiry()),
        )
        _auth_audit(cur, "code_requested", email, str(challenge_id), ip_hash=ip_hash)

    try:
        delivery_id = send_login_email(email, code)
    except RuntimeError as exc:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE starrlabs_email_login_challenges SET status='delivery_failed' WHERE id=%s",
                (str(challenge_id),),
            )
            _auth_audit(cur, "code_delivery_failed", email, str(challenge_id), ip_hash=ip_hash,
                        details={"error": str(exc)[:160]})
        raise HTTPException(status_code=503, detail="Email delivery is temporarily unavailable. Try again shortly.")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE starrlabs_email_login_challenges SET delivery_id=%s WHERE id=%s",
            (delivery_id, str(challenge_id)),
        )
    return JSONResponse(
        {"ok": True, "challenge_id": str(challenge_id), "expires_in": 600},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/auth/email/verify")
async def email_login_verify(request: Request):
    _require_same_site(request)
    payload = await request.json()
    try:
        challenge_id = uuid.UUID(str(payload.get("challenge_id") or ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid login challenge.")
    code = re.sub(r"\D", "", str(payload.get("code") or ""))
    if len(code) != 6:
        raise HTTPException(status_code=400, detail="Enter the six-digit code.")
    ip_hash = hash_request_value(_request_ip(request))
    raw_token = new_session_token()
    session_id = uuid.uuid4()
    expires_at = session_expiry()

    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM starrlabs_email_login_challenges WHERE id=%s FOR UPDATE",
            (str(challenge_id),),
        )
        challenge = cur.fetchone()
        if not challenge or challenge["status"] != "pending":
            raise HTTPException(status_code=400, detail="This login code is no longer active.")
        if challenge["expires_at"] <= datetime.now(timezone.utc):
            cur.execute("UPDATE starrlabs_email_login_challenges SET status='expired' WHERE id=%s", (str(challenge_id),))
            conn.commit()
            raise HTTPException(status_code=400, detail="This login code expired. Request a new one.")
        if challenge["attempts"] >= MAX_CODE_ATTEMPTS:
            cur.execute("UPDATE starrlabs_email_login_challenges SET status='cancelled' WHERE id=%s", (str(challenge_id),))
            conn.commit()
            raise HTTPException(status_code=429, detail="Too many incorrect attempts. Request a new code.")
        if not secrets.compare_digest(challenge["code_digest"], code_digest(challenge_id, code)):
            attempts = int(challenge["attempts"]) + 1
            status = "cancelled" if attempts >= MAX_CODE_ATTEMPTS else "pending"
            cur.execute(
                "UPDATE starrlabs_email_login_challenges SET attempts=%s,status=%s WHERE id=%s",
                (attempts, status, str(challenge_id)),
            )
            _auth_audit(cur, "code_rejected", challenge["identity_email"], str(challenge_id), ip_hash=ip_hash,
                        details={"attempt": attempts})
            conn.commit()
            if attempts >= MAX_CODE_ATTEMPTS:
                raise HTTPException(status_code=429, detail="Too many incorrect attempts. Request a new code.")
            raise HTTPException(status_code=400, detail="That code is incorrect.")

        cur.execute(
            "UPDATE starrlabs_email_login_challenges SET status='verified',consumed_at=now() WHERE id=%s",
            (str(challenge_id),),
        )
        cur.execute(
            """
            INSERT INTO starrlabs_identity_sessions(
              id, token_digest, identity_email, expires_at, created_ip_hash, user_agent_hash
            ) VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (str(session_id), token_digest(raw_token), challenge["identity_email"], expires_at,
             ip_hash, hash_request_value(request.headers.get("user-agent"))),
        )
        _auth_audit(cur, "email_verified", challenge["identity_email"], str(challenge_id),
                    str(session_id), ip_hash)
        _auth_audit(cur, "session_created", challenge["identity_email"], str(challenge_id),
                    str(session_id), ip_hash)
        destination = safe_next_path(challenge["next_path"])

    response = JSONResponse(
        {"ok": True, "next": destination},
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        EMAIL_AUTH_COOKIE,
        raw_token,
        max_age=SESSION_TTL_DAYS * 86400,
        expires=expires_at,
        httponly=True,
        secure=os.getenv("SESSION_HTTPS_ONLY", "0") == "1",
        samesite="lax",
        path="/",
    )
    return response




# ---------- UI Routes ----------
@app.get("/")
def index(request: Request, registry: bool = False):
    # A verified returning user should resume at the player they last used.
    # The explicit registry flag keeps player management one click away and
    # prevents the registry link from redirecting straight back to Profile.
    if not registry and not request.session.get("choose_verified_account"):
        remembered = _session_owner_tag(request)
        identity_email = _identity_email(request)
        if not remembered and identity_email and not request.session.get("suppress_identity_restore"):
            conn = get_conn()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            try:
                cur.execute(
                    """
                    SELECT player_tag
                    FROM player_verifications
                    WHERE identity_email=%s AND status='verified'
                    ORDER BY is_primary DESC, last_used_at DESC NULLS LAST, last_verified_at DESC
                    LIMIT 1
                    """,
                    (identity_email,),
                )
                linked = cur.fetchone()
                remembered = linked["player_tag"] if linked else None
                if remembered:
                    request.session["owner_tag"] = remembered
            finally:
                cur.close()
                conn.close()
        if remembered:
            return RedirectResponse(f"/player/{remembered.lstrip('#')}", status_code=303)
    # The new index.html is JS-driven and calls /api/me, /api/lists, etc.
    # We intentionally do NOT server-render other players for unrecognized users.
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "players": [],  # keep template var available (even if unused)
        },
    )


@app.get("/us")
def us_page(request: Request):
    return templates.TemplateResponse(request, "us.html", {"active_page": "us"})


EVENT_TIMEZONE = ZoneInfo("America/Chicago")


def _event_dashboard_data(selected_date: date) -> dict:
    day_start_local = datetime.combine(selected_date, dt.time.min, tzinfo=EVENT_TIMEZONE)
    day_end_local = day_start_local + timedelta(days=1)
    day_start = day_start_local.astimezone(timezone.utc)
    day_end = day_end_local.astimezone(timezone.utc)
    lookback_start = day_start - timedelta(days=3)
    event_stages = event_stages_for_date(selected_date)
    event_maps = tuple(stage["map"] for stage in event_stages)

    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT battletime_ts, map, mode, result, brawler_name,
                   player_tag, player_name
            FROM battles
            WHERE battletime_ts >= %s
              AND battletime_ts < %s
              AND map = ANY(%s)
              AND player_tag = observed_from_tag
              AND result IN ('victory', 'defeat', 'draw')
            ORDER BY player_tag, battletime_ts
            """,
            (lookback_start, day_end, list(event_maps)),
        )
        rows = cur.fetchall() or []
        cur.execute(
            """
            SELECT DISTINCT ON (b.map) b.map AS name, m.image_url
            FROM battles b
            LEFT JOIN maps m ON m.id = b.map_id
            WHERE b.map = ANY(%s)
            ORDER BY b.map, b.battletime_ts DESC
            """,
            (list(event_maps),),
        )
        map_metadata = {row["name"]: row for row in (cur.fetchall() or [])}
        cur.execute("SELECT id, name, emoji_url FROM brawlers")
        brawler_metadata = {
            str(row["name"] or "").strip().upper(): {
                **dict(row),
                "portrait_url": f"https://cdn.brawlify.com/brawlers/portraits/{row['id']}.png",
            }
            for row in (cur.fetchall() or [])
            if row.get("id") is not None and str(row.get("name") or "").strip()
        }

    dashboard = build_event_dashboard(
        rows,
        day_start=day_start,
        day_end=day_end,
        map_metadata=map_metadata,
        brawler_metadata=brawler_metadata,
        event_stages=event_stages,
    )
    if dashboard["latest_observation"]:
        dashboard["latest_observation"] = dashboard["latest_observation"].astimezone(EVENT_TIMEZONE)
    dashboard.update(
        {
            "selected_date": selected_date,
            "selected_date_iso": selected_date.isoformat(),
            "is_today": selected_date == datetime.now(EVENT_TIMEZONE).date(),
            "previous_date": (selected_date - timedelta(days=1)).isoformat(),
            "next_date": (selected_date + timedelta(days=1)).isoformat(),
            "today_date": datetime.now(EVENT_TIMEZONE).date().isoformat(),
            "timezone_label": "Central Time",
        }
    )
    return dashboard


@app.get("/event")
def event_dashboard(request: Request, event_date: str | None = Query(default=None, alias="date")):
    today = datetime.now(EVENT_TIMEZONE).date()
    try:
        selected_date = date.fromisoformat(event_date) if event_date else today
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Use a date in YYYY-MM-DD format.") from exc
    if selected_date > today:
        raise HTTPException(status_code=400, detail="Future event dates are not available.")
    if selected_date < today - timedelta(days=30):
        raise HTTPException(status_code=400, detail="Event history is limited to the last 30 days.")

    response = templates.TemplateResponse(
        request,
        "event.html",
        {
            "active_page": "event",
            "event": _event_dashboard_data(selected_date),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------- Lists + Identity APIs ----------
@app.get("/api/me")
def api_me(request: Request):
    identity_email = _identity_email(request)
    empty = {"ok": True, "recognized": False, "owner_tag": None, "selected_list_id": None, "verified_player": False, "identity_email": identity_email, "verified_accounts": []}
    if not identity_email:
        return JSONResponse(empty, headers={"Cache-Control": "no-store"})
    owner_tag = _session_owner_tag(request)
    selected = _session_selected_list_id(request)
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT v.player_tag, v.is_primary, v.last_used_at, v.last_verified_at, p.name, p.icon_id, p.trophies FROM player_verifications v JOIN player_profiles p ON p.tag=v.player_tag WHERE v.identity_email=%s AND v.status='verified' AND v.revoked_at IS NULL ORDER BY v.is_primary DESC, v.last_verified_at DESC", (identity_email,))
        accounts = cur.fetchall() or []
        empty["verified_accounts"] = accounts
        if not owner_tag and accounts and not request.session.get("suppress_identity_restore"):
            owner_tag = accounts[0]["player_tag"]
            request.session["owner_tag"] = owner_tag
        if not owner_tag or not any(a["player_tag"] == owner_tag for a in accounts):
            if owner_tag:
                empty.update({"verification_required": True, "verification_url": "/verify-player/" + owner_tag.lstrip("#")})
            return JSONResponse(jsonable_encoder(empty), headers={"Cache-Control": "no-store"})
        if selected and not _list_accessible_to(cur, int(selected), owner_tag, identity_email):
            selected = None
        if not selected:
            selected = _get_best_selected_list(cur, owner_tag)
    return JSONResponse(jsonable_encoder({"ok": True,"recognized":True,"owner_tag":owner_tag,"selected_list_id":int(selected) if selected is not None else None,"verified_player":True,"identity_email":identity_email,"verified_accounts":accounts}),headers={"Cache-Control":"no-store"})


@app.post("/identify")
def identify(request: Request, tag: str = Form(...)):
    from request_budget import admit_request
    admit_request(get_conn, _identity_email(request, required=True), "lookup", 12, 120)
    owner_tag = normalize_tag(tag)
    if not re.fullmatch(r"#[0289PYLQGRJCUV]+", owner_tag or ""):
        return JSONResponse({"ok": False, "message": "Enter a valid Brawl Stars player tag."}, status_code=400)

    try:
        live_player = verification_api_player(owner_tag)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        message = "Player not found. Check the tag and try again." if status == 404 else "Brawl Stars is not responding. Try again shortly."
        return JSONResponse({"ok": False, "message": message}, status_code=404 if status == 404 else 503)
    except Exception:
        return JSONResponse({"ok": False, "message": "Brawl Stars is not responding. Try again shortly."}, status_code=503)

    preview = {
        "owner_tag": owner_tag,
        "player_name": live_player.get("name") or owner_tag,
        "icon_id": (live_player.get("icon") or {}).get("id"),
        "trophies": live_player.get("trophies"),
        "highest_trophies": live_player.get("highestTrophies"),
        "exp_level": live_player.get("expLevel"),
    }
    request.session["pending_identity"] = {**preview, "expires_at": int(time.time()) + 300}
    return JSONResponse({"ok": True, "confirmation_required": True, **preview})


@app.post("/identify/confirm")
def confirm_identity(request: Request):
    identity_email = _identity_email(request, required=True)
    pending = request.session.get("pending_identity") or {}
    owner_tag = normalize_tag(str(pending.get("owner_tag") or ""))
    if not owner_tag or int(pending.get("expires_at") or 0) < int(time.time()):
        request.session.pop("pending_identity", None)
        return JSONResponse({"ok": False, "message": "That profile confirmation expired. Search again."}, status_code=400)
    selected = None
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Selection is browsing-only. It never establishes an ownership relationship.
        cur.execute("INSERT INTO player_profiles (tag,name,icon_id,trophies,highest_trophies,exp_level) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(tag) DO UPDATE SET name=EXCLUDED.name,icon_id=EXCLUDED.icon_id,trophies=EXCLUDED.trophies,highest_trophies=EXCLUDED.highest_trophies,exp_level=EXCLUDED.exp_level,last_update=now()", (owner_tag,pending.get("player_name") or owner_tag,pending.get("icon_id"),pending.get("trophies"),pending.get("highest_trophies"),pending.get("exp_level")))
        verification = _verified_player(cur, owner_tag)
        if verification and verification["identity_email"] == identity_email:
            selected = _ensure_default_list(cur, owner_tag)
            _ensure_system_club_list(cur, owner_tag)
            cur.execute("INSERT INTO player_list_members(list_id,player_tag,is_removed) VALUES (%s,%s,FALSE) ON CONFLICT(list_id,player_tag) DO UPDATE SET is_removed=FALSE,removed_at=NULL", (int(selected),owner_tag))
    request.session["owner_tag"] = owner_tag
    if selected is not None:
        request.session["selected_list_id"] = int(selected)
    else:
        request.session.pop("selected_list_id", None)
    request.session.pop("pending_identity", None)
    request.session.pop("suppress_identity_restore", None)
    return JSONResponse({"ok": True,"owner_tag":owner_tag,"player_name":pending.get("player_name") or owner_tag,"icon_id":pending.get("icon_id"),"selected_list_id":int(selected) if selected is not None else None,"ownership_verified":selected is not None,"verification_url":"/verify-player/"+owner_tag.lstrip("#")},headers={"Cache-Control":"no-store"})

@app.post("/logout")
def logout(request: Request):
    raw_token = request.cookies.get(EMAIL_AUTH_COOKIE)
    if raw_token:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE starrlabs_identity_sessions
                SET revoked_at=now()
                WHERE token_digest=%s AND revoked_at IS NULL
                RETURNING id, identity_email
                """,
                (token_digest(raw_token),),
            )
            revoked = cur.fetchone()
            if revoked:
                _auth_audit(cur, "session_revoked", revoked["identity_email"],
                            session_id=str(revoked["id"]),
                            ip_hash=hash_request_value(_request_ip(request)))
    request.session.pop("owner_tag", None)
    request.session.pop("selected_list_id", None)
    request.session.pop("pending_identity", None)
    request.session.pop("choose_verified_account", None)
    request.session["suppress_identity_restore"] = True

    response = JSONResponse({"ok": True})
    response.delete_cookie(EMAIL_AUTH_COOKIE, path="/", secure=os.getenv("SESSION_HTTPS_ONLY", "0") == "1", samesite="lax")
    return response


@app.post("/switch-player")
def switch_player(request: Request):
    """Leave the active profile selected while keeping the verified account chooser available."""
    request.session.pop("owner_tag", None)
    request.session.pop("selected_list_id", None)
    request.session.pop("pending_identity", None)
    request.session["choose_verified_account"] = True
    request.session.pop("suppress_identity_restore", None)
    return JSONResponse({"ok": True})


@app.post("/api/accounts/switch")
async def switch_verified_account(request: Request):
    identity_email = _identity_email(request, required=True)
    payload = await request.json()
    player_tag = normalize_tag(str(payload.get("player_tag") or ""))
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            """
            SELECT player_tag
            FROM player_verifications
            WHERE player_tag = %s AND identity_email = %s AND status = 'verified'
            """,
            (player_tag, identity_email),
        )
        if not cur.fetchone():
            return JSONResponse({"ok": False, "message": "Verified account not found."}, status_code=404)
        cur.execute(
            "UPDATE player_verifications SET last_used_at = now() WHERE player_tag = %s",
            (player_tag,),
        )
        _audit_account_action(cur, player_tag, identity_email, "account_used")
        cur.execute(
            "SELECT id FROM player_lists WHERE identity_email = %s AND system_key = 'my_accounts' AND is_deleted = FALSE LIMIT 1",
            (identity_email,),
        )
        my_accounts = cur.fetchone()
        request.session["owner_tag"] = player_tag
        if my_accounts:
            request.session["selected_list_id"] = int(my_accounts["id"])
        else:
            request.session.pop("selected_list_id", None)
        request.session.pop("suppress_identity_restore", None)
        request.session.pop("choose_verified_account", None)
        conn.commit()
        return JSONResponse({"ok": True, "owner_tag": player_tag})
    finally:
        cur.close()
        conn.close()


@app.post("/api/accounts/primary")
async def set_primary_verified_account(request: Request):
    identity_email = _identity_email(request, required=True)
    payload = await request.json()
    player_tag = normalize_tag(str(payload.get("player_tag") or ""))
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            """
            SELECT player_tag
            FROM player_verifications
            WHERE identity_email = %s AND status = 'verified'
            FOR UPDATE
            """,
            (identity_email,),
        )
        accounts = {row["player_tag"] for row in (cur.fetchall() or [])}
        if player_tag not in accounts:
            return JSONResponse({"ok": False, "message": "Verified account not found."}, status_code=404)
        cur.execute(
            """
            UPDATE player_verifications
            SET is_primary = FALSE
            WHERE identity_email = %s AND status = 'verified' AND is_primary = TRUE
            """,
            (identity_email,),
        )
        cur.execute(
            """
            UPDATE player_verifications
            SET is_primary = TRUE
            WHERE player_tag = %s AND identity_email = %s AND status = 'verified'
            """,
            (player_tag, identity_email),
        )
        _audit_account_action(cur, player_tag, identity_email, "primary_changed")
        conn.commit()
        return JSONResponse({"ok": True, "primary_player_tag": player_tag})
    finally:
        cur.close()
        conn.close()


@app.post("/api/accounts/unlink")
async def unlink_verified_account(request: Request):
    identity_email = _identity_email(request, required=True)
    payload = await request.json()
    player_tag = normalize_tag(str(payload.get("player_tag") or ""))
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            """
            SELECT player_tag, is_primary
            FROM player_verifications
            WHERE player_tag = %s AND identity_email = %s AND status = 'verified'
            FOR UPDATE
            """,
            (player_tag, identity_email),
        )
        account = cur.fetchone()
        if not account:
            return JSONResponse({"ok": False, "message": "Verified account not found."}, status_code=404)
        cur.execute(
            """
            UPDATE player_verifications
            SET status = 'revoked', is_primary = FALSE, revoked_at = now(), revoked_reason = 'user_unlinked'
            WHERE player_tag = %s
            """,
            (player_tag,),
        )
        _audit_account_action(cur, player_tag, identity_email, "revoked", details={"reason": "user_unlinked"})
        cur.execute(
            """
            UPDATE player_list_members membership
            SET is_removed = TRUE, removed_at = now()
            FROM player_lists list
            WHERE membership.list_id = list.id
              AND list.identity_email = %s AND list.system_key = 'my_accounts'
              AND membership.player_tag = %s
            """,
            (identity_email, player_tag),
        )
        cur.execute(
            """
            SELECT player_tag
            FROM player_verifications
            WHERE identity_email = %s AND status = 'verified'
            ORDER BY last_used_at DESC NULLS LAST, last_verified_at DESC
            LIMIT 1
            """,
            (identity_email,),
        )
        replacement = cur.fetchone()
        if account["is_primary"] and replacement:
            cur.execute("UPDATE player_verifications SET is_primary = TRUE WHERE player_tag = %s", (replacement["player_tag"],))
        if _session_owner_tag(request) == player_tag:
            if replacement:
                request.session["owner_tag"] = replacement["player_tag"]
                request.session.pop("selected_list_id", None)
            else:
                request.session.pop("owner_tag", None)
                request.session.pop("selected_list_id", None)
                request.session["suppress_identity_restore"] = True
        conn.commit()
        return JSONResponse({"ok": True, "owner_tag": replacement["player_tag"] if replacement else None})
    finally:
        cur.close()
        conn.close()


@app.get("/api/accounts/audit")
def account_audit(request: Request, limit: int = Query(50, ge=1, le=100)):
    identity_email = _identity_email(request, required=True)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            """
            SELECT player_tag, action, details, created_at
            FROM player_account_audit
            WHERE actor_identity_email = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (identity_email, limit),
        )
        return JSONResponse({"ok": True, "events": jsonable_encoder(cur.fetchall() or [])}, headers={"Cache-Control": "no-store"})
    finally:
        cur.close()
        conn.close()

@app.get("/api/lists")
def api_lists(request: Request):
    owner_tag = _session_owner_tag(request)
    if not owner_tag:
        return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            """
            SELECT
            id,
            name,
            owner_tag,
            is_favorite,
            updated_at,
            is_system,
            system_key,
            is_deletable,
            share_token,
            is_public,
            (
                owner_tag = %s
                AND COALESCE(is_system, FALSE) = FALSE
                AND COALESCE(is_deletable, TRUE) = TRUE
            ) AS can_edit
            FROM player_lists
            WHERE is_deleted = FALSE
              AND (owner_tag = %s OR (system_key = 'my_accounts' AND identity_email = %s))
            ORDER BY is_favorite DESC, updated_at DESC, id DESC;
            """,
            (owner_tag, owner_tag, _identity_email(request)),
        )
        lists = cur.fetchall() or []

        selected = _session_selected_list_id(request)
        if selected:
            selected = int(selected)
            if not any(int(x["id"]) == selected for x in lists):
                selected = None

        if not selected:
            selected = lists[0]["id"] if lists else None
            if selected is not None:
                request.session["selected_list_id"] = int(selected)

        payload = {"ok": True, "lists": lists, "selected_list_id": int(selected) if selected is not None else None}
        return JSONResponse(content=jsonable_encoder(payload))

    finally:
        cur.close()
        conn.close()


@app.post("/api/lists")
async def api_create_list(request: Request):
    owner_tag = _session_owner_tag(request)
    if not owner_tag:
        return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

    payload = await request.json()
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "message": "Missing name."}, status_code=400)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            """
            INSERT INTO player_lists (owner_tag, name, is_favorite, is_deleted, is_system, is_deletable)
            VALUES (%s, %s, FALSE, FALSE, FALSE, TRUE)
            RETURNING id, name, is_favorite, updated_at;
            """,
            (owner_tag, name),
        )
        row = cur.fetchone()
        request.session["selected_list_id"] = int(row["id"])
        conn.commit()
        return JSONResponse(
            content=jsonable_encoder({"ok": True, "list": row, "selected_list_id": int(row["id"])})
        )
    finally:
        cur.close()
        conn.close()


@app.post("/api/lists/select")
async def api_lists_select(request: Request):
    owner_tag = _session_owner_tag(request)
    if not owner_tag:
        return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

    payload = await request.json()
    list_id = payload.get("list_id")
    if not list_id:
        return JSONResponse({"ok": False, "message": "Missing list_id."}, status_code=400)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if not _list_accessible_to(cur, int(list_id), owner_tag, _identity_email(request)):
            return JSONResponse({"ok": False, "message": "List not found."}, status_code=404)

        request.session["selected_list_id"] = int(list_id)
        # touch list updated_at as "most recent"
        cur.execute("UPDATE player_lists SET updated_at = now() WHERE id = %s;", (int(list_id),))
        conn.commit()
        return JSONResponse({"ok": True, "selected_list_id": int(list_id)})
    finally:
        cur.close()
        conn.close()


@app.post("/api/lists/favorite")
async def api_lists_favorite(request: Request):
    owner_tag = _session_owner_tag(request)
    if not owner_tag:
        return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

    payload = await request.json()
    list_id = payload.get("list_id")
    if not list_id:
        return JSONResponse({"ok": False, "message": "Missing list_id."}, status_code=400)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if not _list_belongs_to_owner(cur, int(list_id), owner_tag):
            return JSONResponse({"ok": False, "message": "List not found."}, status_code=404)
        cur.execute("SELECT system_key FROM player_lists WHERE id = %s", (int(list_id),))
        if (cur.fetchone() or {}).get("system_key") == "my_accounts":
            return JSONResponse({"ok": False, "message": "Remove verified accounts from your profile."}, status_code=400)

        # toggle
        cur.execute("SELECT is_favorite FROM player_lists WHERE id = %s;", (int(list_id),))
        row = cur.fetchone()
        is_fav = bool(row["is_favorite"]) if row else False
        new_val = not is_fav

        # enforce a single favorite per owner (recommended)
        if new_val:
            cur.execute(
                """
                UPDATE player_lists
                SET is_favorite = FALSE
                WHERE owner_tag = %s AND is_deleted = FALSE;
                """,
                (owner_tag,),
            )

        cur.execute(
            """
            UPDATE player_lists
            SET is_favorite = %s, updated_at = now()
            WHERE id = %s;
            """,
            (new_val, int(list_id)),
        )
        conn.commit()
        return JSONResponse({"ok": True, "list_id": int(list_id), "is_favorite": new_val})
    finally:
        cur.close()
        conn.close()


@app.post("/api/lists/delete")
async def api_lists_delete(request: Request):
    owner_tag = _session_owner_tag(request)
    if not owner_tag:
        return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

    payload = await request.json()
    list_id = payload.get("list_id")
    if not list_id:
        return JSONResponse({"ok": False, "message": "Missing list_id."}, status_code=400)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if not _list_belongs_to_owner(cur, int(list_id), owner_tag):
            return JSONResponse({"ok": False, "message": "List not found."}, status_code=404)

        # Prevent deleting system/non-deletable lists
        cur.execute("SELECT is_deletable, is_system FROM player_lists WHERE id = %s;", (int(list_id),))
        meta = cur.fetchone() or {}
        if meta.get("is_deletable") is False or meta.get("is_system") is True:
            return JSONResponse({"ok": False, "message": "This list cannot be deleted."}, status_code=400)

        # System lists cannot be deleted
        cur.execute("SELECT is_deletable FROM player_lists WHERE id = %s;", (int(list_id),))
        row = cur.fetchone() or {}
        if row.get("is_deletable") is False:
            return JSONResponse({"ok": False, "message": "This list cannot be deleted."}, status_code=400)

        cur.execute(
            """
            UPDATE player_lists
            SET is_deleted = TRUE, updated_at = now()
            WHERE id = %s;
            """,
            (int(list_id),),
        )

        # If deleting selected list, pick a new one
        selected = _session_selected_list_id(request)
        if selected and int(selected) == int(list_id):
            new_selected = _get_best_selected_list(cur, owner_tag)
            request.session["selected_list_id"] = int(new_selected) if new_selected else None

        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        cur.close()
        conn.close()


@app.post("/api/lists/remove-player")
async def api_lists_remove_player(request: Request):
    owner_tag = _session_owner_tag(request)
    if not owner_tag:
        return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

    payload = await request.json()
    list_id = payload.get("list_id")
    player_tag = normalize_tag(payload.get("player_tag"))
    if not list_id or not player_tag:
        return JSONResponse({"ok": False, "message": "Missing list_id or player_tag."}, status_code=400)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if not _list_belongs_to_owner(cur, int(list_id), owner_tag):
            return JSONResponse({"ok": False, "message": "List not found."}, status_code=404)
        cur.execute("SELECT system_key FROM player_lists WHERE id = %s", (int(list_id),))
        if (cur.fetchone() or {}).get("system_key") == "my_accounts":
            return JSONResponse({"ok": False, "message": "Remove verified accounts from your profile."}, status_code=400)

        # soft remove from UI only
        cur.execute(
            """
            UPDATE player_list_members
            SET is_removed = TRUE, removed_at = now()
            WHERE list_id = %s AND player_tag = %s;
            """,
            (int(list_id), player_tag),
        )
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        cur.close()
        conn.close()


@app.get("/api/lists/{list_id}/players")
def api_list_players(list_id: int, request: Request):

    owner_tag = _session_owner_tag(request)
    if not owner_tag:
        return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    seven_days_ago = (dt.datetime.utcnow().date() - dt.timedelta(days=7)).strftime("%Y%m%d")

    try:
        if not _list_accessible_to(cur, int(list_id), owner_tag, _identity_email(request)):
            return JSONResponse({"ok": False, "message": "List not found."}, status_code=404)

        cur.execute(
            """
            SELECT
              p.tag,
              p.name,
              p.icon_id,
              p.trophies,
              p.highest_trophies,
              p.exp_level,
              p.total_prestige_level,
              p.ranked_rank AS ranked_level,
              p.ranked_rank_name,
              p.highest_all_time_ranked_rank AS highest_ranked_level,
              p.highest_all_time_ranked_rank_name AS highest_ranked_rank_name,
              p.favorite_brawler_name,
              p.favorite_brawler_trophies,
              p.first_seen,
              p.last_update,
              EXISTS (
                SELECT 1 FROM player_verifications pv
                WHERE pv.player_tag = p.tag AND pv.status = 'verified'
              ) AS verified_player,
              EXISTS (
                SELECT 1 FROM player_verifications pv
                WHERE pv.player_tag = p.tag
                  AND pv.identity_email = %s
                  AND pv.status = 'verified'
                  AND pv.is_primary = TRUE
              ) AS is_primary_account,
              b.emoji_url AS favorite_brawler_emoji_url,
              w7.matches_7d,
                w7.wins_7d,
                w7.losses_7d,
                mu7.most_used_brawler_name,
                mu7.most_used_brawler_emoji_url,
                mu7.most_used_brawler_matches_7d,


              CASE
                WHEN p.ranked_rank IS NOT NULL AND p.ranked_rank > 0
                THEN 'https://cdn.brawlify.com/ranked/tiered/' || (58000000 + p.ranked_rank - 1) || '.png'
                ELSE NULL
              END AS ranked_icon_url,

              CASE
                WHEN p.highest_all_time_ranked_rank IS NOT NULL AND p.highest_all_time_ranked_rank > 0
                THEN 'https://cdn.brawlify.com/ranked/tiered/' || (58000000 + p.highest_all_time_ranked_rank - 1) || '.png'
                ELSE NULL
              END AS highest_ranked_icon_url,

              r.victory_score,
              r.updated_at AS victory_updated_at,

              COALESCE(pba.brawler_count, 0) AS brawlers_owned,
              bt.total_brawlers AS brawlers_total

            FROM player_list_members m
            JOIN player_lists l
              ON l.id = m.list_id
             AND l.is_deleted = FALSE

            JOIN player_profiles p
              ON p.tag = m.player_tag

            LEFT JOIN LATERAL (
              SELECT COUNT(*)::int AS brawler_count
              FROM player_brawlers pb
              WHERE pb.player_tag = p.tag
            ) pba ON TRUE

            CROSS JOIN LATERAL (
              SELECT COUNT(*)::int AS total_brawlers
              FROM brawlers
            ) bt

            LEFT JOIN brawlers b
              ON LOWER(b.name) = LOWER(p.favorite_brawler_name)

            LEFT JOIN LATERAL (
            SELECT
                COUNT(*)::int AS matches_7d,
                COUNT(*) FILTER (WHERE b.result = 'victory')::int AS wins_7d,
                COUNT(*) FILTER (WHERE b.result = 'defeat')::int  AS losses_7d
            FROM battles b
            WHERE b.player_tag = p.tag
                AND LEFT(b.battleTime, 8) >= %s
            ) w7 ON TRUE

            LEFT JOIN LATERAL (
            SELECT
                b.brawler_name AS most_used_brawler_name,
                br.emoji_url   AS most_used_brawler_emoji_url,
                COUNT(*)::int  AS most_used_brawler_matches_7d
            FROM battles b
            LEFT JOIN brawlers br
                ON LOWER(br.name) = LOWER(b.brawler_name)
            WHERE b.player_tag = p.tag
                AND b.brawler_name IS NOT NULL
                AND LEFT(b.battleTime, 8) >= %s
            GROUP BY b.brawler_name, br.emoji_url
            ORDER BY COUNT(*) DESC
            LIMIT 1
            ) mu7 ON TRUE

            LEFT JOIN player_victory_rollups r
              ON r.player_tag = p.tag
             AND r.window_days = %s
             AND r.mode = %s
             AND r.type = %s
             AND r.brawler_name = %s

            WHERE m.list_id = %s
              AND m.is_removed = FALSE
            ORDER BY is_primary_account DESC, p.trophies DESC NULLS LAST, p.tag ASC;
            """,
            (_identity_email(request), seven_days_ago, seven_days_ago, VICTORY_WINDOW_DAYS, ALL_DIM, ALL_DIM, ALL_DIM, int(list_id)),
        )
        players = cur.fetchall() or []
        _add_victory_score_windows(cur, players)

        for p in players:
            owned = p.get("brawlers_owned")
            total = p.get("brawlers_total")
            if owned is None or total is None:
                p["brawlers_owned_total"] = "-"
            else:
                p["brawlers_owned_total"] = f"{owned} / {total}"

        for p in players:
            fs = p.get("first_seen")
            if fs:
                try:
                    if getattr(fs, "tzinfo", None) is None:
                        fs = fs.replace(tzinfo=timezone.utc)
                    p["tracked_since_human"] = fs.strftime("%b %d, %Y")
                    p["tracked_since_tooltip"] = fs.strftime("%Y-%m-%d %H:%M %Z")
                except Exception:
                    p["tracked_since_human"] = str(fs)
                    p["tracked_since_tooltip"] = str(fs)
            else:
                p["tracked_since_human"] = "-"
                p["tracked_since_tooltip"] = "-"

            tier, label = victory_tier_and_label(p.get("victory_score"))
            p["victory_tier"] = tier
            p["victory_label"] = label

        payload = {"ok": True, "players": players}
        return JSONResponse(content=jsonable_encoder(payload))
    finally:
        cur.close()
        conn.close()


# ---------- My club (system list) ----------
@app.post("/api/my-club/select")
def api_my_club_select(request: Request):
    if os.getenv("STARRLABS_COLLECTION_ENABLED", "0") != "1":
        raise HTTPException(status_code=503, detail="Collection is disabled; enable it explicitly")
    from request_budget import admit_request
    admit_request(get_conn, _identity_email(request, required=True), "collection", 12, 60)
    if not collection_slot.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="A collection job is already running")
    try:
        owner_tag = _session_owner_tag(request)
        if not owner_tag:
            return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            # Ensure the system list exists
            club_list_id = _ensure_system_club_list(cur, owner_tag)

            # Sync club + members from Supercell API
            out = sync_club_for_owner(owner_tag)
            if not out.get("ok"):
                return JSONResponse({"ok": False, "message": out.get("message") or "Could not sync club."}, status_code=400)

            member_tags = [normalize_tag(t) for t in (out.get("member_tags") or []) if t]
            # Mark existing list members as removed if they are no longer in the club
            cur.execute(
                """
                UPDATE player_list_members
                SET is_removed = TRUE,
                    removed_at = now()
                WHERE list_id = %s
                  AND is_removed = FALSE
                  AND player_tag <> ALL(%s);
                """,
                (int(club_list_id), member_tags if member_tags else ["#-"],),
            )

            # Upsert current members into list
            rows = [{"list_id": int(club_list_id), "player_tag": t} for t in member_tags]
            if rows:
                execute_batch(
                    cur,
                    """
                    INSERT INTO player_list_members (list_id, player_tag, is_removed)
                    VALUES (%(list_id)s, %(player_tag)s, FALSE)
                    ON CONFLICT (list_id, player_tag)
                    DO UPDATE SET is_removed = FALSE, removed_at = NULL;
                    """,
                    rows,
                    page_size=100,
                )

            # Make it the selected list
            request.session["selected_list_id"] = int(club_list_id)
            cur.execute("UPDATE player_lists SET updated_at = now() WHERE id = %s;", (int(club_list_id),))
            conn.commit()

            return JSONResponse(
                {
                    "ok": True,
                    "list_id": int(club_list_id),
                    "club_tag": out.get("club_tag"),
                    "club_name": out.get("club_name"),
                    "member_count": out.get("member_count"),
                }
            )
        finally:
            cur.close()
            conn.close()
    finally:
        collection_slot.release()


# ---------- Progress polling ----------
@app.get("/api/refresh-status/{job_id}")
def refresh_status(job_id: str, request: Request):
    job = refresh_jobs.get(job_id)
    if job and job.get("owner_tag") != _session_owner_tag(request):
        raise HTTPException(status_code=404, detail="Job not found")
    if not job:
        return JSONResponse({"ok": False, "message": "Unknown job."}, status_code=404)
    return JSONResponse({"ok": True, **job})


# ---------- Track/Add player (now list-aware) ----------
@app.post("/add-player")
def add_player(request: Request, tag: str = Form(...), list_id: int = Form(default=None)):
    if os.getenv("STARRLABS_COLLECTION_ENABLED", "0") != "1":
        raise HTTPException(status_code=503, detail="Collection is disabled; enable it explicitly")
    from request_budget import admit_request
    admit_request(get_conn, _identity_email(request, required=True), "collection", 12, 60)
    if not collection_slot.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="A collection job is already running")
    background_started = False
    try:
        cleaned = normalize_tag(tag)
        wants_json = "application/json" in (request.headers.get("accept") or "")

        if not is_valid_player_tag(cleaned):
            if wants_json:
                return JSONResponse(
                    {"ok": False, "message": "Enter a valid Brawl Stars player tag."},
                    status_code=400,
                )
            return RedirectResponse("/", status_code=303)

        try:
            live_player = verification_api_player(cleaned)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 502
            message = (
                "Player not found. Check the tag and try again."
                if status == 404
                else "Brawl Stars is not responding. Try again shortly."
            )
            if wants_json:
                return JSONResponse(
                    {"ok": False, "message": message},
                    status_code=404 if status == 404 else 503,
                )
            return RedirectResponse("/", status_code=303)
        except Exception:
            if wants_json:
                return JSONResponse(
                    {"ok": False, "message": "Brawl Stars is not responding. Try again shortly."},
                    status_code=503,
                )
            return RedirectResponse("/", status_code=303)

        owner_tag = _session_owner_tag(request)

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            if owner_tag:
                if list_id is None:
                    list_id = _session_selected_list_id(request) or _get_best_selected_list(cur, owner_tag)
                    if not list_id:
                        list_id = _ensure_default_list(cur, owner_tag)
                    request.session["selected_list_id"] = int(list_id)
                else:
                    if not _list_belongs_to_owner(cur, int(list_id), owner_tag):
                        return JSONResponse({"ok": False, "message": "Invalid list."}, status_code=400)
                    cur.execute("SELECT system_key FROM player_lists WHERE id = %s", (int(list_id),))
                    if (cur.fetchone() or {}).get("system_key") == "my_accounts":
                        return JSONResponse({"ok": False, "message": "Accounts can only be added by verification."}, status_code=400)

            cur.execute(
                """
                INSERT INTO player_profiles (tag, name)
                VALUES (%s, %s)
                ON CONFLICT (tag) DO NOTHING;
                """,
                (cleaned, live_player.get("name") or cleaned),
            )
            inserted = (cur.rowcount == 1)

            if owner_tag and list_id:
                cur.execute(
                    """
                    INSERT INTO player_list_members (list_id, player_tag, is_removed)
                    VALUES (%s, %s, FALSE)
                    ON CONFLICT (list_id, player_tag)
                    DO UPDATE SET is_removed = FALSE, removed_at = NULL;
                    """,
                    (int(list_id), cleaned),
                )
                cur.execute("UPDATE player_lists SET updated_at = now() WHERE id = %s;", (int(list_id),))

            conn.commit()
        finally:
            cur.close()
            conn.close()

        if not inserted:
            if wants_json:
                return JSONResponse(
                    {
                        "ok": True,
                        "needs_refresh": False,
                        "message": "Added to list ✅    ",
                        "redirect": "/",
                    }
                )
            return RedirectResponse("/", status_code=303)

        with refresh_jobs_lock:
            cutoff = time.time() - 3600
            for old_id, old in list(refresh_jobs.items()):
                if old.get("done") and old.get("created_at", 0) < cutoff:
                    refresh_jobs.pop(old_id, None)
            completed = [key for key, value in refresh_jobs.items() if value.get("done")]
            while len(refresh_jobs) >= 100 and completed:
                refresh_jobs.pop(completed.pop(0), None)
        job_id = str(uuid.uuid4())
        refresh_jobs[job_id] = {
            "owner_tag": owner_tag,
            "created_at": time.time(),
            "done": False,
            "status": "running",
            "message": "Starting…",
            "redirect": "/",
            "progress": 5,
            "step": "start",
        }

        def _run_refresh():
            try:
                refresh_jobs[job_id].update({"message": "Updating profile…", "progress": 25, "step": "profile"})
                update_profiles([cleaned])

                refresh_jobs[job_id].update({"message": "Collecting battles…", "progress": 55, "step": "battles"})
                _ = collect_battles_for_tags([cleaned]) or {"saved": 0}

                refresh_jobs[job_id].update({"message": "Saving daily snapshot…", "progress": 75, "step": "daily"})
                _ = snapshot_daily_activity([cleaned]) or {"rows_saved": 0}

                refresh_jobs[job_id].update({"message": "Saving brawler snapshot…", "progress": 90, "step": "brawler_daily"})
                _ = snapshot_brawler_daily([cleaned]) or {"rows_saved": 0, "rows_updated_from_battles": 0}

                refresh_jobs[job_id].update(
                    {"done": True, "status": "done", "message": "New player synced ✅    ", "progress": 100, "step": "done"}
                )

            except Exception as e:
                print(f"[add-player] refresh failed: {e}")
                refresh_jobs[job_id].update(
                    {
                        "done": True,
                        "status": "error",
                        "message": "Refresh failed. Check server logs.",
                        "progress": 100,
                        "step": "error",
                    }
                )

            finally:
                collection_slot.release()

        threading.Thread(target=_run_refresh, daemon=True).start()
        background_started = True

        if wants_json:
            return JSONResponse({"ok": True, "needs_refresh": True, "job_id": job_id, "redirect": "/"})

        return RedirectResponse("/", status_code=303)
    finally:
        if not background_started:
            collection_slot.release()


@app.patch("/api/lists/{list_id}")
async def api_rename_list(list_id: int, request: Request):
    owner_tag = _session_owner_tag(request)
    if not owner_tag:
        return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

    payload = await request.json()
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "message": "Missing name."}, status_code=400)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if not _list_belongs_to_owner(cur, int(list_id), owner_tag):
            return JSONResponse({"ok": False, "message": "List not found."}, status_code=404)

        cur.execute("SELECT is_system FROM player_lists WHERE id = %s;", (int(list_id),))
        row = cur.fetchone() or {}
        if row.get("is_system") is True:
            return JSONResponse({"ok": False, "message": "System lists cannot be renamed."}, status_code=400)

        cur.execute(
            """
            UPDATE player_lists
            SET name = %s, updated_at = now()
            WHERE id = %s
            RETURNING id, name, is_favorite, updated_at;
            """,
            (name, int(list_id)),
        )
        row = cur.fetchone()
        conn.commit()
        return JSONResponse(content=jsonable_encoder({"ok": True, "list": row}))
    finally:
        cur.close()
        conn.close()


# ---------- Player pages ----------
@app.get("/player/{tag}")
def player_page(tag: str, request: Request):
    cleaned = normalize_tag(tag)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """
        SELECT
          p.tag,
          p.name,
          p.icon_id,
          p.trophies,
          p.highest_trophies,
          p.exp_level,
          p."3v3_victories",
          p.solo_victories,
          p.duo_victories,
          p.favorite_brawler_name,
          p.favorite_brawler_trophies,
          p.favorite_brawler_rank_on_account,
          p.first_seen,
          p.last_update,
          b.emoji_url AS favorite_brawler_emoji_url
        FROM player_profiles p
        LEFT JOIN brawlers b
          ON LOWER(b.name) = LOWER(p.favorite_brawler_name)
        WHERE p.tag = %s;
        """,
        (cleaned,),
    )

    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Player not found (yet)")

    if row.get("first_seen"):
        try:
            fs = row["first_seen"]
            if getattr(fs, "tzinfo", None) is None:
                fs = fs.replace(tzinfo=timezone.utc)
            row["first_seen_human"] = fs.strftime("%b %d, %Y · %H:%M")
            row["first_seen_human_tooltip"] = fs.strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            row["first_seen_human"] = str(row["first_seen"])
            row["first_seen_human_tooltip"] = str(row["first_seen"])
    else:
        row["first_seen_human"] = "-"
        row["first_seen_human_tooltip"] = "-"

    weekly_delta = get_weekly_deltas(cur, cleaned)
    row["weekly_delta"] = weekly_delta
    verification = _verified_player(cur, cleaned)
    row["verified_player"] = bool(verification)
    row["verified_at"] = verification.get("verified_at") if verification else None

    cur.close()
    conn.close()

    # The profile owns the aggregate companion/prediction story. Reuse the
    # battle-history read model for now so both pages stay mathematically
    # identical while the history page remains focused on individual matches.
    battle_view = player_battles(
        cleaned,
        request,
        from_date=None,
        to_date=None,
        from_ts=None,
        to_ts=None,
        mode=None,
        btype=None,
        brawler=None,
        page=1,
    )
    battle_context = battle_view.context
    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "player": row,
            "companion_summary": battle_context["companion_summary"],
            "companion_breakdowns": battle_context["companion_breakdowns"],
            "progression_context": battle_context["progression_context"],
            "session_context": battle_context["session_context"],
            "relationship_context": battle_context["relationship_context"],
        },
    )


@app.get("/verify-player/{tag}")
def player_verification_page(tag: str, request: Request):
    cleaned = normalize_tag(tag)
    identity_email = _identity_email(request)
    if not identity_email:
        return RedirectResponse(f"/login?next=/verify-player/{cleaned.lstrip('#')}", status_code=303)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT tag, name, icon_id, trophies FROM player_profiles WHERE tag = %s", (cleaned,))
        player = cur.fetchone()
        if not player:
            raise HTTPException(status_code=404, detail="Add this player to BSA before verifying it.")
        verification = _verified_player(cur, cleaned)
        transfer_required = bool(verification and verification["identity_email"] != identity_email)
    finally:
        cur.close()
        conn.close()
    return templates.TemplateResponse(
        request,
        "player_verify.html",
        {"player": player, "identity_email": identity_email,
         "verified": bool(verification and verification["identity_email"] == identity_email),
         "transfer_required": transfer_required},
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


@app.post("/api/player-verification/start")
def player_verification_start(request: Request, payload: dict):
    from request_budget import admit_request
    admit_request(get_conn, _identity_email(request, required=True), "verification", 60, 240)
    identity_email = _identity_email(request, required=True)
    player_tag = normalize_tag(str(payload.get("player_tag") or ""))
    transfer_requested = payload.get("purpose") == "transfer"
    if not re.fullmatch(r"#[0289PYLQGRJCUV]+", player_tag or ""):
        raise HTTPException(status_code=400, detail="Enter a valid Brawl Stars player tag.")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT identity_email FROM player_verifications WHERE player_tag = %s AND status = 'verified'",
            (player_tag,),
        )
        linked = cur.fetchone()
        if linked and linked["identity_email"] != identity_email and not transfer_requested:
            raise HTTPException(
                status_code=409,
                detail="This player is already linked to another website account. Use account recovery to transfer it.",
            )
        cur.execute(
            """
            SELECT count(*) AS attempts
            FROM player_verification_challenges
            WHERE identity_email = %s AND created_at >= now() - interval '15 minutes'
            """,
            (identity_email,),
        )
        if int(cur.fetchone()["attempts"]) >= 5:
            raise HTTPException(status_code=429, detail="Too many new challenges. Wait 15 minutes and try again.")
    finally:
        cur.close()
        conn.close()
    try:
        live_player = verification_api_player(player_tag)
        tasks = build_tasks(live_player, api_rotation())
        api_battlelog(player_tag)  # prove the battle log is reachable before issuing work
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (requests.RequestException, RuntimeError):
        raise HTTPException(status_code=503, detail="Brawl Stars data is temporarily unavailable. Try again shortly.")

    challenge_id = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=PLAYER_VERIFICATION_TTL_MINUTES)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT 1 FROM player_profiles WHERE tag = %s", (player_tag,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Add this player to BSA before verifying it.")
        cur.execute(
            "UPDATE player_verification_challenges SET status = 'cancelled' WHERE identity_email = %s AND status = 'pending'",
            (identity_email,),
        )
        cur.execute(
            """
            INSERT INTO player_verification_challenges(
              id, identity_email, player_tag, tasks, expires_at, purpose, previous_identity_email
            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
            """,
            (str(challenge_id), identity_email, player_tag, json.dumps(tasks), expires_at,
             "transfer" if linked and linked["identity_email"] != identity_email else "link",
             linked["identity_email"] if linked and linked["identity_email"] != identity_email else None),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return JSONResponse(
        {"ok": True, "challenge_id": str(challenge_id), "tasks": tasks, "expires_at": expires_at.isoformat(),
         "purpose": "transfer" if linked and linked["identity_email"] != identity_email else "link"},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/player-verification/{challenge_id}")
def player_verification_status(challenge_id: uuid.UUID, request: Request):
    from request_budget import admit_request
    admit_request(get_conn, _identity_email(request, required=True), "verification", 120, 240)
    identity_email = _identity_email(request, required=True)
    now = datetime.now(timezone.utc)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "SELECT * FROM player_verification_challenges WHERE id = %s AND identity_email = %s FOR UPDATE",
            (str(challenge_id), identity_email),
        )
        challenge = cur.fetchone()
        if not challenge:
            raise HTTPException(status_code=404, detail="Verification challenge not found.")
        if challenge["status"] == "pending" and challenge["expires_at"] <= now:
            challenge["status"] = "expired"
            cur.execute("UPDATE player_verification_challenges SET status = 'expired' WHERE id = %s", (str(challenge_id),))
            conn.commit()
        if challenge["status"] != "pending":
            return JSONResponse({"ok": True, "status": challenge["status"], "completed": 2 if challenge["status"] == "verified" else 0})

        if challenge.get("last_polled_at") and (now - challenge["last_polled_at"]).total_seconds() < 3:
            cached_matches = challenge.get("matches") or []
            return JSONResponse(
                {"ok": True, "status": "pending", "completed": len(cached_matches), "matches": cached_matches},
                headers={"Cache-Control": "no-store", "Retry-After": "3"},
            )

        try:
            log = api_battlelog(challenge["player_tag"])
            completed, matches = matching_progress(
                log.get("items") or [], challenge["player_tag"], challenge["tasks"], challenge["created_at"]
            )
        except (requests.RequestException, RuntimeError):
            raise HTTPException(status_code=503, detail="Battle log is temporarily unavailable; your challenge remains active.")
        cur.execute(
            "UPDATE player_verification_challenges SET matches = %s::jsonb, last_polled_at = now() WHERE id = %s",
            (json.dumps(matches), str(challenge_id)),
        )
        if completed == 2:
            cur.execute(
                "SELECT identity_email, status FROM player_verifications WHERE player_tag = %s FOR UPDATE",
                (challenge["player_tag"],),
            )
            existing_owner = cur.fetchone()
            is_transfer = challenge.get("purpose") == "transfer"
            if existing_owner and existing_owner["status"] == "verified" and existing_owner["identity_email"] != identity_email and not is_transfer:
                raise HTTPException(status_code=409, detail="This player was linked to another website account.")
            previous_email = existing_owner["identity_email"] if existing_owner and existing_owner["identity_email"] != identity_email else None
            if previous_email:
                cur.execute(
                    """UPDATE player_verifications SET status='revoked', is_primary=FALSE,
                       revoked_at=now(), revoked_reason='ownership_transferred'
                       WHERE player_tag=%s AND identity_email=%s""",
                    (challenge["player_tag"], previous_email),
                )
            cur.execute(
                "SELECT NOT EXISTS (SELECT 1 FROM player_verifications WHERE identity_email = %s AND status = 'verified') AS first_account",
                (identity_email,),
            )
            first_account = bool(cur.fetchone()["first_account"])
            cur.execute(
                """
                INSERT INTO player_verifications(player_tag, identity_email, is_primary, last_used_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (player_tag) DO UPDATE SET
                  identity_email = EXCLUDED.identity_email,
                  status = 'verified', last_verified_at = now(), last_used_at = now(),
                  is_primary = CASE WHEN player_verifications.status = 'verified' THEN player_verifications.is_primary ELSE EXCLUDED.is_primary END,
                  revoked_at = NULL, revoked_reason = NULL
                """,
                (challenge["player_tag"], identity_email, first_account),
            )
            my_accounts_list_id = _ensure_my_accounts_list(cur, identity_email, challenge["player_tag"])
            if previous_email:
                cur.execute(
                    """UPDATE player_list_members membership SET is_removed=TRUE, removed_at=now()
                       FROM player_lists list WHERE membership.list_id=list.id
                       AND list.identity_email=%s AND list.system_key='my_accounts'
                       AND membership.player_tag=%s""",
                    (previous_email, challenge["player_tag"]),
                )
                cur.execute(
                    """
                    SELECT player_tag FROM player_verifications
                    WHERE identity_email=%s AND status='verified'
                    ORDER BY last_used_at DESC NULLS LAST, last_verified_at DESC LIMIT 1
                    """,
                    (previous_email,),
                )
                old_replacement = cur.fetchone()
                if old_replacement:
                    cur.execute(
                        "UPDATE player_verifications SET is_primary=TRUE WHERE player_tag=%s",
                        (old_replacement["player_tag"],),
                    )
            _audit_account_action(
                cur, challenge["player_tag"], identity_email,
                "transferred" if previous_email else "verified",
                previous_email=previous_email,
                details={"challenge_id": str(challenge_id), "method": "ordered_battles_v1"},
            )
            cur.execute(
                "UPDATE player_verification_challenges SET status = 'verified', completed_at = now() WHERE id = %s",
                (str(challenge_id),),
            )
            request.session["owner_tag"] = challenge["player_tag"]
            request.session["selected_list_id"] = my_accounts_list_id
            request.session.pop("suppress_identity_restore", None)
            completed_status = "verified"
        else:
            completed_status = "pending"
        conn.commit()
        return JSONResponse({"ok": True, "status": completed_status, "completed": completed, "matches": matches})
    finally:
        cur.close()
        conn.close()


# ---------- Existing JSON API endpoints ----------
@app.get("/api/players")
def api_players(limit: int = Query(50, ge=1, le=250)):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT
          tag,
          name,
          trophies,
          highest_trophies,
          exp_level,
          favorite_brawler_name,
          favorite_brawler_trophies,
          last_update
        FROM player_profiles
        ORDER BY trophies DESC NULLS LAST
        LIMIT %s;
        """,
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.get("/api/players/{tag}")
def api_player(tag: str):
    cleaned = normalize_tag(tag)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM player_profiles WHERE tag = %s;", (cleaned,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Player not found")
    return row



# ---------- Battles pages + APIs ----------
@app.get("/battles/{tag}")
def player_battles(
    tag: str,
    request: Request,
    from_date: str = Query(default=None),
    to_date: str = Query(default=None),
    from_ts: str = Query(default=None),
    to_ts: str = Query(default=None),
    mode: str = Query(default=None),     # <-- RAW battles.mode value
    btype: str = Query(default=None),
    brawler: str = Query(default=None),
    victory_score: bool = Query(default=False),
    page: int = Query(default=1, ge=1),  # ✅ NEW
):
    cleaned = normalize_tag(tag)

    PER_PAGE = 30  # ✅ pagination size
    offset = (page - 1) * PER_PAGE

    today = dt.date.today()

    try:
        from_d = dt.date.fromisoformat(from_date) if from_date else today - dt.timedelta(days=30 if victory_score else 7)
    except ValueError:
        from_d = today - dt.timedelta(days=30 if victory_score else 7)

    try:
        to_d = dt.date.fromisoformat(to_date) if to_date else today
    except ValueError:
        to_d = today

    from_prefix = from_d.strftime("%Y%m%d")
    to_prefix = (to_d + dt.timedelta(days=1)).strftime("%Y%m%d")
    use_ts = bool(from_ts and to_ts)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ✅ player icon for the hero card (adjust URL if you use different CDN)
    cur.execute("SELECT icon_id FROM player_profiles WHERE tag = %s;", (cleaned,))
    p = cur.fetchone() or {}
    icon_id = p.get("icon_id")
    player_icon_url = brawlify_profile_icon_url(icon_id)


    # -------- filters for list queries --------
    where_clauses = ["b.player_tag = %s"]
    params = [cleaned]

    if use_ts:
        where_clauses.append(
            "((b.battletime_ts >= %s AND b.battletime_ts < %s) "
            "OR (b.battletime_ts IS NULL AND b.battleTime >= %s AND b.battleTime < %s))"
        )
        params.extend([from_ts, to_ts, from_prefix, to_prefix])
    else:
        where_clauses.append("b.battleTime >= %s")
        where_clauses.append("b.battleTime < %s")
        params.extend([from_prefix, to_prefix])

    if mode:
        where_clauses.append("LOWER(b.mode) = LOWER(%s)")
        params.append(mode)

    if btype:
        where_clauses.append("LOWER(b.type) = LOWER(%s)")
        params.append(btype)

    if brawler:
        where_clauses.append("LOWER(b.brawler_name) = LOWER(%s)")
        params.append(brawler)

    if victory_score:
        # Match the exact V2 eligibility contract: a published Ladder-model
        # probability and a binary result in the rolling 30-day score window.
        where_clauses.append(
            "EXISTS (SELECT 1 FROM battle_features_3v3 vf "
            "WHERE vf.player_tag=b.player_tag "
            "AND vf.battletime=COALESCE(b.battletime_ts,(b.battletime)::timestamptz) "
            "AND vf.mode=b.mode "
            "AND vf.map_key=COALESCE(b.map_id::text,b.map) "
            "AND vf.type IS NOT DISTINCT FROM b.type "
            "AND vf.duration IS NOT DISTINCT FROM b.duration "
            "AND vf.pred_win_prob IS NOT NULL AND vf.win_label IN (0,1) "
            "AND vf.battletime >= now() - interval '30 days')"
        )

    where_sql = " AND ".join(where_clauses)

    # ✅ Mode options MUST return RAW value = b.mode (sc_hash),
    # but show label = gm.name if available
    if use_ts:
        time_filter_sql = (
            "((b.battletime_ts >= %s AND b.battletime_ts < %s) "
            "OR (b.battletime_ts IS NULL AND b.battleTime >= %s AND b.battleTime < %s))"
        )
        time_params = [from_ts, to_ts, from_prefix, to_prefix]
    else:
        time_filter_sql = "b.battleTime >= %s AND b.battleTime < %s"
        time_params = [from_prefix, to_prefix]

    cur.execute(
        f"""
        SELECT DISTINCT
          b.mode AS value,
          COALESCE(gm.name, b.mode) AS label
        FROM battles b
        LEFT JOIN game_modes gm
          ON LOWER(gm.sc_hash) = LOWER(b.mode)
        WHERE b.player_tag = %s
          AND {time_filter_sql}
          AND b.mode IS NOT NULL
        ORDER BY 2;
        """,
        [cleaned, *time_params],
    )
    mode_options = cur.fetchall() or []

    # Type options (raw)
    cur.execute(
        f"""
        SELECT DISTINCT b.type AS value, b.type AS label
        FROM battles b
        WHERE b.player_tag = %s
          AND {time_filter_sql}
          AND b.type IS NOT NULL
        ORDER BY 1;
        """,
        [cleaned, *time_params],
    )
    type_options = cur.fetchall() or []

    # Brawler options:
    # value should match what you filter on: b.brawler_name
    # label can be normalized via brawlers table
    cur.execute(
        f"""
        SELECT DISTINCT
          b.brawler_name AS value,
          COALESCE(br.name, b.brawler_name) AS label,
          br.model_url AS model_url
        FROM battles b
        LEFT JOIN brawlers br
          ON LOWER(br.name) = LOWER(b.brawler_name)
        WHERE b.player_tag = %s
          AND {time_filter_sql}
          AND b.brawler_name IS NOT NULL
        ORDER BY 2;
        """,
        [cleaned, *time_params],
    )
    brawler_rows = cur.fetchall() or []
    brawler_options = [{"value": r["value"], "label": r["label"]} for r in brawler_rows if r.get("value")]

    # ✅ Map for hero image swapping when a brawler is selected
    brawler_model_map = {}
    for r in brawler_rows:
        if r.get("label") and r.get("model_url"):
            brawler_model_map[r["label"]] = r["model_url"]
        # also allow lookup by raw value
        if r.get("value") and r.get("model_url"):
            brawler_model_map[r["value"]] = r["model_url"]

    # -------- summary (total count) --------
    cur.execute(
        f"""
        SELECT
          COUNT(*) AS total_matches,
          COUNT(*) FILTER (WHERE result = 'victory') AS wins,
          COUNT(*) FILTER (WHERE result = 'defeat')  AS losses,
          COUNT(*) FILTER (WHERE result = 'draw')    AS draws,
          COUNT(*) FILTER (WHERE b.is_starplayer IS TRUE) AS star_player_games
        FROM battles b
        WHERE {where_sql};
        """,
        params,
    )
    summary = cur.fetchone() or {
    "total_matches": 0, "wins": 0, "losses": 0, "draws": 0, "star_player_games": 0
    }

    total = int(summary.get("total_matches") or 0)
    wins = int(summary.get("wins") or 0)
    winrate = round(wins * 100.0 / total, 1) if total else None
    total_pages = max(1, math.ceil(total / PER_PAGE)) if total else 1

    # Companion summary deliberately ignores page filters: it answers the
    # stable product question over 7/30-day windows for validated Ladder rows.
    cur.execute(
        """
        WITH windows(days) AS (VALUES (7), (14), (30)),
        modeled AS (
          SELECT w.days,
                 COUNT(f.*) AS modeled_battles,
                 COUNT(*) FILTER (WHERE f.win_label = 1) AS actual_wins,
                 COALESCE(SUM(f.pred_win_prob), 0) AS expected_wins,
                 COUNT(*) FILTER (WHERE f.win_label=1 AND f.pred_win_prob>=0.5) AS expected_win_count,
                 COUNT(*) FILTER (WHERE f.win_label=1 AND f.pred_win_prob<0.5) AS surprise_win_count,
                 COUNT(*) FILTER (WHERE f.win_label=0 AND f.pred_win_prob<0.5) AS expected_loss_count,
                 COUNT(*) FILTER (WHERE f.win_label=0 AND f.pred_win_prob>=0.5) AS missed_win_count
          FROM windows w
          LEFT JOIN battle_features_3v3 f
            ON f.player_tag=%s
           AND f.battletime >= now() - (w.days * interval '1 day')
           AND f.pred_win_prob IS NOT NULL
           AND f.win_label IN (0,1)
          GROUP BY w.days
        ), totals AS (
          SELECT w.days, COUNT(b.*) AS total_battles
          FROM windows w
          LEFT JOIN battles b
            ON b.player_tag=%s
           AND COALESCE(b.battletime_ts, to_timestamp(left(b.battleTime,15),'YYYYMMDD"T"HH24MISS'))
               >= now() - (w.days * interval '1 day')
          GROUP BY w.days
        )
        SELECT m.*, t.total_battles
        FROM modeled m JOIN totals t USING(days)
        ORDER BY m.days
        """,
        (cleaned, cleaned),
    )
    companion_windows = {}
    for row in cur.fetchall():
        days = int(row["days"])
        modeled = int(row["modeled_battles"] or 0)
        total_battles = int(row["total_battles"] or 0)
        actual_wins = int(row["actual_wins"] or 0)
        expected_wins = float(row["expected_wins"] or 0)
        companion_windows[days] = {
            **dict(row),
            "modeled_battles": modeled,
            "total_battles": total_battles,
            "unmodeled_battles": max(0, total_battles - modeled),
            "actual_wins": actual_wins,
            "expected_wins": round(expected_wins, 1),
            "overperformance": round(actual_wins - expected_wins, 1),
            "coverage_pct": round(modeled * 100.0 / total_battles, 1) if total_battles else 0,
        }

    cur.execute(
        """
        SELECT victory_score, victory_score_version, adjusted_lift,
               evidence_confidence, n_battles, updated_at
        FROM player_victory_rollups
        WHERE player_tag=%s AND window_days=30
          AND mode=%s AND type=%s AND brawler_name=%s
        LIMIT 1
        """,
        (cleaned, ALL_DIM, ALL_DIM, ALL_DIM),
    )
    score_row = cur.fetchone() or {}

    cur.execute(
        """
        SELECT snapshot_date, captured_at, victory_score, n_battles,
               wins, expected_wins, adjusted_lift, evidence_confidence
        FROM player_victory_score_history
        WHERE player_tag=%s AND window_days=30
          AND mode=%s AND type=%s AND brawler_name=%s
        ORDER BY snapshot_date DESC
        LIMIT 90
        """,
        (cleaned, ALL_DIM, ALL_DIM, ALL_DIM),
    )
    score_history = [dict(row) for row in cur.fetchall()]

    cur.execute(
        """
        SELECT f.battletime, b.brawler_name, COALESCE(gm.name, f.mode) AS mode_name,
               f.win_label, f.pred_win_prob,
               (f.win_label - f.pred_win_prob) AS result_signal
        FROM battle_features_3v3 f
        JOIN battles b
          ON f.battletime=COALESCE(b.battletime_ts,(b.battletime)::timestamptz)
         AND f.player_tag=b.player_tag AND f.mode=b.mode
         AND f.map_key=COALESCE(b.map_id::text,b.map)
         AND f.type IS NOT DISTINCT FROM b.type
         AND f.duration IS NOT DISTINCT FROM b.duration
        LEFT JOIN game_modes gm ON lower(gm.sc_hash)=lower(f.mode)
        WHERE f.player_tag=%s AND f.battletime>=now()-interval '30 days'
          AND f.pred_win_prob IS NOT NULL AND f.win_label IN (0,1)
        ORDER BY abs(f.win_label-f.pred_win_prob) DESC, f.battletime DESC
        LIMIT 20
        """,
        (cleaned,),
    )
    score_contributors = []
    for row in cur.fetchall():
        item = dict(row)
        item["result_signal"] = round(float(item["result_signal"]), 2)
        item["predicted_pct"] = round(float(item["pred_win_prob"]) * 100)
        score_contributors.append(item)

    cur.execute(
        """
        WITH days AS (
          SELECT generate_series(current_date - 13, current_date, interval '1 day')::date AS day
        ), daily AS (
          SELECT battletime::date AS day,
                 COUNT(*) FILTER (WHERE win_label=1) AS wins,
                 COALESCE(SUM(pred_win_prob),0) AS expected_wins,
                 COUNT(*) AS modeled
          FROM battle_features_3v3
          WHERE player_tag=%s AND battletime >= current_date - 13
            AND pred_win_prob IS NOT NULL AND win_label IN (0,1)
          GROUP BY battletime::date
        )
        SELECT d.day, COALESCE(x.wins,0) AS wins,
               COALESCE(x.expected_wins,0) AS expected_wins,
               COALESCE(x.modeled,0) AS modeled
        FROM days d LEFT JOIN daily x USING(day) ORDER BY d.day
        """,
        (cleaned,),
    )
    trend = []
    for row in cur.fetchall():
        delta = float(row["wins"] or 0) - float(row["expected_wins"] or 0)
        trend.append({"day": row["day"], "delta": round(delta, 2), "modeled": int(row["modeled"] or 0)})
    scale = max([abs(point["delta"]) for point in trend] + [1.0])
    for point in trend:
        point["height"] = round(12 + (abs(point["delta"]) / scale) * 38)
        point["level"] = min(5, max(1, int(round((abs(point["delta"]) / scale) * 4)) + 1))
    recent_delta = round(sum(point["delta"] for point in trend[-7:]), 1)
    prior_delta = round(sum(point["delta"] for point in trend[:7]), 1)
    companion_summary = {
        "windows": companion_windows,
        "victory_score": round(float(score_row["victory_score"])) if score_row.get("victory_score") is not None else None,
        "victory_score_version": score_row.get("victory_score_version"),
        "adjusted_lift_pct": round(float(score_row["adjusted_lift"]) * 100, 1) if score_row.get("adjusted_lift") is not None else None,
        "evidence_confidence": round(float(score_row["evidence_confidence"]) * 100) if score_row.get("evidence_confidence") is not None else None,
        "evidence_label": (
            "High evidence" if float(score_row.get("evidence_confidence") or 0) >= 0.65
            else "Medium evidence" if float(score_row.get("evidence_confidence") or 0) >= 0.35
            else "Provisional"
        ),
        "eligible_battles": int(score_row.get("n_battles") or 0),
        "score_updated_at": score_row.get("updated_at"),
        "score_history": score_history,
        "score_contributors_positive": [x for x in score_contributors if x["result_signal"] > 0][:3],
        "score_contributors_negative": [x for x in score_contributors if x["result_signal"] < 0][:3],
        "trend": trend,
        "recent_delta": recent_delta,
        "prior_delta": prior_delta,
        "trend_change": round(recent_delta - prior_delta, 1),
    }
    _, companion_summary["victory_label"] = victory_tier_and_label(companion_summary["victory_score"])

    # Human-readable play sessions and result streaks. A 45-minute pause starts
    # a new session; this is descriptive history and never a prediction input.
    cur.execute(
        """
        SELECT battletime_ts, result, mode, map, brawler_name
        FROM battles
        WHERE player_tag=%s AND battletime_ts>=now()-interval '30 days'
        ORDER BY battletime_ts DESC
        """,
        (cleaned,),
    )
    recent_rows = [dict(row) for row in cur.fetchall()]
    sessions = []
    current = None
    for battle in recent_rows:
        stamp = battle.get("battletime_ts")
        if current is None or (current["oldest_at"] - stamp).total_seconds() > 45 * 60:
            current = {"newest_at": stamp, "oldest_at": stamp, "battles": 0, "wins": 0, "losses": 0, "draws": 0, "brawlers": set()}
            sessions.append(current)
        current["oldest_at"] = stamp
        current["battles"] += 1
        result = (battle.get("result") or "").lower()
        if result == "victory": current["wins"] += 1
        elif result == "defeat": current["losses"] += 1
        else: current["draws"] += 1
        if battle.get("brawler_name"):
            current["brawlers"].add(battle["brawler_name"])
    for session in sessions:
        session["duration_minutes"] = max(0, int((session["newest_at"] - session["oldest_at"]).total_seconds() // 60))
        session["brawlers"] = sorted(session["brawlers"])
        session["win_rate"] = round(session["wins"] * 100 / session["battles"]) if session["battles"] else 0

    streak_kind = None
    streak_count = 0
    initial_streak_open = True
    longest_win_streak = 0
    running_wins = 0
    for battle in recent_rows:
        outcome = "win" if battle.get("result") == "victory" else "loss" if battle.get("result") == "defeat" else "draw"
        if streak_kind is None:
            streak_kind, streak_count = outcome, 1
        elif initial_streak_open and outcome == streak_kind:
            streak_count += 1
        else:
            initial_streak_open = False
        if outcome == "win":
            running_wins += 1
            longest_win_streak = max(longest_win_streak, running_wins)
        else:
            running_wins = 0
    session_context = {
        "current": sessions[0] if sessions else None,
        "recent": sessions[:6],
        "session_count_30d": len(sessions),
        "streak_kind": streak_kind,
        "streak_count": streak_count,
        "longest_win_streak_30d": longest_win_streak,
    }

    # Direct teammate/opponent history from complete roster observations.
    cur.execute(
        """
        WITH focal AS (
          SELECT battletime, raw_battle_hash, team_index, result
          FROM battles
          WHERE player_tag=%s AND battletime_ts>=now()-interval '30 days'
            AND raw_battle_hash IS NOT NULL
        ), meetings AS (
          SELECT other.player_tag, max(other.player_name) AS player_name,
                 CASE WHEN focal.team_index IS NOT NULL AND other.team_index=focal.team_index
                      THEN 'teammate' ELSE 'opponent' END AS relationship,
                 count(DISTINCT focal.battletime) AS meetings,
                 count(DISTINCT focal.battletime) FILTER (WHERE focal.result='victory') AS wins,
                 max(other.brawler_name) AS latest_brawler
          FROM focal
          JOIN battles other ON other.raw_battle_hash=focal.raw_battle_hash
            AND other.player_tag<>%s
          GROUP BY other.player_tag,
                   CASE WHEN focal.team_index IS NOT NULL AND other.team_index=focal.team_index
                        THEN 'teammate' ELSE 'opponent' END
          HAVING count(DISTINCT focal.battletime) >= 2
        ), ranked AS (
          SELECT *, row_number() OVER (PARTITION BY relationship ORDER BY meetings DESC, player_name) AS rn
          FROM meetings
        )
        SELECT * FROM ranked WHERE rn<=6 ORDER BY relationship, rn
        """,
        (cleaned, cleaned),
    )
    relationship_context = {"teammate": [], "opponent": []}
    for row in cur.fetchall():
        item = dict(row)
        item["win_rate"] = round(int(item["wins"] or 0) * 100 / int(item["meetings"] or 1))
        relationship_context[item["relationship"]].append(item)

    # Thirty-day Ladder-only performance lenses. Expected wins are always the
    # sum of published probabilities; small samples stay visible but labeled.
    cur.execute(
        """
        WITH modeled AS (
          SELECT f.battletime, f.player_tag, f.mode, f.map_key,
                 f.pred_win_prob, f.win_label,
                 b.brawler_name,
                 COALESCE(gm.name, f.mode) AS mode_name,
                 gm.regular_url AS mode_icon,
                 COALESCE(m.name, b.map, f.map_key) AS map_name,
                 m.image_url AS map_image,
                 br.emoji_url AS brawler_icon
          FROM battle_features_3v3 f
          JOIN battles b
            ON f.battletime = COALESCE(b.battletime_ts, (b.battletime)::timestamptz)
           AND f.player_tag = b.player_tag
           AND f.mode = b.mode
           AND f.map_key = COALESCE(b.map_id::text, b.map)
           AND f.type IS NOT DISTINCT FROM b.type
           AND f.duration IS NOT DISTINCT FROM b.duration
          LEFT JOIN game_modes gm ON lower(gm.sc_hash)=lower(f.mode)
          LEFT JOIN maps m ON m.id=b.map_id
          LEFT JOIN brawlers br ON lower(br.name)=lower(b.brawler_name)
          WHERE f.player_tag=%s
            AND f.battletime >= now() - interval '30 days'
            AND f.pred_win_prob IS NOT NULL
            AND f.win_label IN (0,1)
        ), dimensions AS (
          SELECT 'brawler'::text AS dimension, brawler_name AS key,
                 brawler_name AS label, max(brawler_icon) AS icon,
                 count(*) AS battles, sum(win_label) AS actual_wins,
                 sum(pred_win_prob) AS expected_wins
          FROM modeled WHERE brawler_name IS NOT NULL GROUP BY brawler_name
          UNION ALL
          SELECT 'mode', mode, max(mode_name), max(mode_icon), count(*),
                 sum(win_label), sum(pred_win_prob)
          FROM modeled GROUP BY mode
          UNION ALL
          SELECT 'map', map_key, max(map_name), max(map_image), count(*),
                 sum(win_label), sum(pred_win_prob)
          FROM modeled GROUP BY map_key
        ), ranked AS (
          SELECT *, row_number() over(partition by dimension order by battles desc, label) AS rn
          FROM dimensions
        )
        SELECT dimension,key,label,icon,battles,actual_wins,expected_wins
        FROM ranked WHERE rn<=8 ORDER BY dimension,rn
        """,
        (cleaned,),
    )
    companion_breakdowns = {"brawler": [], "mode": [], "map": []}
    for row in cur.fetchall():
        item = dict(row)
        games = int(item.get("battles") or 0)
        actual = int(item.get("actual_wins") or 0)
        expected = float(item.get("expected_wins") or 0)
        delta = actual - expected
        item.update({
            "battles": games,
            "actual_wins": actual,
            "expected_wins": round(expected, 1),
            "delta": round(delta, 1),
            "actual_rate": round(actual * 100.0 / games, 1) if games else 0,
            "expected_rate": round(expected * 100.0 / games, 1) if games else 0,
            "sample_label": "Established" if games >= 20 else ("Emerging" if games >= 10 else "Early read"),
            "sample_class": "established" if games >= 20 else ("emerging" if games >= 10 else "early"),
            "bar_actual": round(actual * 100.0 / games, 1) if games else 0,
            "bar_expected": round(expected * 100.0 / games, 1) if games else 0,
        })
        companion_breakdowns[item["dimension"]].append(item)

    cur.execute(
        """
        SELECT p.trophies, p.highest_trophies, p.ranked_elo, p.ranked_rank_name,
               p.highest_season_ranked_elo, p.highest_season_ranked_rank_name,
               p.highest_all_time_ranked_elo, p.highest_all_time_ranked_rank_name,
               p.total_prestige_level,
               s.last_successful_battle_poll_at, s.last_successful_profile_poll_at,
               s.next_battle_poll_at, s.polling_interval_seconds, s.possible_gap,
               latest.captured_at AS latest_snapshot_at,
               (SELECT max(pred_updated_at) FROM battle_features_3v3 WHERE player_tag=%s) AS latest_prediction_at,
               (SELECT count(*) FROM battles b WHERE b.player_tag=%s
                  AND b.battletime_ts>=now()-interval '30 days' AND b.type='soloRanked') AS ranked_battles_30d,
               (SELECT count(*) FROM battle_profile_context c JOIN battles b
                    ON b.battletime=c.battletime AND b.player_tag=c.player_tag
                  WHERE c.player_tag=%s AND b.battletime_ts>=now()-interval '30 days'
                    AND b.type='soloRanked' AND c.ranked_elo IS NOT NULL) AS ranked_elo_context_30d,
               (SELECT count(*) FROM battles b WHERE b.player_tag=%s
                  AND b.battletime_ts>=now()-interval '30 days' AND b.possible_gap) AS possible_gap_battles_30d
        FROM player_profiles p
        LEFT JOIN tracked_player_state s ON s.player_tag=p.tag
        LEFT JOIN LATERAL (
          SELECT captured_at FROM player_profile_snapshots ps
          WHERE ps.player_tag=p.tag AND ps.historical_precision='timestamped'
          ORDER BY captured_at DESC LIMIT 1
        ) latest ON true
        WHERE p.tag=%s
        """,
        (cleaned, cleaned, cleaned, cleaned, cleaned),
    )
    context_row = dict(cur.fetchone() or {})
    ranked_total = int(context_row.get("ranked_battles_30d") or 0)
    ranked_covered = int(context_row.get("ranked_elo_context_30d") or 0)
    gap_battles = int(context_row.get("possible_gap_battles_30d") or 0)
    poll_at = context_row.get("last_successful_battle_poll_at")
    profile_at = context_row.get("last_successful_profile_poll_at") or context_row.get("latest_snapshot_at")
    now_utc = dt.datetime.now(dt.timezone.utc)
    def age_minutes(value):
        if not value:
            return None
        aware = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
        return max(0, int((now_utc - aware).total_seconds() // 60))
    poll_age = age_minutes(poll_at)
    profile_age = age_minutes(profile_at)
    context_status = "healthy"
    if context_row.get("possible_gap") or gap_battles:
        context_status = "attention"
    elif poll_age is None or poll_age > 30 or profile_age is None or profile_age > 120:
        context_status = "stale"
    progression_context = {
        **context_row,
        "poll_age_minutes": poll_age,
        "profile_age_minutes": profile_age,
        "ranked_battles_30d": ranked_total,
        "ranked_elo_context_30d": ranked_covered,
        "ranked_context_pct": round(ranked_covered * 100.0 / ranked_total, 1) if ranked_total else 0,
        "possible_gap_battles_30d": gap_battles,
        "status": context_status,
        "ranked_gate_label": "Collecting honest history" if ranked_total and ranked_covered < max(50, ranked_total) else "Coverage accumulating",
    }

    # -------- top stats blocks --------
    cur.execute(
        f"""
        SELECT
          b.brawler_name,
          br.emoji_url AS brawler_icon,
          COUNT(*) AS games,
          COUNT(*) FILTER (WHERE b.result = 'victory') AS wins
        FROM battles b
        LEFT JOIN brawlers br
          ON LOWER(br.name) = LOWER(b.brawler_name)
        WHERE {where_sql}
        GROUP BY b.brawler_name, br.emoji_url
        ORDER BY games DESC
        LIMIT 5;
        """,
        params,
    )
    brawler_stats = cur.fetchall()

    cur.execute(
        f"""
        SELECT
          COALESCE(gm.name, b.mode) AS mode_name,
          gm.regular_url AS mode_icon,
          COUNT(*) AS games,
          COUNT(*) FILTER (WHERE b.result = 'victory') AS wins
        FROM battles b
        LEFT JOIN game_modes gm
          ON LOWER(gm.sc_hash) = LOWER(b.mode)
        WHERE {where_sql}
        GROUP BY COALESCE(gm.name, b.mode), gm.regular_url
        ORDER BY games DESC;
        """,
        params,
    )
    modes_stats = cur.fetchall()

    # ✅ paged battles list
    cur.execute(
        f"""
        SELECT
          b.battleTime,
          b.result,
          b.type,
          b.mode,
          b.map_id,
          b.map,
          b.brawler_name,
          b.rank,
          b.trophyChange,

          COALESCE(gm.name, b.mode) AS mode_name,
          gm.regular_url            AS mode_icon,

          COALESCE(m.name, b.map)   AS map_name,
          m.image_url               AS map_image,

          br.emoji_url              AS brawler_icon,
          br.model_url              AS brawler_model_url,

          bf.pred_win_prob          AS pred_win_prob,
          bf.win_label              AS prediction_win_label,
          bf.pred_explanation_player AS pred_explanation_player,
          bf.pred_explanation       AS pred_explanation,
          bf.model_version          AS prediction_model_version,
          bf.pred_updated_at        AS prediction_updated_at
        FROM battles b
        LEFT JOIN game_modes gm
          ON LOWER(gm.sc_hash) = LOWER(b.mode)
        LEFT JOIN maps m
          ON m.id = b.map_id
        LEFT JOIN brawlers br
          ON LOWER(br.name) = LOWER(b.brawler_name)
        LEFT JOIN battle_features_3v3 bf
          ON bf.battletime = COALESCE(b.battletime_ts, (b.battletime)::timestamptz)
         AND bf.mode = b.mode
         AND bf.map_key = COALESCE(b.map_id::text, b.map)
         AND bf.type IS NOT DISTINCT FROM b.type
         AND bf.duration IS NOT DISTINCT FROM b.duration
         AND bf.player_tag = b.player_tag
        WHERE {where_sql}
        ORDER BY b.battleTime DESC
        LIMIT %s OFFSET %s;
        """,
        params + [PER_PAGE, offset],
    )
    battles = cur.fetchall()

    for b in battles:
        raw = b.get("battleTime") or b.get("battletime")
        if raw:
            try:
                dt_obj = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
                b["battle_time_human"] = dt_obj.strftime("%b %d, %Y · %H:%M")
            except Exception:
                b["battle_time_human"] = raw
        else:
            b["battle_time_human"] = "-"
        if b.get("pred_win_prob") is not None and b.get("prediction_win_label") in (0, 1):
            b["score_result_signal"] = round(float(b["prediction_win_label"]) - float(b["pred_win_prob"]), 2)
        else:
            b["score_result_signal"] = None
        b["prediction_freshness_label"] = "Not available"
        updated = b.get("prediction_updated_at")
        if updated:
            aware = updated if updated.tzinfo else updated.replace(tzinfo=dt.timezone.utc)
            age = max(0, int((dt.datetime.now(dt.timezone.utc) - aware).total_seconds() // 60))
            b["prediction_freshness_label"] = f"{age}m ago" if age < 120 else (f"{age // 60}h ago" if age < 2880 else f"{age // 1440}d ago")

    cur.close()
    conn.close()

    return templates.TemplateResponse(
        request,
        "player_battles.html",
        {
            "tag": cleaned,

            # ✅ hero visuals
            "player_icon_url": player_icon_url,
            "brawler_model_map": brawler_model_map,

            # summary/cards
            "summary": summary,
            "winrate": winrate,
            "modes_stats": modes_stats,
            "brawler_stats": brawler_stats,
            "companion_summary": companion_summary,
            "companion_breakdowns": companion_breakdowns,
            "progression_context": progression_context,
            "session_context": session_context,
            "relationship_context": relationship_context,

            # table data
            "battles": battles,

            # filters
            "from_date": from_d.isoformat(),
            "to_date": to_d.isoformat(),
            "from_ts": from_ts,
            "to_ts": to_ts,
            "mode_filter": mode,       # raw value
            "type_filter": btype,
            "brawler_filter": brawler,
            "victory_score_filter": victory_score,

            # ✅ dropdowns now {value,label}
            "mode_options": mode_options,
            "type_options": type_options,
            "brawler_options": brawler_options,

            # ✅ pagination
            "page": page,
            "per_page": PER_PAGE,
            "total_pages": total_pages,
            "pred_threshold": PRED_WIN_THRESHOLD,
        },
    )

# ============================================================
# ✅ UPDATED: player daily stats now supports battles.mode filter
# ============================================================

@app.get("/api/player-daily-stats/{tag}")
def api_player_daily_stats(
    tag: str,
    brawler: str = Query(default=None),
    period: str = Query(default="30"),
    mode: str = Query(default=None),   # ✅ battles.mode
    btype: str = Query(default=None),  # ✅ battles.type (teamRanked/soloRanked/etc.)
):
    cleaned = normalize_tag(tag)

    def _parse_days(p: str):
        if p == "all":
            return None
        try:
            return int(p)
        except ValueError:
            return 30

    days = _parse_days(period)
    today = dt.date.today()
    from_date = today - dt.timedelta(days=days) if days is not None else None

    conn = get_conn()
    cur = conn.cursor()

    # -------------------------
    # Battles filters (chart + winrate + KPIs)
    # -------------------------
    battles_params = [cleaned]

    date_filter_battles_sql = ""
    if from_date is not None:
        from_prefix = from_date.strftime("%Y%m%d")
        date_filter_battles_sql = "AND LEFT(b.battleTime, 8) >= %s"
        battles_params.append(from_prefix)

    brawler_filter_sql = ""
    if brawler:
        brawler_filter_sql = "AND LOWER(b.brawler_name) = LOWER(%s)"
        battles_params.append(brawler)

    mode_filter_sql = ""
    if mode:
        mode_filter_sql = "AND LOWER(b.mode) = LOWER(%s)"
        battles_params.append(mode)

    type_filter_sql = ""
    if btype:
        type_filter_sql = "AND LOWER(b.type) = LOWER(%s)"
        battles_params.append(btype)

    # Per-day battle counts + winrate (THIS is what the UI filters)
    cur.execute(
        f"""
        SELECT
          LEFT(b.battleTime, 8) AS d,
          COUNT(*) AS games,
          COUNT(*) FILTER (WHERE b.result = 'victory') AS wins,
          COUNT(*) FILTER (WHERE b.result = 'defeat')  AS losses,
          COUNT(*) FILTER (WHERE b.result = 'draw')    AS draws
        FROM battles b
        WHERE b.player_tag = %s
          {date_filter_battles_sql}
          {brawler_filter_sql}
          {mode_filter_sql}
          {type_filter_sql}
        GROUP BY LEFT(b.battleTime, 8)
        ORDER BY LEFT(b.battleTime, 8);
        """,
        battles_params,
    )
    battle_rows = cur.fetchall()

    per_day_map = {}
    total_games = total_wins = total_losses = total_draws = 0

    for d, games, wins, losses, draws in battle_rows:
        games = games or 0
        wins = wins or 0
        losses = losses or 0
        draws = draws or 0

        total_games += games
        total_wins += wins
        total_losses += losses
        total_draws += draws

        date_str = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        day_wr = (wins * 100.0 / games) if games else None

        per_day_map[date_str] = {
            "date": date_str,
            "games": games,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "trophies": None,
            "winrate_pct": round(day_wr, 1) if day_wr is not None else None,
        }

    # -------------------------
    # Trophies series (snapshots)
    # IMPORTANT: trophies snapshots are NOT per-mode/type,
    # so we do NOT apply battles.mode or battles.type here.
    # -------------------------
    if brawler:
        pda_params = [cleaned, brawler]
        date_filter_sql = ""
        if from_date is not None:
            date_filter_sql = "AND activity_date >= %s"
            pda_params.append(from_date)

        cur.execute(
            f"""
            SELECT activity_date, trophies
            FROM player_brawler_daily_activity
            WHERE player_tag = %s
              AND brawler_name = %s
              {date_filter_sql}
            ORDER BY activity_date;
            """,
            pda_params,
        )
    else:
        pda_params = [cleaned]
        date_filter_sql = ""
        if from_date is not None:
            date_filter_sql = "AND activity_date >= %s"
            pda_params.append(from_date)

        cur.execute(
            f"""
            SELECT activity_date, trophies
            FROM player_daily_activity
            WHERE player_tag = %s
              {date_filter_sql}
            ORDER BY activity_date;
            """,
            pda_params,
        )

    pda_rows = cur.fetchall()

    for day_obj, trophies in pda_rows:
        date_str = day_obj.isoformat()
        if date_str not in per_day_map:
            per_day_map[date_str] = {
                "date": date_str,
                "games": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "trophies": trophies,
                "winrate_pct": None,
            }
        else:
            per_day_map[date_str]["trophies"] = trophies

    per_day = sorted(per_day_map.values(), key=lambda x: x["date"])
    overall_wr = (total_wins * 100.0 / total_games) if total_games else None

    overall = {
        "total": total_games,
        "wins": total_wins,
        "losses": total_losses,
        "draws": total_draws,
        "winrate_pct": round(overall_wr, 1) if overall_wr is not None else None,
    }

    # -------------------------
    # Brawler dropdown options (unfiltered)
    # -------------------------
    cur.execute(
        """
        SELECT DISTINCT
          COALESCE(br.name, b.brawler_name) AS name,
          br.model_url AS model_url
        FROM battles b
        LEFT JOIN brawlers br
          ON LOWER(br.name) = LOWER(b.brawler_name)
        WHERE b.player_tag = %s
          AND b.brawler_name IS NOT NULL
        ORDER BY COALESCE(br.name, b.brawler_name);
        """,
        (cleaned,),
    )
    brawler_rows = cur.fetchall()
    brawlers = [{"name": name, "model_url": model_url} for (name, model_url) in brawler_rows if name]

    # -------------------------
    # Mode dropdown options (unfiltered)
    # returns {label,value} where value is raw battles.mode
    # -------------------------
    cur.execute(
        """
        SELECT DISTINCT
          COALESCE(gm.name, b.mode) AS label,
          b.mode AS value
        FROM battles b
        LEFT JOIN game_modes gm
          ON LOWER(gm.sc_hash) = LOWER(b.mode)
        WHERE b.player_tag = %s
          AND b.mode IS NOT NULL
        ORDER BY 1;
        """,
        (cleaned,),
    )
    mode_rows = cur.fetchall()
    modes = [{"label": label, "value": value} for (label, value) in mode_rows if value]

    # -------------------------
    # ✅ Type dropdown options (unfiltered)
    # returns {label,value} where value is raw battles.type
    # -------------------------
    cur.execute(
        """
        SELECT DISTINCT
          b.type AS value,
          b.type AS label
        FROM battles b
        WHERE b.player_tag = %s
          AND b.type IS NOT NULL
        ORDER BY 1;
        """,
        (cleaned,),
    )
    type_rows = cur.fetchall()
    types = [{"label": label, "value": value} for (value, label) in type_rows if value]

    cur.close()
    conn.close()

    return {
        "overall": overall,
        "per_day": per_day,
        "brawlers": brawlers,
        "modes": modes,
        "types": types,   # ✅ NEW
    }

@app.get("/api/collector-health")
def api_collector_health():
    """Phase 4 freshness, coverage, and gap telemetry for the live UI."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT
          (SELECT count(*) FROM tracked_players) AS tracked_players,
          (SELECT count(*) FROM tracked_player_state
             WHERE last_successful_battle_poll_at >= now() - interval '30 minutes') AS fresh_tracked_players,
          (SELECT count(*) FROM tracked_player_state WHERE possible_gap) AS possible_gap_players,
          (SELECT max(last_successful_battle_poll_at) FROM tracked_player_state) AS last_successful_poll_at,
          (SELECT max(battletime_ts) FROM battles) AS newest_battle_at,
          (SELECT count(*) FROM player_profile_snapshots) AS profile_snapshots,
          (SELECT max(captured_at) FROM player_profile_snapshots
             WHERE source = 'supercell_player_api') AS newest_profile_snapshot_at,
          (SELECT count(*) FROM collector_polls
             WHERE rate_limited AND started_at >= now() - interval '24 hours') AS rate_limits_24h,
          (SELECT count(*) FROM collector_polls
             WHERE NOT success AND started_at >= now() - interval '24 hours') AS failures_24h
        """
    )
    row = dict(cur.fetchone() or {})
    cur.close()
    conn.close()
    for key, value in list(row.items()):
        if hasattr(value, "isoformat"):
            row[key] = value.isoformat()
    row["database_target"] = "self-hosted-postgres"
    row["battle_poll_baseline_seconds"] = 900
    row["battle_poll_accelerated_seconds"] = 300
    return row


@app.get("/api/battles/{tag}/{battle_time}")
def api_battle_detail(tag: str, battle_time: str):
    cleaned = normalize_tag(tag)

    # Read-only: missing post-match profiles remain missing until an explicit job runs.
    scout_status = {"mode": "cached_only", "queued": 0}

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ✅ IMPORTANT:
    # - filter by battletime AND player's tag
    # - use correct column name: is_starplayer
    # - keep output field name consistent for JS: is_starplayer
    cur.execute(
        """
        SELECT
          b.battletime AS "battleTime",
          b.mode,
          COALESCE(gm.name, b.mode) AS mode_name,
          gm.regular_url            AS mode_icon,

          b.map_id,
          b.map,
          COALESCE(m.name, b.map)   AS map_name,
          m.image_url               AS map_image,

          b.result,
          b.type,
          b.duration,
          b.team_index,

          b.player_tag,
          b.player_name,
          b.player_total_trophies,
          b.player_level,

          b.brawler_id,
          b.brawler_name,
          b.brawler_power,
          b.brawler_trophies,
          b.brawler_total_trophies,

          b.rank,
          b.trophychange,
          b.trophychange AS "trophyChange",

          b.is_starplayer AS is_starplayer,

          b.is_highest_player_brawler,
          br.emoji_url AS brawler_emoji_url,

          ctx.profile_captured_at,
          ctx.profile_age_at_battle_seconds,
          ctx.profile_trophies AS snapshot_profile_trophies,
          ctx.ranked_elo AS snapshot_ranked_elo,
          ctx.ranked_rank AS snapshot_ranked_rank,
          ctx.total_prestige_level AS snapshot_total_prestige_level,
          ctx.snapshot_brawler_power,
          ctx.snapshot_brawler_trophies,
          ctx.snapshot_brawler_prestige_level,
          ctx.snapshot_brawler_win_streak,
          ps.ranked_rank_name AS snapshot_ranked_rank_name,
          ps.highest_trophies AS snapshot_profile_highest_trophies,
          ps.exp_level AS snapshot_profile_exp_level,
          ps.total_prestige_level AS snapshot_account_prestige,
          ps.victories_3v3 AS snapshot_victories_3v3,
          ps.club_name AS snapshot_club_name,
          bs.rank AS snapshot_brawler_rank,
          bs.highest_trophies AS snapshot_brawler_highest_trophies,
          bs.max_win_streak AS snapshot_brawler_max_win_streak,
          bs.skin_json AS snapshot_brawler_skin,
          bs.star_powers_json AS snapshot_star_powers,
          bs.gadgets_json AS snapshot_gadgets,
          bs.gears_json AS snapshot_gears,
          bs.hypercharges_json AS snapshot_hypercharges,
          ps.icon_id AS snapshot_icon_id
          ,current_ps.captured_at AS current_profile_captured_at
          ,current_ps.trophies AS current_profile_trophies
          ,current_ps.highest_trophies AS current_profile_highest_trophies
          ,current_ps.exp_level AS current_profile_exp_level
          ,current_ps.ranked_elo AS current_ranked_elo
          ,current_ps.ranked_rank_name AS current_ranked_rank_name
          ,current_ps.total_prestige_level AS current_account_prestige
          ,current_ps.victories_3v3 AS current_victories_3v3
          ,current_ps.solo_victories AS current_solo_victories
          ,current_ps.duo_victories AS current_duo_victories
          ,current_ps.club_name AS current_club_name
          ,current_ps.icon_id AS current_icon_id
          ,current_bs.rank AS current_brawler_rank
          ,current_bs.highest_trophies AS current_brawler_highest_trophies
          ,current_bs.prestige_level AS current_brawler_prestige_level
          ,current_bs.current_win_streak AS current_brawler_win_streak
          ,current_bs.max_win_streak AS current_brawler_max_win_streak
          ,current_bs.skin_json AS current_brawler_skin
          ,current_bs.star_powers_json AS current_star_powers
          ,current_bs.gadgets_json AS current_gadgets
          ,current_bs.gears_json AS current_gears
          ,current_bs.hypercharges_json AS current_hypercharges
          ,(SELECT count(*) FROM player_brawler_snapshots owned
             WHERE owned.profile_snapshot_id = current_ps.id) AS current_owned_brawlers
          ,(
             SELECT count(DISTINCT met.battletime)
             FROM battles met
             WHERE met.player_tag = b.player_tag
               AND met.battletime <= b.battletime
               AND EXISTS (
                 SELECT 1 FROM battles own
                 WHERE own.battletime = met.battletime AND own.player_tag = %s
               )
           ) AS encounters_total
          ,(
             SELECT count(DISTINCT met.battletime)
             FROM battles met
             WHERE met.player_tag = b.player_tag
               AND met.battletime <= b.battletime
               AND COALESCE(met.map_id::text, met.map, '') = COALESCE(b.map_id::text, b.map, '')
               AND EXISTS (
                 SELECT 1 FROM battles own
                 WHERE own.battletime = met.battletime AND own.player_tag = %s
               )
           ) AS encounters_same_map
        FROM battles b
        LEFT JOIN game_modes gm
          ON LOWER(gm.sc_hash) = LOWER(b.mode)
        LEFT JOIN maps m
          ON m.id = b.map_id
        LEFT JOIN brawlers br
          ON LOWER(br.name) = LOWER(b.brawler_name)
        LEFT JOIN battle_profile_context ctx
          ON ctx.battletime = b.battletime
         AND ctx.player_tag = b.player_tag
         AND ctx.brawler_id IS NOT DISTINCT FROM b.brawler_id
        LEFT JOIN player_profile_snapshots ps
          ON ps.id = ctx.profile_snapshot_id
        LEFT JOIN player_brawler_snapshots bs
          ON bs.profile_snapshot_id = ctx.profile_snapshot_id
         AND bs.brawler_id IS NOT DISTINCT FROM b.brawler_id
        LEFT JOIN LATERAL (
          SELECT latest.*
          FROM player_profile_snapshots latest
          WHERE latest.player_tag = b.player_tag
          ORDER BY latest.captured_at DESC
          LIMIT 1
        ) current_ps ON true
        LEFT JOIN player_brawler_snapshots current_bs
          ON current_bs.profile_snapshot_id = current_ps.id
         AND current_bs.brawler_id IS NOT DISTINCT FROM b.brawler_id
        WHERE b.battletime = %s
        ORDER BY (b.is_starplayer IS TRUE) DESC, b.rank ASC NULLS LAST;
        """,
        (cleaned, cleaned, battle_time),
    )

    rows = cur.fetchall() or []

    # ✅ Safety: only keep participants that belong to the same battleTime
    # (already true) and normalize output fields used by JS
    for r in rows:
        raw = r.get("battleTime") or r.get("battletime")
        if raw:
            try:
                dt_obj = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
                r["battle_time_human"] = dt_obj.strftime("%b %d, %Y · %H:%M")
            except Exception:
                r["battle_time_human"] = raw
        else:
            r["battle_time_human"] = "-"

        # ✅ Ensure boolean present for JS
        r["is_starplayer"] = bool(r.get("is_starplayer"))

        # Post-match bot-control evidence. Account resolution is intentionally not
        # human evidence: bot-controlled fighters can use resolvable account records.
        player_tag = str(r.get("player_tag") or "").upper()
        scout_result = scout_status.get("statuses", {}).get(player_tag)
        evidence = participant_evidence(r, scout_result)
        r["account_kind"] = evidence["account_kind"]
        r["account_label"] = evidence["account_label"]
        r["control_kind"] = evidence["control_kind"]
        r["control_label"] = evidence["control_label"]
        r["control_evidence"] = evidence["evidence_reasons"]
        # Compatibility fields consumed by the current UI. Bots take precedence;
        # otherwise the badge reports account availability, never "Player".
        if evidence["control_kind"] in {"bot", "likely-bot"}:
            r["identity_kind"] = evidence["control_kind"]
            r["identity_label"] = evidence["control_label"]
            r["identity_reason"] = ", ".join(evidence["evidence_reasons"])
        else:
            r["identity_kind"] = "account-found" if evidence["account_kind"] == "found" else "unknown"
            r["identity_label"] = evidence["account_label"]
            r["identity_reason"] = "Account availability only; human or bot control is not proven."
        r["hard_bot_signal"] = evidence["hard_bot_signal"]
        r["bot_features"] = evidence["features"]

    opponent_evidence = [r for r in rows if str(r.get("player_tag") or "").upper() != cleaned]
    rule_detection = lobby_detection(opponent_evidence)
    cur.execute(
        """SELECT label, source, confidence, notes, labeled_at
           FROM bot_lobby_labels
           WHERE observed_from_tag = %s AND battle_time = %s
           ORDER BY (confidence = 'trusted') DESC, labeled_at DESC LIMIT 1""",
        (cleaned, battle_time),
    )
    trusted_label = cur.fetchone()
    if trusted_label:
        bot_lobby = {
            **rule_detection,
            "label": trusted_label["label"],
            "basis": "trusted_label",
            "label_source": trusted_label["source"],
            "label_confidence": trusted_label["confidence"],
            "label_notes": trusted_label["notes"],
            "rule_label": rule_detection["label"],
        }
    else:
        bot_lobby = {**rule_detection, "basis": "rules"}
    cur.close()
    conn.close()

    return {
        "tracked_tag": cleaned,
        "participants": rows,
        "scout_profiles": scout_status,
        "bot_lobby_detection": bot_lobby,
    }

@app.get("/how-it-works")
def how_it_works_page(request: Request):
    return templates.TemplateResponse(request, "how.html", {"active_page": "how"})







@app.post("/api/lists/{list_id}/share")
def api_share_list(list_id: int, request: Request):
    owner_tag = _session_owner_tag(request)
    if not owner_tag:
        return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if not _list_belongs_to_owner(cur, int(list_id), owner_tag):
            return JSONResponse({"ok": False, "message": "List not found."}, status_code=404)

        # Optional: prevent sharing system lists
        cur.execute(
            "SELECT is_system, system_key FROM player_lists WHERE id = %s;",
            (int(list_id),)
        )
        meta = cur.fetchone() or {}

        if meta.get("is_system") is True and meta.get("system_key") != "my_club":
            return JSONResponse({"ok": False, "message": "This system list cannot be shared."}, status_code=400)


        # Ensure token exists
        cur.execute("SELECT share_token FROM player_lists WHERE id = %s;", (int(list_id),))
        row = cur.fetchone() or {}
        token = row.get("share_token")
        if not token:
            token = str(uuid.uuid4())
            cur.execute("UPDATE player_lists SET share_token = %s WHERE id = %s;", (token, int(list_id)))

        # Enable public
        cur.execute(
            "UPDATE player_lists SET is_public = TRUE, updated_at = now() WHERE id = %s;",
            (int(list_id),),
        )
        conn.commit()

        return JSONResponse({"ok": True, "share_token": token})
    finally:
        cur.close()
        conn.close()


@app.get("/api/shared/{token}")
def api_shared_meta(token: str):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        r = _public_list_by_token(cur, token)
        if not r:
            return JSONResponse({"ok": False, "message": "Shared list not found (or not public)."}, status_code=404)
        return JSONResponse({"ok": True, "list": r})
    finally:
        cur.close()
        conn.close()


@app.get("/api/shared/{token}/players")
def api_shared_players(token: str):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    seven_days_ago = (dt.datetime.utcnow().date() - dt.timedelta(days=7)).strftime("%Y%m%d")
    try:
        shared = _public_list_by_token(cur, token)
        if not shared:
            return JSONResponse({"ok": False, "message": "Shared list not found (or not public)."}, status_code=404)

        list_id = int(shared["id"])

        # Reuse your existing list players query BUT without owner checks.
        # Easiest: call the same SQL you already have in api_list_players,
        # replacing WHERE m.list_id = %s and removing owner checks.
        cur.execute(
            """
            SELECT
              p.tag,
              p.name,
              p.icon_id,
              p.trophies,
              p.highest_trophies,
              p.exp_level,
              p.total_prestige_level,
              p.ranked_rank AS ranked_level,
              p.ranked_rank_name,
              p.highest_all_time_ranked_rank AS highest_ranked_level,
              p.highest_all_time_ranked_rank_name AS highest_ranked_rank_name,
              p.favorite_brawler_name,
              p.favorite_brawler_trophies,
              p.first_seen,
              p.last_update,
              EXISTS (
                SELECT 1 FROM player_verifications pv
                WHERE pv.player_tag = p.tag AND pv.status = 'verified'
              ) AS verified_player,
              b.emoji_url AS favorite_brawler_emoji_url,
              w7.matches_7d,
              w7.wins_7d,
              w7.losses_7d,
              mu7.most_used_brawler_name,
              mu7.most_used_brawler_emoji_url,
              mu7.most_used_brawler_matches_7d,

              CASE
                WHEN p.ranked_rank IS NOT NULL AND p.ranked_rank > 0
                THEN 'https://cdn.brawlify.com/ranked/tiered/' || (58000000 + p.ranked_rank - 1) || '.png'
                ELSE NULL
              END AS ranked_icon_url,

              CASE
                WHEN p.highest_all_time_ranked_rank IS NOT NULL AND p.highest_all_time_ranked_rank > 0
                THEN 'https://cdn.brawlify.com/ranked/tiered/' || (58000000 + p.highest_all_time_ranked_rank - 1) || '.png'
                ELSE NULL
              END AS highest_ranked_icon_url,

              r.victory_score,
              r.updated_at AS victory_updated_at,

              COALESCE(pba.brawler_count, 0) AS brawlers_owned,
              bt.total_brawlers AS brawlers_total

            FROM player_list_members m
            JOIN player_lists l
              ON l.id = m.list_id
             AND l.is_deleted = FALSE

            JOIN player_profiles p
              ON p.tag = m.player_tag

            LEFT JOIN LATERAL (
              SELECT COUNT(*)::int AS brawler_count
              FROM player_brawlers pb
              WHERE pb.player_tag = p.tag
            ) pba ON TRUE

            CROSS JOIN LATERAL (
              SELECT COUNT(*)::int AS total_brawlers
              FROM brawlers
            ) bt

            LEFT JOIN brawlers b
              ON LOWER(b.name) = LOWER(p.favorite_brawler_name)

            LEFT JOIN player_victory_rollups r
              ON r.player_tag = p.tag
             AND r.window_days = %s
             AND r.mode = %s
             AND r.type = %s
             AND r.brawler_name = %s

            LEFT JOIN LATERAL (
                SELECT
                    COUNT(*)::int AS matches_7d,
                    COUNT(*) FILTER (WHERE b.result = 'victory')::int AS wins_7d,
                    COUNT(*) FILTER (WHERE b.result = 'defeat')::int  AS losses_7d
                FROM battles b
                WHERE b.player_tag = p.tag
                    AND LEFT(b.battleTime, 8) >= %s
            ) w7 ON TRUE

            LEFT JOIN LATERAL (
                SELECT
                    b.brawler_name AS most_used_brawler_name,
                    br.emoji_url   AS most_used_brawler_emoji_url,
                    COUNT(*)::int  AS most_used_brawler_matches_7d
                FROM battles b
                LEFT JOIN brawlers br
                    ON LOWER(br.name) = LOWER(b.brawler_name)
                WHERE b.player_tag = p.tag
                    AND b.brawler_name IS NOT NULL
                    AND LEFT(b.battleTime, 8) >= %s
                GROUP BY b.brawler_name, br.emoji_url
                ORDER BY COUNT(*) DESC
                LIMIT 1
            ) mu7 ON TRUE

            WHERE m.list_id = %s
              AND m.is_removed = FALSE
            ORDER BY p.trophies DESC NULLS LAST, p.tag ASC;
            """,
            (VICTORY_WINDOW_DAYS, ALL_DIM, ALL_DIM, ALL_DIM, seven_days_ago, seven_days_ago, list_id),
        )
        players = cur.fetchall() or []
        _add_victory_score_windows(cur, players)

        # same enrichment you already do (owned/total, tracked_since, tier/label)
        for p in players:
            owned = p.get("brawlers_owned")
            total = p.get("brawlers_total")
            p["brawlers_owned_total"] = "-" if owned is None or total is None else f"{owned} / {total}"

            fs = p.get("first_seen")
            if fs:
                try:
                    if getattr(fs, "tzinfo", None) is None:
                        fs = fs.replace(tzinfo=timezone.utc)
                    p["tracked_since_human"] = fs.strftime("%b %d, %Y")
                    p["tracked_since_tooltip"] = fs.strftime("%Y-%m-%d %H:%M %Z")
                except Exception:
                    p["tracked_since_human"] = str(fs)
                    p["tracked_since_tooltip"] = str(fs)
            else:
                p["tracked_since_human"] = "-"
                p["tracked_since_tooltip"] = "-"

            tier, label = victory_tier_and_label(p.get("victory_score"))
            p["victory_tier"] = tier
            p["victory_label"] = label

        return JSONResponse(content=jsonable_encoder({"ok": True, "players": players, "list": shared}))
    finally:
        cur.close()
        conn.close()

@app.get("/api/shared/{token}/meta")
def api_shared_meta_any(token: str):
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        row = _public_list_by_token(cur, token)
        if not row:
            return JSONResponse({"ok":False,"message":"Shared list not found."},status_code=404,headers={"Cache-Control":"no-store"})
        return JSONResponse({"ok":True,"list_id":int(row["id"]),"owner_tag":row.get("owner_tag"),"name":row.get("name"),"is_public":True},headers={"Cache-Control":"no-store"})

@app.post("/api/shared/{token}/copy")
def api_shared_copy(token: str, request: Request):
    owner_tag = _session_owner_tag(request)
    if not owner_tag:
        return JSONResponse({"ok": False, "message": "Not recognized."}, status_code=401)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        shared = _public_list_by_token(cur, token)
        if not shared:
            return JSONResponse({"ok": False, "message": "Shared list not found."}, status_code=404)

        src_list_id = int(shared["id"])
        src_name = shared.get("name") or "Shared list"

        # Create new list owned by recipient
        new_name = f"Copy of {src_name}"
        cur.execute(
            """
            INSERT INTO player_lists (owner_tag, name, is_favorite, is_deleted, is_system, is_deletable)
            VALUES (%s, %s, FALSE, FALSE, FALSE, TRUE)
            RETURNING id;
            """,
            (owner_tag, new_name),
        )
        new_list_id = int(cur.fetchone()["id"])

        # Copy members (only active, non-removed)
        cur.execute(
            """
            INSERT INTO player_list_members (list_id, player_tag, is_removed)
            SELECT %s, player_tag, FALSE
            FROM player_list_members
            WHERE list_id = %s AND is_removed = FALSE
            ON CONFLICT (list_id, player_tag)
            DO UPDATE SET is_removed = FALSE, removed_at = NULL;
            """,
            (new_list_id, src_list_id),
        )

        request.session["selected_list_id"] = new_list_id
        conn.commit()

        return JSONResponse({"ok": True, "new_list_id": new_list_id})
    finally:
        cur.close()
        conn.close()

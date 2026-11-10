"""Read-only standalone authorization guard.

Integrate through a *sync* FastAPI dependency wrapper accepting Request and calling
require_standalone_access(request, _identity_email, get_conn). Register it when
constructing FastAPI (before routes). Dependencies run inside SessionMiddleware;
returning a RedirectResponse from a dependency DOES NOT stop a route, so redirects
here are HTTPException(303, headers={"Location": ...}). Existing resource-level
list ACLs, challenge/email validation and same-origin checks must remain in place.
No identity may be taken from headers, selected_list_id or a public player tag.
"""
import re
from urllib.parse import quote

# Imported lazily below so classification and pure/mock tests need no web runtime.
_READ_METHODS = frozenset({"GET", "HEAD"})
_PUBLIC_PAGES = frozenset({"/login", "/how-it-works", "/us", "/event"})
_AUTH_POSTS = frozenset({"/api/auth/email/start", "/api/auth/email/verify"})
_EMAIL_POSTS = frozenset({
    "/identify", "/identify/confirm", "/switch-player", "/logout",
    "/api/accounts/switch", "/api/accounts/primary", "/api/accounts/unlink",
    "/api/player-verification/start",
})
_TAG = re.compile(r"#[0289PYLQGRJCUV]+")
_SHARED = re.compile(r"/api/shared/[^/]+(?:/(?:players|meta))?")
_VERIFY_PAGE = re.compile(r"/verify-player/[^/]+")
_VERIFY_STATUS = re.compile(r"/api/player-verification/[^/]+")
_OWNER_PATHS = frozenset({"/api/me", "/api/players", "/api/my-club/select", "/add-player"})


def classify_route(method, path):
    """Return public/root/email/owner; unknown reads need email, writes proof.

    Explicit bootstrap allowlist avoids requiring proof to acquire/recover proof.
    Unknown /api/lists descendants and all shared mutations remain owner-gated.
    OPTIONS is deliberately NOT a blanket authentication exemption.
    """
    method = method.upper()
    if method in _READ_METHODS:
        if path == "/":
            return "root"
        if path == "/api/me" or path in _PUBLIC_PAGES or path.startswith("/static/") or _SHARED.fullmatch(path):
            return "public"
        if _VERIFY_PAGE.fullmatch(path) or _VERIFY_STATUS.fullmatch(path):
            return "email"
    if method == "POST" and (path in _AUTH_POSTS or path == "/logout"):
        return "public"
    if method == "POST" and (path in _EMAIL_POSTS or _VERIFY_STATUS.fullmatch(path)):
        return "email"
    if (path in _OWNER_PATHS or path == "/api/lists" or path.startswith("/api/lists/")
            or path.startswith("/api/shared/")):
        return "owner"
    return "email" if method in _READ_METHODS else "owner"


def _owner_tag(session):
    value = session.get("owner_tag")
    if not isinstance(value, str):
        return None
    value = value.strip().upper().replace("O", "0")
    if value and not value.startswith("#"):
        value = "#" + value
    return value if _TAG.fullmatch(value) else None


def _deny(status, detail, location=None):
    from fastapi import HTTPException
    headers = {"Cache-Control": "no-store"}
    if location is not None:
        headers["Location"] = location
    raise HTTPException(status_code=status, detail=detail, headers=headers)


def _has_verified_owner(connect_callback, owner_tag, email):
    # Default tuple cursor works with the project's pooled psycopg2 connection.
    # Do not use its context manager: __exit__ commits even read-only transactions.
    conn = connect_callback()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM player_verifications "
            "WHERE player_tag = %s AND identity_email = %s "
            "AND status = 'verified' AND revoked_at IS NULL LIMIT 1",
            (owner_tag, email),
        )
        return cur.fetchone() is not None
    finally:
        try:
            if cur is not None:
                cur.close()
        finally:
            # End the SELECT transaction without committing or mutating data.
            try:
                conn.rollback()
            finally:
                conn.close()


def require_standalone_access(request, identity_email_callback, connect_callback):
    """Enforce route policy, raising HTTPException before endpoint side effects.

    Callbacks are synchronous: identity_email_callback(request) must validate the
    current opaque email session (including revocation/expiry) on every request;
    connect_callback() returns the existing pooled DB connection. Return None on
    success. DB/identity errors propagate, never authorize. No session writes,
    network, cache, list creation, commits, or account selection occur here.
    """
    path, method = request.url.path, request.method.upper()
    role = classify_route(method, path)
    if role == "public":
        return None
    # .session asserts if middleware is absent: fail closed, never assume {}.
    session = request.session
    selected = session.get("owner_tag")
    if role == "root" and not selected:
        return None  # public landing; no private owner/list data is authorized
    email = identity_email_callback(request)
    if not isinstance(email, str) or not email.strip():
        if method in _READ_METHODS and not path.startswith("/api/"):
            _deny(303, "Log in with your verified email first.", "/login?next=" + quote(path, safe=""))
        _deny(401, "Log in with your verified email first.")
    if role == "email":
        return None
    owner_tag = _owner_tag(session)
    if owner_tag and _has_verified_owner(connect_callback, owner_tag, email):
        return None
    if role == "root":
        if not owner_tag:
            _deny(403, "Select a valid player tag before accessing private lists.")
        _deny(303, "Verify player ownership first.", "/verify-player/" + quote(owner_tag[1:], safe=""))
    _deny(403, "Verify ownership of the selected player with this email first.")

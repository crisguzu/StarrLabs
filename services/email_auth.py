"""Passwordless StarrLabs email authentication delivered through Resend."""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
COOKIE_NAME = "starrlabs_auth"
OTP_TTL_MINUTES = 10
SESSION_TTL_DAYS = 30
MAX_CODE_ATTEMPTS = 5
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ValueError("Enter a valid email address.")
    return email


def safe_next_path(value: str | None) -> str:
    candidate = (value or "/").strip()
    parsed = urlparse(candidate)
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate or any(ord(c) < 32 or ord(c) == 127 for c in candidate) or parsed.scheme or parsed.netloc:
        return "/"
    return candidate[:1000]


def _secret() -> bytes:
    value = os.getenv("EMAIL_AUTH_SECRET") or os.getenv("SESSION_SECRET")
    if not value or len(value) < 32 or value in {"dev-only-change-me", "GENERATE_A_DIFFERENT_LONG_RANDOM_LOCAL_SECRET", "GENERATE_A_LONG_RANDOM_LOCAL_SECRET"}:
        raise RuntimeError("EMAIL_AUTH_SECRET or a production SESSION_SECRET is required")
    return value.encode("utf-8")


def opaque_hash(value: str) -> str:
    return hmac.new(_secret(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def code_digest(challenge_id: uuid.UUID | str, code: str) -> str:
    return opaque_hash(f"otp:{challenge_id}:{code}")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_request_value(value: str | None) -> str | None:
    return opaque_hash(value) if value else None


def verify_turnstile(token: str, remote_ip: str | None = None) -> bool:
    """Verify a browser challenge before spending an outbound login email."""
    secret = os.getenv("TURNSTILE_SECRET_KEY")
    if not secret:
        raise RuntimeError("TURNSTILE_SECRET_KEY is not configured")
    if not token:
        return False
    payload = {"secret": secret, "response": token}
    if remote_ip and remote_ip != "unknown":
        payload["remoteip"] = remote_ip
    try:
        response = requests.post(TURNSTILE_VERIFY_URL, data=payload, timeout=8)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        return False
    from standalone_security import allowed_hosts
    hosts = {v.strip().lower() for v in os.getenv("STARRLABS_TURNSTILE_HOSTNAMES", ",".join(allowed_hosts())).split(",") if v.strip()}
    return bool(result.get("success")) and str(result.get("hostname") or "").lower() in hosts


def session_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)


def challenge_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)


def render_login_email(code: str) -> tuple[str, str, str]:
    safe_code = html.escape(code)
    subject = "Your StarrLabs login code"
    text = (
        "STARRLABS — BATTLE RESEARCH DIVISION\n\n"
        f"Your login code is: {code}\n\n"
        "This single-use code expires in 10 minutes.\n"
        "StarrLabs will never ask for your Supercell password.\n\n"
        "If you did not request this code, you can ignore this email."
    )
    markup = f"""<!doctype html>
<html><body style="margin:0;background:#08182d;color:#eefcff;font-family:Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#08182d;padding:28px 12px">
<tr><td align="center"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#fff4d9;border:4px solid #101b38;border-radius:18px;overflow:hidden;box-shadow:0 12px 0 #050d1d">
<tr><td style="background:#0b3047;padding:24px 28px;border-bottom:5px solid #f4bd38">
<div style="color:#5ee7e2;font-size:13px;font-weight:800;letter-spacing:2px">BATTLE RESEARCH DIVISION</div>
<div style="color:#fff4d9;font-size:34px;font-weight:900;letter-spacing:.5px;margin-top:4px">StarrLabs</div>
</td></tr>
<tr><td style="padding:34px 28px 28px;color:#17233f">
<div style="font-size:13px;font-weight:800;letter-spacing:1.5px;color:#a72e69">IDENTITY CLEARANCE</div>
<h1 style="font-size:25px;line-height:1.2;margin:8px 0 12px">Your login code is ready</h1>
<p style="font-size:16px;line-height:1.55;margin:0 0 22px;color:#43516a">Enter this code in StarrLabs to confirm that you control this email address.</p>
<div style="background:#0a2037;border:3px solid #142748;border-radius:14px;color:#f8ce4b;font-family:'Courier New',monospace;font-size:38px;font-weight:900;letter-spacing:9px;text-align:center;padding:20px 8px">{safe_code}</div>
<p style="font-size:14px;line-height:1.55;margin:22px 0 0;color:#5f718a"><strong>Expires in 10 minutes.</strong> This code is single-use. Requesting another code replaces this one.</p>
<div style="margin-top:24px;padding-top:20px;border-top:2px solid #dacba8;font-size:13px;line-height:1.55;color:#5f718a">StarrLabs will never ask for your Supercell password. If you did not request this code, you can safely ignore this email.</div>
</td></tr></table></td></tr></table></body></html>"""
    return subject, markup, text


def send_login_email(email: str, code: str) -> str:
    api_key = os.getenv("RESEND_API_KEY")
    sender = os.getenv("STARRLABS_EMAIL_FROM", "StarrLabs <operator@example.invalid>")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")
    subject, markup, text = render_login_email(code)
    payload = {"from": sender, "to": [email], "subject": subject, "html": markup, "text": text}
    reply_to = os.getenv("STARRLABS_EMAIL_REPLY_TO")
    if reply_to:
        payload["reply_to"] = reply_to
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=(5, 15),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Resend delivery failed with HTTP {response.status_code}")
    delivery_id = (response.json() or {}).get("id")
    if not delivery_id:
        raise RuntimeError("Resend did not return a delivery id")
    return str(delivery_id)

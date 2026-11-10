"""Fail-closed boundaries for the independently hosted distribution."""
import os
from urllib.parse import urlsplit

def allowed_hosts():
    values=[v.strip().lower() for v in os.getenv("STARRLABS_ALLOWED_HOSTS","127.0.0.1,localhost").split(",") if v.strip()]
    if not values or any("*" in v or "/" in v or ":" in v for v in values):
        raise RuntimeError("Set explicit STARRLABS_ALLOWED_HOSTS without wildcards, schemes or ports")
    return values

def allowed_origins():
    values={v.strip().rstrip("/").lower() for v in os.getenv("STARRLABS_ALLOWED_ORIGINS","http://127.0.0.1:8000,http://localhost:8000").split(",") if v.strip()}
    if not values:
        raise RuntimeError("STARRLABS_ALLOWED_ORIGINS must not be empty")
    for value in values:
        p=urlsplit(value)
        if p.scheme not in {"http","https"} or not p.hostname or p.username or p.password or p.path or p.query or p.fragment:
            raise RuntimeError("STARRLABS_ALLOWED_ORIGINS must contain exact HTTP(S) origins only")
    return values

def legacy_operator_path(path):
    value=path.lower().rstrip("/")
    return value=="/missy" or value.startswith("/missy/") or value=="/api/missy" or value.startswith("/api/missy/") or value=="/auth/scid-test" or value.startswith("/api/auth/scid-poc/")

def trusted_mutation(headers):
    if headers.get("sec-fetch-site", "").lower()=="cross-site":
        return False
    origin=headers.get("origin", "").rstrip("/").lower()
    return bool(origin) and origin in allowed_origins()

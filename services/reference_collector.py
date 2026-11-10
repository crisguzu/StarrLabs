"""Collect official Supercell reference feeds with immutable provenance."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import requests
from dotenv import load_dotenv

from database import connect


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
BASE_URL = "https://api.brawlstars.com/v1"
SESSION = requests.Session()


def _headers() -> Dict[str, str]:
    token = os.getenv("API_TOKEN")
    if not token:
        raise RuntimeError("API_TOKEN is not set")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _hash(payload: Any) -> str:
    value = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fetch(endpoint: str) -> Tuple[Any, int, Dict[str, str]]:
    started = time.monotonic()
    response = SESSION.get(f"{BASE_URL}{endpoint}", headers=_headers(), timeout=30)
    response.raise_for_status()
    return response.json(), int((time.monotonic() - started) * 1000), dict(response.headers)


def _item_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return len(payload.get("items", []) or [])
    return 0


def persist(endpoint: str, scope_key: str, payload: Any, latency_ms: int) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_polls (
                  endpoint, completed_at, http_status, api_latency_ms, returned_items, success
                ) VALUES (%s, now(), 200, %s, %s, true) RETURNING id
                """,
                (endpoint, latency_ms, _item_count(payload)),
            )
            poll_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO official_reference_snapshots (
                  endpoint, scope_key, source_poll_id, payload_hash, payload
                ) VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (endpoint, scope_key, payload_hash) DO UPDATE SET
                  captured_at = now(), source_poll_id = EXCLUDED.source_poll_id
                """,
                (endpoint, scope_key, poll_id, _hash(payload), json.dumps(payload)),
            )
        conn.commit()
    finally:
        conn.close()


def collect_one(endpoint: str, scope_key: str = "") -> Any:
    payload, latency_ms, _headers_map = fetch(endpoint)
    persist(endpoint, scope_key, payload, latency_ms)
    return payload


def main() -> None:
    events = collect_one("/events/rotation")
    catalog = collect_one("/brawlers")
    players = collect_one("/rankings/global/players")
    clubs = collect_one("/rankings/global/clubs")

    # Per-brawler rankings are official and relatively slow-changing. Collect
    # all catalog brawlers daily with the same global rate budget.
    ranked_brawlers = 0
    if os.getenv("COLLECT_BRAWLER_RANKINGS", "1") == "1":
        for brawler in catalog.get("items", []) or []:
            brawler_id = brawler.get("id")
            if brawler_id is None:
                continue
            endpoint = f"/rankings/global/brawlers/{urllib.parse.quote(str(brawler_id))}"
            collect_one(endpoint, str(brawler_id))
            ranked_brawlers += 1
            time.sleep(float(os.getenv("REFERENCE_REQUEST_DELAY", "0.12")))

    print(
        "REFERENCE_COLLECTION_OK|"
        f"events={_item_count(events)}|"
        f"brawlers={len(catalog.get('items', []) or [])}|"
        f"players={len(players.get('items', []) or [])}|"
        f"clubs={len(clubs.get('items', []) or [])}|"
        f"brawler_rankings={ranked_brawlers}"
    )


if __name__ == "__main__":
    main()

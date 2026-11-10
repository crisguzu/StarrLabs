"""Collect immutable, versioned BrawlAPI game/reference datasets."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from database import connect


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
BASE_URL = "https://api.brawlapi.com"
PROVIDER = "brawlapi"
SESSION = requests.Session()
REQUEST_DELAY = float(os.getenv("BRAWLAPI_REQUEST_DELAY", "0.15"))
REQUEST_TIMEOUT = float(os.getenv("BRAWLAPI_REQUEST_TIMEOUT", "120"))

CURATED_DATASETS = (
    "v1/brawlers",
    "v1/maps",
    "v1/gamemodes",
    "v1/events",
)

RAW_DATASETS = tuple(
    f"v2/raw/{path}" for path in (
        "csv_logic/locations",
        "csv_logic/maps",
        "csv_logic/map_templates",
        "csv_logic/tiles",
        "csv_logic/characters",
        "csv_logic/class_archetypes",
        "csv_logic/traits",
        "csv_logic/character_actions",
        "csv_logic/character_components_logic",
        "csv_logic/character_conditions",
        "csv_logic/skills",
        "csv_logic/projectiles_logic",
        "csv_logic/area_effects_logic",
        "csv_logic/status_effects_logic",
        "csv_logic/accessories",
        "csv_logic/cards",
        "csv_logic/items",
        "csv_logic/gear_boosts",
        "csv_logic/gear_levels",
        "csv_logic/gear_rarities",
        "csv_logic/event_modifiers",
        "csv_logic/event_slots",
        "csv_logic/game_mode_variations",
        "csv_logic/ranked_locations",
        "localization/texts",
    )
)

DATASETS = CURATED_DATASETS + RAW_DATASETS


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _shape(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        samples = [item for item in value if item is not None]
        return [(_shape(samples[0]) if samples else "empty")]
    if value is None:
        return "null"
    return type(value).__name__


def schema_descriptor(payload: Any) -> Any:
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and metadata.get("columns") is not None:
        return {
            "columns": metadata.get("columns"),
            "types": metadata.get("types"),
            "hasId": metadata.get("hasId"),
            "dataContainer": type(payload.get("data")).__name__,
        }
    return _shape(payload)


def schema_fingerprint(payload: Any) -> str:
    return hashlib.sha256(canonical_json(schema_descriptor(payload)).encode("utf-8")).hexdigest()


def row_count(payload: Any) -> int:
    if isinstance(payload, dict):
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("rowCount") is not None:
            return int(metadata["rowCount"])
        for key in ("list", "items"):
            if isinstance(payload.get(key), list):
                return len(payload[key])
        return len(payload)
    if isinstance(payload, list):
        return len(payload)
    return 0


def upstream_generated_at(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("generatedAt")
    if not value:
        return None
    # Validate before sending it to PostgreSQL as timestamptz.
    datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return str(value)


def header_value(headers: Dict[str, str], name: str) -> Optional[str]:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def latest_snapshot(dataset_path: str) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, upstream_etag, schema_fingerprint
                FROM third_party_reference_snapshots
                WHERE provider = %s AND dataset_path = %s
                ORDER BY last_observed_at DESC, id DESC
                LIMIT 1
                """,
                (PROVIDER, dataset_path),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"id": row[0], "etag": row[1], "schema_fingerprint": row[2]}
    finally:
        conn.close()


def fetch(dataset_path: str, etag: Optional[str]) -> Tuple[int, Optional[Any], int, Dict[str, str], int]:
    headers = {"Accept": "application/json", "User-Agent": "StarrLabsReferenceCollector/1.0"}
    if etag:
        headers["If-None-Match"] = etag
    started = time.monotonic()
    response = SESSION.get(f"{BASE_URL}/{dataset_path}", headers=headers, timeout=REQUEST_TIMEOUT)
    latency_ms = int((time.monotonic() - started) * 1000)
    if response.status_code == 304:
        return 304, None, latency_ms, dict(response.headers), 0
    response.raise_for_status()
    return response.status_code, response.json(), latency_ms, dict(response.headers), len(response.content)


def persist_not_modified(dataset_path: str, latest: Dict[str, Any], latency_ms: int, headers: Dict[str, str]) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_polls (
                  endpoint, completed_at, http_status, api_latency_ms, success, metadata
                ) VALUES (%s, now(), 304, %s, true, %s::jsonb) RETURNING id
                """,
                (f"brawlapi:{dataset_path}", latency_ms, json.dumps({"provider": PROVIDER, "not_modified": True})),
            )
            poll_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE third_party_reference_snapshots SET last_observed_at = now() WHERE id = %s",
                (latest["id"],),
            )
            cur.execute(
                """
                INSERT INTO third_party_reference_observations (
                  source_poll_id, snapshot_id, http_status, not_modified, response_etag
                ) VALUES (%s, %s, 304, true, %s)
                """,
                (poll_id, latest["id"], header_value(headers, "ETag") or latest.get("etag")),
            )
        conn.commit()
        return poll_id
    finally:
        conn.close()


def persist_payload(
    dataset_path: str, payload: Any, latency_ms: int,
    headers: Dict[str, str], payload_bytes: int, latest: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    content_hash = payload_hash(payload)
    fingerprint = schema_fingerprint(payload)
    changed_schema = bool(latest and latest.get("schema_fingerprint") != fingerprint)
    etag = header_value(headers, "ETag")
    rows = row_count(payload)
    generated_at = upstream_generated_at(payload)
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_polls (
                  endpoint, completed_at, http_status, api_latency_ms,
                  returned_items, success, metadata
                ) VALUES (%s, now(), 200, %s, %s, true, %s::jsonb) RETURNING id
                """,
                (
                    f"brawlapi:{dataset_path}", latency_ms, rows,
                    json.dumps({"provider": PROVIDER, "payload_bytes": payload_bytes, "schema_changed": changed_schema}),
                ),
            )
            poll_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO third_party_reference_snapshots (
                  provider, dataset_path, source_poll_id, upstream_generated_at,
                  upstream_etag, payload_hash, schema_fingerprint,
                  payload_bytes, row_count, payload
                ) VALUES (%s, %s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (provider, dataset_path, payload_hash) DO UPDATE SET
                  last_observed_at = now(),
                  upstream_etag = COALESCE(EXCLUDED.upstream_etag, third_party_reference_snapshots.upstream_etag)
                RETURNING id, (xmax = 0) AS inserted
                """,
                (
                    PROVIDER, dataset_path, poll_id, generated_at, etag,
                    content_hash, fingerprint, payload_bytes, rows, json.dumps(payload),
                ),
            )
            snapshot_id, inserted = cur.fetchone()
            cur.execute(
                """
                INSERT INTO third_party_reference_observations (
                  source_poll_id, snapshot_id, http_status, schema_changed,
                  response_etag, metadata
                ) VALUES (%s, %s, 200, %s, %s, %s::jsonb)
                """,
                (
                    poll_id, snapshot_id, changed_schema, etag,
                    json.dumps({"content_changed": bool(inserted), "row_count": rows}),
                ),
            )
        conn.commit()
        return {
            "dataset": dataset_path,
            "rows": rows,
            "bytes": payload_bytes,
            "new_content": bool(inserted),
            "schema_changed": changed_schema,
        }
    finally:
        conn.close()


def persist_failure(dataset_path: str, error: Exception) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_polls (
                  endpoint, completed_at, success, error_category, error_message, metadata
                ) VALUES (%s, now(), false, 'brawlapi_fetch_failed', %s, %s::jsonb)
                """,
                (f"brawlapi:{dataset_path}", str(error)[:1000], json.dumps({"provider": PROVIDER})),
            )
        conn.commit()
    finally:
        conn.close()


def collect_dataset(dataset_path: str) -> Dict[str, Any]:
    latest = latest_snapshot(dataset_path)
    status, payload, latency_ms, headers, response_bytes = fetch(
        dataset_path, latest.get("etag") if latest else None
    )
    if status == 304:
        persist_not_modified(dataset_path, latest, latency_ms, headers)
        return {"dataset": dataset_path, "not_modified": True, "rows": 0, "bytes": 0}
    return persist_payload(dataset_path, payload, latency_ms, headers, response_bytes, latest)


def main() -> None:
    results: List[Dict[str, Any]] = []
    failures: List[Tuple[str, str]] = []
    for dataset_path in DATASETS:
        try:
            result = collect_dataset(dataset_path)
            results.append(result)
            print("BRAWLAPI_DATASET_OK|" + "|".join(f"{k}={v}" for k, v in result.items()))
        except Exception as exc:
            persist_failure(dataset_path, exc)
            failures.append((dataset_path, str(exc)))
            print(f"BRAWLAPI_DATASET_FAILED|dataset={dataset_path}|error={exc}")
        time.sleep(REQUEST_DELAY)

    print(
        "BRAWLAPI_REFERENCE_COLLECTION|"
        f"datasets={len(results)}|failures={len(failures)}|"
        f"rows={sum(r.get('rows', 0) for r in results)}|"
        f"bytes={sum(r.get('bytes', 0) for r in results)}|"
        f"new_content={sum(bool(r.get('new_content')) for r in results)}|"
        f"schema_changes={sum(bool(r.get('schema_changed')) for r in results)}"
    )
    if failures:
        raise RuntimeError(f"BrawlAPI reference collection failed for {len(failures)} datasets: {failures}")


if __name__ == "__main__":
    main()
